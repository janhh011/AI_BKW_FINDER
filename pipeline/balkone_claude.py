#!/usr/bin/env python3
"""
Balkonerkennung mit der Claude API (Message Batches)
====================================================
Schickt die Frontalansichten der Fassaden (aus fassaden_pipeline.py) an Claude
und lässt Balkone/Loggien als Boxen mit Typ und Geschoss melden. Maße werden
nicht vom Modell geschätzt, sondern aus Box und bekanntem Bildmaßstab berechnet.

Batches laufen asynchron (meist Minuten, maximal 24 Stunden), daher drei Schritte:

  python3 pipeline/balkone_claude.py submit  --classes S --model sonnet
  python3 pipeline/balkone_claude.py status
  python3 pipeline/balkone_claude.py collect

Optionen für submit:
  --classes S,SO,SW   Himmelsrichtungen (Standard: S)
  --ids 733,624       statt --classes gezielt einzelne Fassaden
  --model sonnet      opus | sonnet | haiku (Standard: sonnet)
  --min-ev 0.15       Mindest-Wandnachweis (filtert Fehlkanten in Baumkronen)

Der API-Key wird aus ANTHROPIC_API_KEY oder einer .env-Datei im Projektordner
gelesen (.env niemals committen).

Abhängigkeiten: anthropic, pillow
"""
import argparse
import base64
import io
import json
import math
import os
import sys
import time

from PIL import Image, ImageDraw

try:
    import anthropic
except ImportError:
    sys.exit("Bitte zuerst installieren: pip3 install anthropic")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBAPP = os.path.join(ROOT, "webapp")
STATE = os.path.join(WEBAPP, "_cache", "claude_batch.json")
RESULTS = os.path.join(WEBAPP, "balkone.json")
RESULTS_JS = os.path.join(WEBAPP, "balkone.js")
REVIEW_DIR = os.path.join(ROOT, "_analyse", "claude")

MODELS = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}
# US-$ pro 1 Mio. Tokens, Batch-Preise (50 % Rabatt). Stand Okt. 2026, bei Bedarf anpassen.
BATCH_PRICES = {
    "claude-opus-5-5": (2.00, 10.00),
    "claude-sonnet-5-5": (1.00, 5.00),
    "claude-haiku-5-5": (0.05, 0.25),
}
MAX_EDGE = 1568           # längste Bildkante, ab der die API selbst verkleinern würde
MAX_PIXELS = 1_150_000    # Pixelgrenze, ab der die API selbst verkleinern würde
MAX_TOKENS = 3000

TOOL = {
    "name": "balkone_melden",
    "description": "Meldet alle auf der Fassade sichtbaren Balkone und Loggien.",
    "input_schema": {
        "type": "object",
        "properties": {
            "bildqualitaet": {"type": "string", "enum": ["gut", "mittel", "schlecht"]},
            "geschosse": {"type": "integer", "description": "Vollgeschosse inkl. Erdgeschoss"},
            "balkone": {
                "type": "array",
                "description": "Ein Eintrag pro einzelnem Balkon bzw. einzelner Loggia (pro Wohnung und Geschoss).",
                "items": {
                    "type": "object",
                    "properties": {
                        "box": {
                            "type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
                            "description": "[x0, y0, x1, y1] in Pixeln dieses Bildes, Ursprung oben links. "
                                           "Horizontal die gesamte Breite der Brüstung bzw. Öffnung.",
                        },
                        "typ": {"type": "string", "enum": ["Balkon", "Loggia", "unsicher"]},
                        "geschoss": {"type": "integer", "description": "0 = Erdgeschoss"},
                        "sicherheit": {"type": "number", "description": "0 bis 1"},
                    },
                    "required": ["box", "typ", "geschoss", "sicherheit"],
                },
            },
            "anmerkung": {"type": "string", "description": "Optional, höchstens ein Satz."},
        },
        "required": ["bildqualitaet", "geschosse", "balkone"],
    },
}

SYSTEM = (
    "Du findest Balkone und Loggien auf Fassadenbildern.\n\n"
    "Die Bilder sind keine Fotos, sondern Frontalansichten aus einem photogrammetrischen "
    "3D-Stadtmodell (Luftbildbefliegung): verwaschene Texturen, verzerrte Kanten, Lücken. "
    "Hellgraue strukturlose Flächen bedeuten 'keine Daten' (z. B. wo Bäume vor der Fassade "
    "herausgeschnitten wurden). Unten im Bild ist das Erdgeschoss.\n\n"
    "Melde jeden einzelnen Balkon und jede einzelne Loggia als eigenen Eintrag, "
    "übereinanderliegende also pro Geschoss einzeln. Die Box soll die volle Breite der "
    "Brüstung bzw. Loggienöffnung umfassen. Fenster ohne Austritt, Erker und reine "
    "Fassadenvorsprünge sind keine Balkone. Unsichere Fälle mit typ 'unsicher' und niedriger "
    "Sicherheit aufnehmen statt weglassen. Antworte ausschließlich über das Werkzeug "
    "balkone_melden, ohne weiteren Text."
)


# ----------------------------------------------------------------------------
def load_env():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY fehlt (Umgebungsvariable oder .env im Projektordner).")


def load_facades():
    raw = open(os.path.join(WEBAPP, "data.js"), encoding="utf-8").read()
    data = json.loads(raw.split("=", 1)[1].strip().rstrip(";"))
    return data["meta"], {f["id"]: f for f in data["facades"]}


def prepare_image(path):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    s = min(1.0, MAX_EDGE / max(w, h), (MAX_PIXELS / (w * h)) ** 0.5)
    if s < 1.0:
        im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return base64.standard_b64encode(buf.getvalue()).decode(), im.size, (w, h)


def facade_res(f, orig_w):
    """Meter pro Pixel im Originalbild der Fassade."""
    return f.get("img_res") or (f["len"] + 1.0) / orig_w


def user_text(f, sent, orig):
    m_px = facade_res(f, orig[0]) * orig[0] / sent[0]
    return (
        f"Fassade #{f['id']}, Breite ca. {f['len']:.1f} m, Höhe ca. {f['h']:.1f} m. "
        f"Bild {sent[0]} x {sent[1]} px, 1 px = {m_px:.3f} m "
        f"(ein Geschoss ist rund {3.0 / m_px:.0f} px hoch). Melde alle Balkone und Loggien."
    )


# ----------------------------------------------------------------------------
def cmd_submit(args):
    load_env()
    meta, facades = load_facades()
    if args.ids:
        ids = [int(x) for x in args.ids.split(",")]
    else:
        classes = {c.strip() for c in args.classes.split(",")}
        ids = sorted(fid for fid, f in facades.items()
                     if f["cls"] in classes and f.get("img") and f["ev"] >= args.min_ev)
    model = MODELS[args.model]
    requests, info = [], {}
    for fid in ids:
        f = facades[fid]
        if not f.get("img"):
            continue
        b64, sent, orig = prepare_image(os.path.join(WEBAPP, f["img"]))
        cid = f"f{fid}"
        requests.append({
            "custom_id": cid,
            "params": {
                "model": model,
                "max_tokens": MAX_TOKENS,
                "system": SYSTEM,
                "tools": [TOOL],
                # Opus/Sonnet 5.5 unterstützen kein erzwungenes Werkzeug -> "auto"
                "tool_choice": {"type": "auto"},
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": user_text(f, sent, orig)},
                ]}],
            },
        })
        info[cid] = {"facade": fid, "model": model, "sent": sent, "orig": orig, "img": f["img"]}
    if not requests:
        sys.exit("Keine Anfragen.")
    batch = anthropic.Anthropic().messages.batches.create(requests=requests)
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump({"batch_id": batch.id, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "requests": info},
              open(STATE, "w"), indent=1)
    print(f"Batch {batch.id} eingereicht: {len(requests)} Fassaden mit {model}")


def cmd_status(args):
    load_env()
    st = json.load(open(STATE))
    b = anthropic.Anthropic().messages.batches.retrieve(st["batch_id"])
    c = b.request_counts
    print(f"Batch {b.id}: {b.processing_status} | in Arbeit {c.processing}, fertig {c.succeeded}, "
          f"Fehler {c.errored}, abgebrochen {c.canceled}, abgelaufen {c.expired}")


def parse_answer(msg):
    tool = next((c for c in msg.content if c.type == "tool_use"), None)
    if tool:
        return tool.input
    text = "".join(c.text for c in msg.content if c.type == "text")
    try:
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        return {"balkone": [], "anmerkung": "keine auswertbare Antwort"}


def gueltige_box(b):
    """Antwort des Modells prüfen: Box aus vier endlichen Zahlen."""
    box = b.get("box") if isinstance(b, dict) else None
    return (isinstance(box, list) and len(box) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in box))


def measure(b, f, info):
    """Box ins Originalbild umrechnen und Maße in Metern bestimmen.
    Alle Felder aus der Modellantwort werden auf feste Typen und Wertebereiche gebracht (sie landen im Cache für alle)."""
    sx = info["orig"][0] / info["sent"][0]
    sy = info["orig"][1] / info["sent"][1]
    x0, y0, x1, y1 = (float(v) for v in b["box"])
    x0, x1 = sorted((x0 * sx, x1 * sx))
    y0, y1 = sorted((y0 * sy, y1 * sy))
    x0, x1 = max(0.0, x0), min(float(info["orig"][0]), x1)
    y0, y1 = max(0.0, y0), min(float(info["orig"][1]), y1)
    res = facade_res(f, info["orig"][0])
    out = {
        "box": [round(x0), round(y0), round(x1), round(y1)],
        "typ": b.get("typ") if b.get("typ") in ("Balkon", "Loggia", "unsicher") else "unsicher",
        "geschoss": _ganzzahl(b.get("geschoss"), 0, 40),
        "sicherheit": round(min(1.0, max(0.0, _zahl(b.get("sicherheit"), 0.0))), 2),
        "breite_m": round((x1 - x0) * res, 2),
        "hoehe_m": round((y1 - y0) * res, 2),
    }
    if f.get("img_ztop") is not None:
        out["unterkante_m"] = round(f["img_ztop"] - y1 * res - f["ground"], 1)   # über Gelände/Nachbardach
    return out


def _zahl(v, standard):
    try:
        v = float(v)
        return v if math.isfinite(v) else standard
    except (TypeError, ValueError):
        return standard


def _ganzzahl(v, lo, hi):
    if isinstance(v, bool) or v is None:
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def draw_review(entry, f, out_path):
    im = Image.open(os.path.join(WEBAPP, f["img"])).convert("RGB")
    d = ImageDraw.Draw(im)
    col = {"Balkon": (255, 40, 40), "Loggia": (255, 0, 200), "unsicher": (150, 150, 150)}
    for b in entry["balkone"]:
        c = col.get(b["typ"], (255, 255, 0))
        d.rectangle(b["box"], outline=c, width=3)
        d.text((b["box"][0] + 3, b["box"][1] + 2), f"{b['breite_m']:.1f} m", fill=c)
    im.save(out_path, quality=88)


def cmd_collect(args):
    load_env()
    st = json.load(open(STATE))
    client = anthropic.Anthropic()
    b = client.messages.batches.retrieve(st["batch_id"])
    if b.processing_status != "ended":
        sys.exit(f"Batch noch nicht fertig ({b.processing_status}). Später erneut 'collect' ausführen.")
    _, facades = load_facades()
    out = {}
    if os.path.exists(RESULTS):
        old = json.load(open(RESULTS))
        # nur Einträge im aktuellen Format übernehmen
        out = {k: v for k, v in old.items() if isinstance(v, dict) and "balkone" in v}
    os.makedirs(REVIEW_DIR, exist_ok=True)
    n_ok = n_err = tin = tout = 0
    cost = 0.0
    for r in client.messages.batches.results(st["batch_id"]):
        info = st["requests"].get(r.custom_id)
        if info is None:
            continue
        if r.result.type != "succeeded":
            err = getattr(r.result, "error", None)
            detail = err.error.message if err is not None and getattr(err, "error", None) else ""
            print(f"{r.custom_id}: {r.result.type} {detail}")
            n_err += 1
            continue
        msg = r.result.message
        f = facades[info["facade"]]
        res = parse_answer(msg)
        pin, pout = BATCH_PRICES.get(info["model"], (0, 0))
        c = msg.usage.input_tokens / 1e6 * pin + msg.usage.output_tokens / 1e6 * pout
        entry = {
            "model": info["model"],
            "bildqualitaet": res.get("bildqualitaet"),
            "geschosse": res.get("geschosse"),
            "anmerkung": res.get("anmerkung", ""),
            "balkone": [measure(bb, f, info) for bb in res.get("balkone", []) if len(bb.get("box", [])) == 4],
            "tokens": [msg.usage.input_tokens, msg.usage.output_tokens],
            "kosten_usd": round(c, 5),
            "batch": st["batch_id"],
            "datum": time.strftime("%Y-%m-%d"),
        }
        out[str(info["facade"])] = entry
        draw_review(entry, f, os.path.join(REVIEW_DIR, f"{r.custom_id}.jpg"))
        n_ok += 1
        tin += msg.usage.input_tokens
        tout += msg.usage.output_tokens
        cost += c
    json.dump(out, open(RESULTS, "w"), ensure_ascii=False, indent=1)
    with open(RESULTS_JS, "w", encoding="utf-8") as fh:
        fh.write("window.BALKONE = ")
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";\n")
    nb = sum(len(out[str(st["requests"][k]["facade"])]["balkone"])
             for k in st["requests"] if str(st["requests"][k]["facade"]) in out)
    print(f"{n_ok} Fassaden ausgewertet, {n_err} Fehler, {nb} Balkone/Loggien gemeldet")
    if n_ok:
        print(f"Tokens: {tin} rein / {tout} raus, Kosten {cost:.3f} $ "
              f"(Ø {cost / n_ok * 100:.2f} US-Cent pro Fassade)")
    print(f"Ergebnisse: {RESULTS}\nPrüfbilder: {REVIEW_DIR}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("--ids", help="Fassaden-IDs, kommagetrennt")
    s.add_argument("--classes", default="S", help="Himmelsrichtungen, z. B. S oder S,SO,SW")
    s.add_argument("--model", default="sonnet", choices=sorted(MODELS))
    s.add_argument("--min-ev", type=float, default=0.15, help="Mindest-Wandnachweis")
    sub.add_parser("status")
    sub.add_parser("collect")
    args = ap.parse_args()
    {"submit": cmd_submit, "status": cmd_status, "collect": cmd_collect}[args.cmd](args)


if __name__ == "__main__":
    main()
