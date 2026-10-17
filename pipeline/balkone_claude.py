#!/usr/bin/env python3
"""
Balkonerkennung mit der Claude API (Message Batches)
====================================================
Schickt die Frontalansichten der Fassaden (aus fassaden_pipeline.py) an Claude,
lässt Balkone/Loggien mit Bildkoordinaten, Typ und Geschoss melden und
sammelt die Ergebnisse samt Token-Verbrauch und Kosten ein.

Läuft in drei Schritten, weil Batches asynchron verarbeitet werden
(meist Minuten, maximal 24 Stunden):

  python3 pipeline/balkone_claude.py submit  --ids 733,624 --models sonnet,haiku,opus
  python3 pipeline/balkone_claude.py status
  python3 pipeline/balkone_claude.py collect

Ohne --ids werden alle Fassaden mit Ausrichtung SO/S/SW eingereicht.

Der API-Key wird aus der Umgebungsvariable ANTHROPIC_API_KEY oder aus einer
.env-Datei im Projektordner gelesen (.env niemals committen).

Abhängigkeiten: anthropic, pillow
"""
import argparse
import base64
import io
import json
import os
import sys
import time

from PIL import Image, ImageDraw, ImageFont

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
    "opus":   "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku":  "claude-haiku-5-5",
}
# US-$ pro 1 Mio. Tokens, Batch-Preise (50 % Rabatt). Stand Okt. 2026, bei Bedarf anpassen.
BATCH_PRICES = {
    "claude-opus-5-5":   (2.00, 10.00),
    "claude-sonnet-5-5": (1.00, 5.00),
    "claude-haiku-5-5":  (0.05, 0.25),
}
MAX_EDGE = 1568           # längste Bildkante, ab der die API selbst verkleinern würde
MAX_PIXELS = 1_150_000    # ca. Pixelgrenze, ab der die API selbst verkleinern würde
MAX_TOKENS = 4000

TOOL = {
    "name": "balkone_melden",
    "description": "Meldet alle auf der Fassade sichtbaren Balkone und Loggien.",
    "input_schema": {
        "type": "object",
        "properties": {
            "bildqualitaet": {
                "type": "string", "enum": ["gut", "mittel", "schlecht"],
                "description": "Wie gut lassen sich Balkone auf diesem Bild beurteilen?",
            },
            "fassadenbeschreibung": {
                "type": "string",
                "description": "Ein Satz: Gebäudetyp, Geschosszahl, Auffälligkeiten.",
            },
            "geschosse": {"type": "integer", "description": "Anzahl Vollgeschosse inkl. Erdgeschoss."},
            "balkone": {
                "type": "array",
                "description": "Ein Eintrag pro einzelnem Balkon bzw. einzelner Loggia (pro Wohnung und Geschoss), nicht ein Eintrag für einen ganzen Balkonstrang.",
                "items": {
                    "type": "object",
                    "properties": {
                        "box": {
                            "type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
                            "description": "[x0, y0, x1, y1] in Pixeln dieses Bildes, Ursprung oben links.",
                        },
                        "typ": {
                            "type": "string",
                            "enum": ["Balkon", "Loggia", "Französischer Balkon", "Dachterrasse", "unsicher"],
                        },
                        "geschoss": {"type": "integer", "description": "0 = Erdgeschoss, 1 = 1. OG usw."},
                        "sicherheit": {"type": "number", "description": "0 bis 1"},
                        "geeignet_fuer_bkw": {
                            "type": "boolean",
                            "description": "Brüstung/Geländer, an dem man ein Solarmodul (ca. 1,7 x 1,1 m) außen befestigen könnte.",
                        },
                    },
                    "required": ["box", "typ", "geschoss", "sicherheit", "geeignet_fuer_bkw"],
                },
            },
            "anmerkung": {"type": "string", "description": "Optional: Unsicherheiten, Verdeckungen, Artefakte."},
        },
        "required": ["bildqualitaet", "fassadenbeschreibung", "geschosse", "balkone"],
    },
}

SYSTEM = (
    "Du analysierst Fassadenbilder für eine Software, die Wohnungen mit gutem Standort für "
    "Balkonkraftwerke (steckerfertige Solaranlagen am Balkongeländer) findet.\n\n"
    "Die Bilder sind keine Fotos, sondern Frontalansichten aus einem photogrammetrischen "
    "3D-Stadtmodell (Luftbildbefliegung). Typische Eigenschaften: verwaschene Texturen, "
    "verzerrte Kanten, Lücken. Hellgraue Flächen ohne Struktur sind 'keine Daten' "
    "(z. B. dort, wo Bäume vor der Fassade herausgeschnitten wurden). Das Bild zeigt die "
    "Fassade von außen, unten ist das Erdgeschoss.\n\n"
    "Melde jeden einzelnen Balkon und jede einzelne Loggia als eigenen Eintrag "
    "(übereinanderliegende Balkone also einzeln, pro Geschoss). Fenster ohne Austritt, "
    "Erker ohne Austritt und reine Fassadenvorsprünge sind keine Balkone. Wenn du etwas "
    "nicht sicher erkennst, nimm es mit typ 'unsicher' und niedriger Sicherheit auf, statt "
    "es wegzulassen. Antworte ausschließlich über das Werkzeug balkone_melden."
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


def user_text(f, sent_size, orig_size):
    m_per_px = (f["len"] + 1.0) / orig_size[0] * orig_size[0] / sent_size[0]
    return (
        f"Fassade #{f['id']}, Ausrichtung {f['cls']} ({f['az']:.0f}°), "
        f"Breite ca. {f['len']:.1f} m, Höhe ca. {f['h']:.1f} m über Gelände. "
        f"Bildgröße {sent_size[0]} x {sent_size[1]} px, Maßstab ca. 1 px = {m_per_px:.3f} m "
        f"(ein Geschoss ist rund {3.0 / m_per_px:.0f} px hoch). "
        "Melde alle Balkone und Loggien."
    )


# ----------------------------------------------------------------------------
def cmd_submit(args):
    load_env()
    meta, facades = load_facades()
    if args.ids:
        ids = [int(x) for x in args.ids.split(",")]
    else:
        ids = [fid for fid, f in facades.items()
               if f.get("target") and f.get("img") and f["ev"] >= args.min_ev and f["len"] >= args.min_len]
    models = [MODELS[m.strip()] for m in args.models.split(",")]
    requests, info = [], {}
    for fid in ids:
        f = facades[fid]
        if not f.get("img"):
            print(f"Fassade {fid} hat kein Bild, übersprungen")
            continue
        b64, sent, orig = prepare_image(os.path.join(WEBAPP, f["img"]))
        for model in models:
            cid = f"{model.replace('claude-', '').replace('-', '')}_f{fid}"
            requests.append({
                "custom_id": cid,
                "params": {
                    "model": model,
                    "max_tokens": MAX_TOKENS,
                    "system": SYSTEM,
                    "tools": [TOOL],
                    # Opus/Sonnet 5.5 unterstützen kein erzwungenes Werkzeug -> "auto",
                    # die Systemanweisung verlangt die Antwort über das Werkzeug.
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
    client = anthropic.Anthropic()
    batch = client.messages.batches.create(requests=requests)
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump({"batch_id": batch.id, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "requests": info},
              open(STATE, "w"), indent=1)
    print(f"Batch {batch.id} eingereicht: {len(requests)} Anfragen ({len(ids)} Fassaden x {len(models)} Modelle)")


def cmd_status(args):
    load_env()
    st = json.load(open(STATE))
    b = anthropic.Anthropic().messages.batches.retrieve(st["batch_id"])
    c = b.request_counts
    print(f"Batch {b.id}: {b.processing_status} | in Arbeit {c.processing}, fertig {c.succeeded}, "
          f"Fehler {c.errored}, abgebrochen {c.canceled}, abgelaufen {c.expired}")
    return b.processing_status


def draw_review(entry, out_path):
    src = os.path.join(WEBAPP, entry["img"])
    im = Image.open(src).convert("RGB")
    sx = entry["orig"][0] / entry["sent"][0]
    sy = entry["orig"][1] / entry["sent"][1]
    d = ImageDraw.Draw(im)
    col = {"Balkon": (255, 40, 40), "Loggia": (255, 0, 200), "Französischer Balkon": (255, 160, 0),
           "Dachterrasse": (0, 180, 255), "unsicher": (150, 150, 150)}
    for b in entry["result"].get("balkone", []):
        x0, y0, x1, y1 = b["box"]
        c = col.get(b["typ"], (255, 255, 0))
        d.rectangle([x0 * sx, y0 * sy, x1 * sx, y1 * sy], outline=c, width=3)
        d.text((x0 * sx + 3, y0 * sy + 2), f"{b['geschoss']} {b['sicherheit']:.1f}", fill=c)
    im.save(out_path, quality=90)


def cmd_collect(args):
    load_env()
    st = json.load(open(STATE))
    client = anthropic.Anthropic()
    b = client.messages.batches.retrieve(st["batch_id"])
    if b.processing_status != "ended":
        sys.exit(f"Batch noch nicht fertig ({b.processing_status}). Später erneut 'collect' ausführen.")
    out = json.load(open(RESULTS)) if os.path.exists(RESULTS) else {}
    os.makedirs(REVIEW_DIR, exist_ok=True)
    totals = {}
    for r in client.messages.batches.results(st["batch_id"]):
        info = st["requests"].get(r.custom_id)
        if info is None:
            continue
        model = info["model"]
        if r.result.type != "succeeded":
            err = getattr(r.result, "error", None)
            detail = err.error.message if err is not None and getattr(err, "error", None) else ""
            print(f"{r.custom_id}: {r.result.type} {detail}")
            continue
        msg = r.result.message
        tool = next((c for c in msg.content if c.type == "tool_use"), None)
        if tool:
            res = tool.input
        else:
            # Rückfall: JSON aus Textantwort lesen
            text = "".join(c.text for c in msg.content if c.type == "text")
            try:
                res = json.loads(text[text.index("{"):text.rindex("}") + 1])
            except ValueError:
                res = {"balkone": [], "anmerkung": "keine auswertbare Antwort: " + text[:300]}
        pin, pout = BATCH_PRICES.get(model, (0, 0))
        cost = msg.usage.input_tokens / 1e6 * pin + msg.usage.output_tokens / 1e6 * pout
        entry = dict(info, result=res, usage={"in": msg.usage.input_tokens, "out": msg.usage.output_tokens},
                     cost_usd=round(cost, 6), batch=st["batch_id"])
        out.setdefault(str(info["facade"]), {})[model] = entry
        t = totals.setdefault(model, {"n": 0, "in": 0, "out": 0, "cost": 0.0})
        t["n"] += 1; t["in"] += msg.usage.input_tokens; t["out"] += msg.usage.output_tokens; t["cost"] += cost
        draw_review(entry, os.path.join(REVIEW_DIR, f"{r.custom_id}.jpg"))
        n = len(res.get("balkone", []))
        print(f"{r.custom_id}: {n} Balkone, Qualität {res.get('bildqualitaet')}, "
              f"{msg.usage.input_tokens} in / {msg.usage.output_tokens} out, {cost * 100:.3f} US-Cent")
    json.dump(out, open(RESULTS, "w"), ensure_ascii=False, indent=1)
    with open(RESULTS_JS, "w", encoding="utf-8") as f:
        f.write("window.BALKONE = ")
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    print("\nKosten pro Fassade (Batch-Preise):")
    for model, t in totals.items():
        per = t["cost"] / t["n"]
        print(f"  {model:20s} Ø {t['in'] / t['n']:.0f} in / {t['out'] / t['n']:.0f} out Tokens  ->  "
              f"{per * 100:.3f} US-Cent pro Fassade, {per * 1000:.2f} $ pro 1.000 Fassaden")
    print(f"\nErgebnisse: {RESULTS}\nPrüfbilder: {REVIEW_DIR}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("--ids", help="Fassaden-IDs, kommagetrennt (Standard: alle SO/S/SW)")
    s.add_argument("--models", default="sonnet", help="opus, sonnet, haiku (kommagetrennt)")
    s.add_argument("--min-ev", type=float, default=0.3, help="Mindest-Wandnachweis ohne --ids")
    s.add_argument("--min-len", type=float, default=5.0, help="Mindestlänge in m ohne --ids")
    sub.add_parser("status")
    sub.add_parser("collect")
    args = ap.parse_args()
    {"submit": cmd_submit, "status": cmd_status, "collect": cmd_collect}[args.cmd](args)


if __name__ == "__main__":
    main()
