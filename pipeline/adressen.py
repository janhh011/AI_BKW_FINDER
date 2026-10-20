#!/usr/bin/env python3
"""
Adressen für die Adresssuche der Webseite
=========================================
Lädt alle Hausadressen im Gebiet der Kachel und schreibt webapp/adressen.json
(Straße, Hausnummer, PLZ, Koordinate in EPSG:25833). Die Suche läuft danach komplett im Browser,
ohne externen Geocoding-Dienst (gut für Datenschutz und Tempo).

Quellen:
  berlin (Standard)  Amtliche Hauskoordinaten, Geoportal Berlin (WFS „Adressen Berlin“),
                     Lizenz Datenlizenz Deutschland – Zero 2.0 (kommerziell frei nutzbar, keine Namensnennung nötig)
  osm                OpenStreetMap über Overpass (ODbL: Namensnennung „© OpenStreetMap-Mitwirkende“ nötig)

Nutzung:
  python3 pipeline/adressen.py webapp
  python3 pipeline/adressen.py webapp --quelle osm

Hinweis: braucht Internet (gdi.berlin.de bzw. overpass-api.de); im eigenen Terminal ausführen.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

WFS = "https://gdi.berlin.de/services/wfs/adressen_berlin"
OVERPASS = "https://overpass-api.de/api/interpreter"
RAND = 30.0   # m über die Kachel hinaus


def bounds(webapp):
    s = open(os.path.join(webapp, "data.js"), encoding="utf-8").read()
    meta = json.loads(s[s.index("=") + 1:].strip().rstrip(";"))["meta"]
    return meta["bounds"], meta["crs"]


def holen(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "balki-strom-adressen/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    except urllib.error.URLError as e:
        # Python von python.org auf dem Mac kennt die Systemzertifikate oft nicht -> curl nutzt den Schlüsselbund
        if "CERTIFICATE_VERIFY_FAILED" not in str(e):
            raise
        import subprocess
        cmd = ["curl", "-sS", "--fail", "--max-time", "120", "-A", "balki-strom-adressen/1.0", url]
        if data is not None:
            cmd += ["--data-binary", "@-"]
        return subprocess.run(cmd, input=data, capture_output=True, check=True).stdout


def berlin(b, crs):
    cap = holen(WFS + "?SERVICE=WFS&REQUEST=GetCapabilities").decode("utf-8", "replace")
    namen = re.findall(r"<(?:wfs:)?Name>([^<]+)</(?:wfs:)?Name>", cap)
    typ = next((n for n in namen if "adress" in n.lower()), None)
    if not typ:
        sys.exit(f"Kein Adress-Layer im WFS gefunden. Vorhanden: {namen}")
    q = dict(SERVICE="WFS", VERSION="2.0.0", REQUEST="GetFeature", TYPENAMES=typ,
             SRSNAME="EPSG:25833", OUTPUTFORMAT="application/json",
             BBOX=f"{b[0] - RAND},{b[1] - RAND},{b[2] + RAND},{b[3] + RAND},urn:ogc:def:crs:EPSG::25833")
    fc = json.loads(holen(WFS + "?" + urllib.parse.urlencode(q)))
    out, schluessel = [], None
    for ft in fc.get("features", []):
        p = {k.lower(): v for k, v in (ft.get("properties") or {}).items()}
        schluessel = schluessel or sorted(p)
        strasse = next((p[k] for k in ("str_name", "strname", "strasse", "straßenname", "stn", "street") if p.get(k)), None)
        hnr = next((str(p[k]) for k in ("hnr", "hausnummer", "hsnr", "housenumber") if p.get(k) not in (None, "")), None)
        zus = next((str(p[k]) for k in ("hnr_zusatz", "hausnummerzusatz", "adz", "zusatz") if p.get(k)), "")
        plz = next((str(p[k]) for k in ("plz", "postleitzahl", "postcode") if p.get(k)), "")
        g = ft.get("geometry") or {}
        if not strasse or not hnr or g.get("type") != "Point":
            continue
        x, y = g["coordinates"][:2]
        out.append(dict(s=strasse, n=f"{hnr}{zus}", p=plz, x=round(x, 1), y=round(y, 1)))
    if not out:
        sys.exit(f"Keine Adressen erkannt. Felder im Datensatz: {schluessel}")
    return out, "Amtliche Hauskoordinaten Berlin (Geoportal Berlin, dl-de/zero-2-0)"


def osm(b, crs):
    from pyproj import Transformer
    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    back = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    lon0, lat0 = tr.transform(b[0] - RAND, b[1] - RAND)
    lon1, lat1 = tr.transform(b[2] + RAND, b[3] + RAND)
    q = (f'[out:json][timeout:60];(node["addr:housenumber"]({lat0},{lon0},{lat1},{lon1});'
         f'way["addr:housenumber"]({lat0},{lon0},{lat1},{lon1}););out center tags;')
    res = json.loads(holen(OVERPASS, urllib.parse.urlencode({"data": q}).encode()))
    out = []
    for e in res.get("elements", []):
        t = e.get("tags", {})
        lat, lon = (e["lat"], e["lon"]) if "lat" in e else (e["center"]["lat"], e["center"]["lon"])
        if not t.get("addr:street"):
            continue
        x, y = back.transform(lon, lat)
        out.append(dict(s=t["addr:street"], n=t["addr:housenumber"], p=t.get("addr:postcode", ""),
                        x=round(x, 1), y=round(y, 1)))
    return out, "© OpenStreetMap-Mitwirkende (ODbL)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("webapp")
    ap.add_argument("--quelle", choices=["berlin", "osm"], default="berlin")
    a = ap.parse_args()
    b, crs = bounds(a.webapp)
    adr, lizenz = (berlin if a.quelle == "berlin" else osm)(b, crs)
    adr = sorted({(d["s"], d["n"]): d for d in adr}.values(), key=lambda d: (d["s"], int(re.match(r"\d*", d["n"]).group() or 0), d["n"]))
    with open(os.path.join(a.webapp, "adressen.json"), "w", encoding="utf-8") as fh:
        json.dump(dict(quelle=lizenz, crs="EPSG:25833", adressen=adr), fh, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(adr)} Adressen -> {os.path.join(a.webapp, 'adressen.json')} ({lizenz})")


if __name__ == "__main__":
    main()
