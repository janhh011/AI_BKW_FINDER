#!/usr/bin/env python3
"""
Geodaten aus einem ATOM-Feed (INSPIRE-Downloaddienst) herunterladen
===================================================================
Die Berliner Geodaten (LoD2, Luftbilder, Laserscan) werden als ATOM-Feed angeboten.
Ein ATOM-Feed ist eine XML-Liste mit Download-Links, technisch ähnlich wie ein RSS-Feed.
Oft gibt es zwei Ebenen: ein Service-Feed verweist auf Datensatz-Feeds, und diese enthalten
die eigentlichen Dateien (meist ZIP-Archive, je Kachel eins).

Nutzung:
  # 1. Nur anzeigen, was der Feed anbietet
  python3 pipeline/atom_download.py "<URL des ATOM-Feeds>"

  # 2. Dateien für ein Gebiet laden (Koordinaten in EPSG:25833, hier unsere Testkachel)
  python3 pipeline/atom_download.py "<URL>" --bbox 385848,5817143,386226,5817517 --ziel downloads/lod2

  # 3. Dateien nach Namen filtern (z. B. Kachelnummer), ohne Gebietsangabe
  python3 pipeline/atom_download.py "<URL>" --enthaelt 386_5817 --ziel downloads/lod2

Ohne --ziel wird nur aufgelistet, nichts geladen. Bereits vorhandene Dateien werden übersprungen.
"""
import argparse
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

NS = {"a": "http://www.w3.org/2005/Atom", "georss": "http://www.georss.org/georss"}
UA = {"User-Agent": "balki-strom-atom/1.0"}


def lade(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
        return r.read()


def box_aus_eintrag(e):
    """georss:box ('lat1 lon1 lat2 lon2' oder Projektionskoordinaten) bzw. georss:polygon -> (xmin, ymin, xmax, ymax)."""
    b = e.find("georss:box", NS)
    if b is not None and b.text:
        v = [float(x) for x in b.text.split()]
        return min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3])
    p = e.find("georss:polygon", NS)
    if p is not None and p.text:
        v = [float(x) for x in p.text.split()]
        a, b2 = v[0::2], v[1::2]
        return min(a), min(b2), max(a), max(b2)
    return None


def ueberlappt(box, bbox, transformer):
    """bbox in EPSG:25833; box aus dem Feed meist in WGS84 (lat lon). Bei Zweifel: True."""
    if box is None:
        return True
    a0, a1, a2, a3 = box
    if max(abs(a0), abs(a2)) <= 90 and max(abs(a1), abs(a3)) <= 180:      # WGS84 (lat, lon)
        if transformer is None:
            return True
        x0, y0 = transformer.transform(a1, a0)
        x1, y1 = transformer.transform(a3, a2)
        a0, a1, a2, a3 = min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
    return not (a2 < bbox[0] or a0 > bbox[2] or a3 < bbox[1] or a1 > bbox[3])


def eintraege(url, tiefe=0, gesehen=None):
    """Liefert (titel, download_url, box) für alle Dateien; folgt verschachtelten Feeds."""
    gesehen = gesehen if gesehen is not None else set()
    if url in gesehen or tiefe > 3:
        return
    gesehen.add(url)
    root = ET.fromstring(lade(url))
    for e in root.findall("a:entry", NS):
        titel = (e.findtext("a:title", default="", namespaces=NS) or "").strip()
        box = box_aus_eintrag(e)
        for l in e.findall("a:link", NS):
            href, typ, rel = l.get("href"), (l.get("type") or ""), (l.get("rel") or "")
            if not href:
                continue
            href = urllib.parse.urljoin(url, href)
            if "atom+xml" in typ or (rel == "alternate" and href.endswith((".xml", "atom"))):
                yield from eintraege(href, tiefe + 1, gesehen)
            elif rel in ("", "alternate", "section", "enclosure") and not typ.startswith("text/html"):
                yield titel, href, box


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("feed")
    ap.add_argument("--bbox", help="xmin,ymin,xmax,ymax in EPSG:25833")
    ap.add_argument("--enthaelt", help="nur Links, deren Name diesen Text enthält")
    ap.add_argument("--ziel", help="Ordner zum Speichern; ohne Angabe nur auflisten")
    a = ap.parse_args()

    bbox = [float(v) for v in a.bbox.split(",")] if a.bbox else None
    tr = None
    if bbox:
        try:
            from pyproj import Transformer
            tr = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
        except ImportError:
            print("Hinweis: pyproj fehlt – Gebietsfilter nur für Feeds mit Projektionskoordinaten.")

    treffer = []
    for titel, href, box in eintraege(a.feed):
        if a.enthaelt and a.enthaelt not in href and a.enthaelt not in titel:
            continue
        if bbox and not ueberlappt(box, bbox, tr):
            continue
        treffer.append((titel, href))
    if not treffer:
        sys.exit("Keine passenden Dateien gefunden. Ohne Filter aufrufen, um zu sehen, was der Feed enthält.")

    for titel, href in treffer:
        print(f"{titel[:60]:60}  {href}")
    print(f"\n{len(treffer)} Dateien")
    if not a.ziel:
        return
    os.makedirs(a.ziel, exist_ok=True)
    for i, (titel, href) in enumerate(treffer, 1):
        name = os.path.basename(urllib.parse.urlparse(href).path) or re.sub(r"\W+", "_", titel)
        pfad = os.path.join(a.ziel, name)
        if os.path.exists(pfad):
            continue
        print(f"[{i}/{len(treffer)}] {name}")
        with urllib.request.urlopen(urllib.request.Request(href, headers=UA), timeout=600) as r, open(pfad + ".teil", "wb") as fh:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
        os.replace(pfad + ".teil", pfad)
    print("Fertig.")


if __name__ == "__main__":
    main()
