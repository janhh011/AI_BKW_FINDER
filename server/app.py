#!/usr/bin/env python3
"""
balki strom – Webserver mit API
================================
Liefert die Webseite aus (webapp/) und rechnet auf Abruf:

  GET  /api/status                      Ist die Balkonsuche verfügbar?
  GET  /api/fassade/<id>                Bekannte Balkone einer Hauswand
  POST /api/fassade/<id>/analyse        Balkone dieser einen Hauswand mit Claude suchen (einmal je Wand, danach Cache)
  POST /api/angebot                     {"fassade": 376, "balkon": 0}  oder
                                        {"fassade": 376, "box": [x0, y0, x1, y1], "gelaender": "staebe"}
                                        -> {"id": "376_0"}  (Angebotsseite unter /angebot/?id=...)

Start (lokal):
  python3 server/app.py data/<kachel> webapp --port 8000

Sicherheit (Kurzfassung, Details in docs/ARCHITEKTUR.md):
  - API-Key nur serverseitig (.env / Umgebungsvariable), nie im Browser.
  - Jede Hauswand wird höchstens einmal analysiert -> Kosten sind nach oben durch die Anzahl der Wände begrenzt.
  - Zusätzlich Tagesbudget und Limits je IP, kleine JSON-Bodies, strikte Prüfung aller Eingaben.
  - Statische Dateien nur aus webapp/, ohne _cache/ und versteckte Dateien.
  - Es werden keine personenbezogenen Daten gespeichert (keine IPs auf Platte, kein Tracking).
"""
import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import angebot as A  # noqa: E402

GELAENDER = {"staebe": "Stäbe/Gitter", "glas": "Glas", "mauer": "Mauer/Brüstung", "blech": "Blech/Platte"}
MAX_BODY = 2048

LIMITS = {                       # (Anzahl, Zeitfenster in s) je IP
    "analyse": (int(os.environ.get("LIMIT_ANALYSE_STUNDE", 6)), 3600),
    "angebot": (int(os.environ.get("LIMIT_ANGEBOT_STUNDE", 30)), 3600),
    "lesen": (int(os.environ.get("LIMIT_LESEN_MINUTE", 120)), 60),
}
TAGESBUDGET = int(os.environ.get("ANALYSEN_PRO_TAG", 300))      # Claude-Aufrufe je Tag, alle Nutzer zusammen
MODELL = os.environ.get("BKW_MODELL", "sonnet")
# Automatische Balkonsuche (Claude) nur für sonnige Wände; alle anderen: selbst einzeichnen
ANALYSE_RICHTUNGEN = set(os.environ.get("ANALYSE_RICHTUNGEN", "S,SO,SW").split(","))


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Begrenzer:
    """Gleitendes Zeitfenster je (IP, Art). Nur im Speicher, wird nie gespeichert."""

    def __init__(self):
        self.t = defaultdict(deque)
        self.lock = threading.Lock()

    def erlaubt(self, ip, art):
        n, fenster = LIMITS[art]
        now = time.time()
        with self.lock:
            q = self.t[(ip, art)]
            while q and q[0] < now - fenster:
                q.popleft()
            if len(q) >= n:
                return False
            q.append(now)
            if len(self.t) > 50000:            # Speicher begrenzen
                self.t.clear()
            return True


class App:
    def __init__(self, tile_dir, webapp, wetter=None):
        self.webapp = os.path.realpath(webapp)
        self.R = A.Rechner(tile_dir, webapp, wetter)
        self.rechnen = threading.Lock()        # numpy-Rechnung: eine nach der anderen
        self.claude_sem = threading.Semaphore(3)
        self.live_path = os.path.join(webapp, "_cache", "balkone_live.json")
        self.nutzer_path = os.path.join(webapp, "_cache", "balkone_nutzer.json")
        self.live = self._lade(self.live_path)
        self.nutzer = self._lade(self.nutzer_path)
        for fid, e in self.live.items():
            self.R.BK.setdefault(fid, e)
        self.datei_lock = threading.Lock()
        self.analyse_locks = defaultdict(threading.Lock)
        self.tag, self.heute = time.strftime("%Y-%m-%d"), 0
        self.client = None
        try:
            import balkone_claude as BC
            self.BC = BC
            if not os.environ.get("ANTHROPIC_API_KEY"):
                env = os.path.join(ROOT, ".env")
                if os.path.exists(env):
                    for line in open(env, encoding="utf-8"):
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            if os.environ.get("ANTHROPIC_API_KEY"):
                self.client = BC.anthropic.Anthropic()
        except SystemExit:
            self.BC = None
        log(f"Balkonsuche mit Claude: {'an' if self.client else 'aus (kein API-Key oder Paket anthropic fehlt)'}")
        self.angebote = {a["id"] for a in self._angebote_js()}

    @staticmethod
    def _lade(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _speichere(self, p, d):
        with self.datei_lock:
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh, ensure_ascii=False)
            os.replace(tmp, p)

    def _angebote_js(self):
        try:
            s = open(os.path.join(self.webapp, "angebote.js"), encoding="utf-8").read()
            return json.loads(s[s.index("=") + 1:].strip().rstrip(";"))
        except (OSError, ValueError):
            return []

    # ---------------------------------------------------------------- Fassaden
    def fassade(self, fid):
        f = self.R.fac.get(fid)
        if f is None:
            return None
        e = self.R.BK.get(str(fid))
        out = dict(id=fid, analysiert=e is not None, analyse_moeglich=f["cls"] in ANALYSE_RICHTUNGEN and f.get("img_quelle") != "schema",
                   schema=f.get("img_quelle") == "schema", bild=f.get("img"), cls=f["cls"], az=f["az"],
                   breite_m=f["len"], hoehe_m=f["h"], img_res=f.get("img_res"))
        if e is not None:
            out.update(geschosse=e.get("geschosse"), bildqualitaet=e.get("bildqualitaet"), balkone=[
                dict(i=i, box=b["box"], typ=b["typ"], geschoss=b.get("geschoss"), breite_m=b["breite_m"], sicherheit=b.get("sicherheit"),
                     angebot=f"{fid}_{i}" if f"{fid}_{i}" in self.angebote else None)
                for i, b in enumerate(e.get("balkone", []))])
        return out

    def analysiere(self, fid):
        f = self.R.fac.get(fid)
        if f is None or not f.get("img"):
            raise ValueError("Für diese Hauswand gibt es kein Bild.")
        if f.get("img_quelle") == "schema":
            raise ValueError("Für diese Hauswand gibt es nur eine schematische Ansicht. Bitte zeichne deinen Balkon selbst ein.")
        if f["cls"] not in ANALYSE_RICHTUNGEN:
            raise ValueError("Die automatische Suche gibt es nur für Hauswände nach Süden, Südosten und Südwesten. "
                             "Bitte zeichne deinen Balkon selbst ein.")
        with self.analyse_locks[fid]:              # gleichzeitige Anfragen zur selben Wand: nur ein Aufruf
            if str(fid) in self.R.BK:
                return self.fassade(fid)
            if not self.client:
                raise RuntimeError("Die automatische Balkonsuche ist gerade nicht verfügbar.")
            heute = time.strftime("%Y-%m-%d")
            if heute != self.tag:
                self.tag, self.heute = heute, 0
            if self.heute >= TAGESBUDGET:
                raise RuntimeError("Heute sind schon sehr viele Hauswände untersucht worden. Bitte morgen noch einmal – "
                                   "oder Balkon einfach selbst einzeichnen.")
            self.heute += 1
            BC = self.BC
            b64, sent, orig = BC.prepare_image(os.path.join(self.webapp, f["img"]))
            model = BC.MODELS.get(MODELL, MODELL)
            with self.claude_sem:
                t0 = time.time()
                try:
                    msg = self._frage_claude(BC, model, b64, f, sent, orig)
                except BC.anthropic.APIError as e:
                    self.heute -= 1
                    log("Claude-Fehler:", type(e).__name__, getattr(e, "status_code", ""))
                    raise RuntimeError("Die automatische Balkonsuche ist gerade nicht verfügbar. "
                                       "Du kannst deinen Balkon aber selbst einzeichnen.") from None
            res = BC.parse_answer(msg)
            info = {"sent": sent, "orig": orig}
            entry = dict(model=model, bildqualitaet=res.get("bildqualitaet"), geschosse=res.get("geschosse"),
                         anmerkung=res.get("anmerkung", ""),
                         balkone=[BC.measure(bb, f, info) for bb in res.get("balkone", []) if len(bb.get("box", [])) == 4],
                         tokens=[msg.usage.input_tokens, msg.usage.output_tokens], quelle="live",
                         datum=time.strftime("%Y-%m-%d"))
            self.live[str(fid)] = entry
            self.R.BK[str(fid)] = entry
            self._speichere(self.live_path, self.live)
            log(f"Analyse Fassade {fid}: {len(entry['balkone'])} Balkone, {time.time() - t0:.1f} s, Tokens {entry['tokens']}")
            return self.fassade(fid)

    def _frage_claude(self, BC, model, b64, f, sent, orig):
        return self.client.messages.create(
            model=model, max_tokens=BC.MAX_TOKENS, system=BC.SYSTEM, tools=[BC.TOOL],
            tool_choice={"type": "auto"}, timeout=90,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": BC.user_text(f, sent, orig)}]}])

    # ---------------------------------------------------------------- Angebote
    def angebot_erkannt(self, fid, bi):
        e = self.R.BK.get(str(fid))
        if e is None or not (0 <= bi < len(e.get("balkone", []))):
            raise ValueError("Diesen Balkon kennen wir nicht.")
        key = f"{fid}_{bi}"
        if key in self.angebote or os.path.exists(os.path.join(self.R.adir, key, "daten.json")):
            return key
        with self.rechnen:
            d = self.R.angebot(fid, bi, e["balkone"], key=key)
        if d is None:
            raise ValueError("Auf diesen Balkon passt leider kein Modul.")
        self.angebote.add(key)
        return key

    def balkon_zu(self, basis):
        """Angebots-Kennung (z. B. 376_0 oder 376_u1a2b…) -> (Fassade, Balkonliste, Index)."""
        m = BASIS.match(basis)
        if not m:
            raise ValueError("Unbekanntes Angebot.")
        fid, teil = int(m.group(1)), m.group(2)
        f = self.R.fac.get(fid)
        if f is None:
            raise ValueError("Unbekanntes Angebot.")
        alle = list((self.R.BK.get(str(fid)) or {}).get("balkone", []))
        if teil.startswith("u"):
            n = self.nutzer.get(basis)
            if not n:
                raise ValueError("Unbekanntes Angebot.")
            alle.append(A.balkon_aus_eingabe(f, n["box"], gelaender=GELAENDER.get(n["gelaender"])))
            return fid, alle, len(alle) - 1
        bi = int(teil)
        if not 0 <= bi < len(alle):
            raise ValueError("Unbekanntes Angebot.")
        return fid, alle, bi

    def angebot_ohne_baeume(self, basis, kronen):
        """Neu rechnen, nachdem der Nutzer Bäume entfernt hat, die es nicht (mehr) gibt."""
        fid, alle, bi = self.balkon_zu(basis)
        kronen = sorted({(round(x, 1), round(y, 1), round(r, 1)) for x, y, r in kronen})
        if not kronen:
            return basis
        h = hashlib.sha1(json.dumps([basis, kronen]).encode()).hexdigest()[:10]
        key = f"{basis}_b{h}"
        if os.path.exists(os.path.join(self.R.adir, key, "daten.json")):
            return key
        with self.rechnen:
            geo = A.balkon_geometrie(self.R.fac[fid], alle[bi], *self.R.geo_wolke(self.R.fac[fid]))
            if geo is None:
                raise ValueError("Dieser Balkon lässt sich nicht neu berechnen.")
            P, baum, n = self.R.ohne_baeume(geo["mitte"], kronen)
            d = self.R.angebot(fid, bi, alle, key=key, P=P, baum=baum,
                               extra=dict(basis=basis, ohne_baeume=[list(k) for k in kronen]))
        if d is None:
            raise ValueError("Dieser Balkon lässt sich nicht neu berechnen.")
        log(f"Angebot {key}: {len(kronen)} Bäume entfernt ({n} Punkte)")
        return key

    def angebot_selbst(self, fid, box, gelaender):
        f = self.R.fac.get(fid)
        if f is None or not f.get("img_res") or not f.get("img"):
            raise ValueError("Unbekannte Hauswand.")
        from PIL import Image
        with Image.open(os.path.join(self.webapp, f["img"])) as im:
            W, H = im.size
        x0, y0, x1, y1 = box
        if not (0 <= min(x0, x1) and max(x0, x1) <= W and 0 <= min(y0, y1) and max(y0, y1) <= H):
            raise ValueError("Der Balkon liegt außerhalb der Hauswand.")
        breite = abs(x1 - x0) * f["img_res"]
        if not 0.8 <= breite <= 12:
            raise ValueError("Bitte eine Breite zwischen 0,8 und 12 m einzeichnen.")
        z_unten = f["img_ztop"] - max(y0, y1) * f["img_res"]
        if f.get("frei_ab") is not None and z_unten < f["frei_ab"] - 0.5:
            raise ValueError("Dieser Teil der Wand grenzt an ein Nachbarhaus oder ein tieferes Dach. "
                             "Bitte den Balkon im hellen, freien Teil der Wand einzeichnen.")
        # Balkon muss auf der Wand liegen (nicht in der Luft neben einem Gebäudeteil)
        sp = f.get("img_schema") or (f["img"] if f.get("img_quelle") == "schema" else None)
        if sp and os.path.exists(os.path.join(self.webapp, sp)):
            with Image.open(os.path.join(self.webapp, sp)) as sim:
                sch = sim.convert("RGB")
                if sch.size != (W, H):
                    sch = sch.resize((W, H), Image.NEAREST)
                xs = range(int(min(x0, x1)), int(max(x0, x1)), max(1, int(abs(x1 - x0) / 20)))
                yu = min(H - 1, int(max(y0, y1)) - 2)
                treffer = [sch.getpixel((x, yu)) for x in xs]
                auf = sum(1 for r, g, b in treffer if abs(r - 244) < 14 and abs(g - 238) < 14 and abs(b - 226) < 14)
                if treffer and auf / len(treffer) < 0.5:
                    raise ValueError("Der Balkon liegt nicht auf der Hauswand. Bitte direkt auf der hellen Wandfläche einzeichnen.")
        b = A.balkon_aus_eingabe(f, box, gelaender=GELAENDER.get(gelaender))
        h = hashlib.sha1(json.dumps([fid, b["box"], gelaender]).encode()).hexdigest()[:10]
        key = f"{fid}_u{h}"
        if os.path.exists(os.path.join(self.R.adir, key, "daten.json")):
            return key
        alle = list((self.R.BK.get(str(fid)) or {}).get("balkone", [])) + [b]
        with self.rechnen:
            d = self.R.angebot(fid, len(alle) - 1, alle, key=key)
        if d is None:
            raise ValueError("An dieser Stelle können wir leider nicht rechnen.")
        self.nutzer[key] = dict(fassade=fid, box=b["box"], gelaender=gelaender, datum=time.strftime("%Y-%m-%d"))
        self._speichere(self.nutzer_path, self.nutzer)
        return key


BASIS = re.compile(r"^(\d{1,6})_(\d{1,3}|u[0-9a-f]{10})$")
ANGEBOT_ID = re.compile(r"^\d{1,6}_(\d{1,3}|u[0-9a-f]{10})(_b[0-9a-f]{10})?$")


def handler_fuer(app, begrenzer, vertraue_proxy):
    webroot = app.webapp

    class H(BaseHTTPRequestHandler):
        server_version = "balki"
        sys_version = ""

        def log_message(self, fmt, *args):      # keine IPs ins Log
            pass

        def ip(self):
            if vertraue_proxy and self.client_address[0] in ("127.0.0.1", "::1"):
                xf = self.headers.get("X-Forwarded-For", "")
                if xf:
                    return xf.split(",")[-1].strip()
            return self.client_address[0]

        def sicherheits_header(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Permissions-Policy", "geolocation=(), camera=(), microphone=()")

        def json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.sicherheits_header()
            self.end_headers()
            self.wfile.write(body)

        def fehler(self, code, text):
            self.json(code, {"fehler": text})

        # ------------------------------------------------------------ GET
        def do_GET(self):
            pfad = urlparse(self.path).path
            if pfad.startswith("/api/"):
                if not begrenzer.erlaubt(self.ip(), "lesen"):
                    return self.fehler(429, "Zu viele Anfragen. Bitte kurz warten.")
                if pfad == "/api/status":
                    return self.json(200, {"analyse": app.client is not None, "angebot": True, "analyse_richtungen": sorted(ANALYSE_RICHTUNGEN)})
                teile = pfad.strip("/").split("/")
                if len(teile) == 3 and teile[1] == "fassade" and teile[2].isdigit():
                    f = app.fassade(int(teile[2]))
                    return self.json(200, f) if f else self.fehler(404, "Unbekannte Hauswand.")
                return self.fehler(404, "Unbekannt.")
            self.statisch(pfad)

        def do_HEAD(self):
            self.statisch(urlparse(self.path).path, nur_kopf=True)

        def statisch(self, pfad, nur_kopf=False):
            rel = unquote(pfad).lstrip("/")
            if rel == "" or rel.endswith("/"):
                rel += "index.html"
            teile = rel.split("/")
            if any(t.startswith(".") or t.startswith("_") for t in teile):
                return self.fehler(404, "Nicht gefunden.")
            ziel = os.path.realpath(os.path.join(webroot, rel))
            if not ziel.startswith(webroot + os.sep) or not os.path.isfile(ziel):
                return self.fehler(404, "Nicht gefunden.")
            typ = mimetypes.guess_type(ziel)[0] or "application/octet-stream"
            if typ.startswith("text/") or typ in ("application/javascript", "application/json"):
                typ += "; charset=utf-8"
            groesse = os.path.getsize(ziel)
            self.send_response(200)
            self.send_header("Content-Type", typ)
            self.send_header("Content-Length", str(groesse))
            self.send_header("Cache-Control", "no-cache" if ziel.endswith((".html", ".js")) else "public, max-age=86400")
            self.sicherheits_header()
            self.end_headers()
            if not nur_kopf:
                with open(ziel, "rb") as fh:
                    while True:
                        chunk = fh.read(1 << 16)
                        if not chunk:
                            break
                        self.wfile.write(chunk)

        # ------------------------------------------------------------ POST
        def do_POST(self):
            pfad = urlparse(self.path).path
            ctype = self.headers.get("Content-Type", "")
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if not ctype.startswith("application/json") or not 0 <= n <= MAX_BODY:
                return self.fehler(400, "Ungültige Anfrage.")
            try:
                daten = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(daten, dict):
                    raise ValueError
            except ValueError:
                return self.fehler(400, "Ungültige Anfrage.")
            teile = pfad.strip("/").split("/")
            if teile == ["api", "klick"]:
                # Klick auf einen Partnerlink zählen (für den Abgleich der Provisionen). Keine personenbezogenen Daten.
                if not begrenzer.erlaubt(self.ip(), "lesen"):
                    return self.fehler(429, "Zu viele Anfragen.")
                ang, st, pk = daten.get("angebot"), daten.get("set"), daten.get("paket")
                if not (isinstance(ang, str) and ANGEBOT_ID.match(ang) and isinstance(st, str) and re.match(r"^[a-z0-9-]{1,40}$", st)
                        and pk in ("schnupper", "einfach", "optimiert", "smart")):
                    return self.fehler(400, "Ungültige Anfrage.")
                with app.datei_lock:
                    with open(os.path.join(app.webapp, "_cache", "klicks.jsonl"), "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"zeit": time.strftime("%Y-%m-%d %H:00"), "angebot": ang, "set": st, "paket": pk}) + "\n")
                return self.json(200, {"ok": True})
            try:
                if len(teile) == 4 and teile[:2] == ["api", "fassade"] and teile[2].isdigit() and teile[3] == "analyse":
                    fid = int(teile[2])
                    if str(fid) in app.R.BK:                       # schon bekannt: kostet nichts
                        return self.json(200, app.fassade(fid))
                    if not begrenzer.erlaubt(self.ip(), "analyse"):
                        return self.fehler(429, "Du hast gerade schon einige Hauswände untersuchen lassen. "
                                                "Versuch es in einer Stunde wieder – oder zeichne deinen Balkon selbst ein.")
                    return self.json(200, app.analysiere(fid))
                if teile == ["api", "angebot"]:
                    if not begrenzer.erlaubt(self.ip(), "angebot"):
                        return self.fehler(429, "Zu viele Anfragen. Bitte später noch einmal.")
                    if "basis" in daten:
                        basis, kr = daten.get("basis"), daten.get("ohne_baeume")
                        if not isinstance(basis, str) or not isinstance(kr, list) or not 1 <= len(kr) <= 30:
                            raise ValueError("Ungültige Anfrage.")
                        ok = all(isinstance(k, list) and len(k) == 3 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                 and abs(v) <= A.MODELL_RADIUS + 10 for v in k) and 0 < k[2] <= 15 for k in kr)
                        if not ok:
                            raise ValueError("Ungültige Anfrage.")
                        return self.json(200, {"id": app.angebot_ohne_baeume(basis, [[float(v) for v in k] for k in kr])})
                    fid = daten.get("fassade")
                    if not isinstance(fid, int) or isinstance(fid, bool):
                        raise ValueError("Unbekannte Hauswand.")
                    if "box" in daten:
                        box = daten["box"]
                        if not (isinstance(box, list) and len(box) == 4
                                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box)):
                            raise ValueError("Ungültiger Balkon.")
                        gel = daten.get("gelaender")
                        if gel not in GELAENDER:
                            raise ValueError("Bitte die Geländerart wählen.")
                        key = app.angebot_selbst(fid, [float(v) for v in box], gel)
                    else:
                        bi = daten.get("balkon")
                        if not isinstance(bi, int) or isinstance(bi, bool):
                            raise ValueError("Ungültiger Balkon.")
                        key = app.angebot_erkannt(fid, bi)
                    return self.json(200, {"id": key})
                return self.fehler(404, "Unbekannt.")
            except ValueError as e:
                return self.fehler(400, str(e) or "Ungültige Anfrage.")
            except RuntimeError as e:
                return self.fehler(503, str(e))
            except Exception as e:      # noqa: BLE001 – Details nur ins Server-Log
                log("Fehler:", type(e).__name__, e)
                return self.fehler(500, "Da ist etwas schiefgegangen. Bitte später noch einmal.")

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tile_dir")
    ap.add_argument("webapp")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="hinter einem Reverse-Proxy immer 127.0.0.1")
    ap.add_argument("--wetter")
    ap.add_argument("--hinter-proxy", action="store_true", help="X-Forwarded-For von 127.0.0.1 übernehmen")
    a = ap.parse_args()
    app = App(a.tile_dir, a.webapp, a.wetter)
    srv = ThreadingHTTPServer((a.host, a.port), handler_fuer(app, Begrenzer(), a.hinter_proxy))
    srv.daemon_threads = True
    log(f"balki strom läuft auf http://{a.host}:{a.port}/")
    srv.serve_forever()


if __name__ == "__main__":
    main()
