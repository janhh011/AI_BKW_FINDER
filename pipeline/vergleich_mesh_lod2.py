#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vergleich: Ertrag aus der Mesh-Version (webapp/angebot/*/daten.json) gegen die LoD2-Version
==========================================================================================
Für jedes vorhandene Mesh-Angebot wird dieselbe Stelle (Lage, Höhe, Breite, Tiefe des Balkons)
auf die passende LoD2-Hauswand übertragen und neu gerechnet. Ergebnis: Tabelle (CSV) und Kennzahlen.

Nutzung:
  python3 pipeline/vergleich_mesh_lod2.py webapp webapp_lod2 [--csv vergleich.csv]
"""
import argparse
import csv
import glob
import json
import math
import os
import sys

import numpy as np
from pyproj import Transformer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import angebot as A  # noqa: E402


def lade_data(web):
    txt = open(os.path.join(web, "data.js"), encoding="utf-8").read()
    return json.loads(txt[txt.index("=") + 1:].strip().rstrip(";"))


def passende_wand(facades, p, n_mesh, tiefe):
    """LoD2-Wand mit ähnlicher Richtung, deren Fläche hinter dem Modul liegt."""
    best, bd = None, 1e9
    for f in facades:
        n = np.array(f["n"])
        if n @ n_mesh < math.cos(math.radians(30)):
            continue
        a, b = np.array(f["a"]), np.array(f["b"])
        t = (b - a) / max(np.linalg.norm(b - a), 1e-6)
        u = (p - a) @ t
        if u < -0.5 or u > np.linalg.norm(b - a) + 0.5:
            continue
        o = (p - a) @ n                       # Abstand vor der Wand
        d = abs(o - tiefe)
        if -1.0 < o < tiefe + 3.0 and d < bd:
            best, bd = f, d
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mesh_web")
    ap.add_argument("lod2_web")
    ap.add_argument("--csv", default="vergleich_mesh_lod2.csv")
    a = ap.parse_args()

    Dm = lade_data(a.mesh_web)
    fm = {f["id"]: f for f in Dm["facades"]}
    R = A.Rechner(None, a.lod2_web)
    to_grid = Transformer.from_crs("EPSG:4326", R.meta["crs"], always_xy=True)
    zeilen = []
    for p in sorted(glob.glob(os.path.join(a.mesh_web, "angebot", "*", "daten.json"))):
        d = json.load(open(p, encoding="utf-8"))
        if "_u" in d["id"] or "_b" in d["id"] or d.get("ohne_baeume"):
            continue
        f0 = fm.get(d["fassade"])
        if f0 is None:
            continue
        x, y = to_grid.transform(d["lon"], d["lat"])
        n_mesh = np.array(f0["n"], float)
        f = passende_wand(R.fac.values(), np.array([x, y]), n_mesh, d["tiefe_m"])
        if f is None:
            zeilen.append(dict(id=d["id"], status="keine LoD2-Wand"))
            continue
        # Balkon-Box im LoD2-Fassadenbild: gleiche Mitte, Breite, Unterkante (absolut) wie im Mesh
        res = f["img_res"]
        t = np.array(f["img_t"], float)
        u = (np.array([x, y]) - np.array(f["img_left"], float)) @ t
        z_unten = f["ground"] + (d.get("unterkante_m") or 0)      # relativ zum Gelände (Höhensysteme können abweichen)
        z_oben = z_unten + d["hoehe_m"]
        box = [(u - d["breite_m"] / 2) / res, (f["img_ztop"] - z_oben) / res,
               (u + d["breite_m"] / 2) / res, (f["img_ztop"] - z_unten) / res]
        if f.get("frei_ab") is not None and z_unten < f["frei_ab"] - 0.5:
            zeilen.append(dict(id=d["id"], status="unter Nachbardach (LoD2-Wand passt nicht)"))
            continue
        b = A.balkon_aus_eingabe(f, box, geschoss=d.get("geschoss"))
        b["typ"] = d["typ"]
        b["tiefe_m"] = d["tiefe_m"]
        r = R.simuliere(f["id"], b)
        if r is None:
            zeilen.append(dict(id=d["id"], status="passt nicht"))
            continue
        geo, erg, az, rand = r
        kwh_l = float(erg["pac"].sum()) / 1000
        frei_l = float(erg["pac_frei"].sum()) / 1000
        # Mesh-Angebot hat ggf. andere Modulzahl -> auf kWh je kWp vergleichen
        wp_m = d["vorschlag"]["wp"]
        wp_l = geo["vorschlag"]["wp"]
        zeilen.append(dict(
            id=d["id"], status="ok", richtung=f0["cls"], geschoss=d.get("geschoss"),
            wand_lod2=f["id"], abstand_hoehe=round(f["ground"] - f0["ground"], 2),
            kwh_kwp_mesh=round(d["jahr"]["kwh"] * 1000 / wp_m), kwh_kwp_lod2=round(kwh_l * 1000 / wp_l),
            verlust_mesh=round(1 - d["jahr"]["kwh"] / d["jahr"]["kwh_frei"], 3),
            verlust_lod2=round(1 - kwh_l / frei_l, 3),
            sonne_mesh=d["jahr"]["sonnenstunden"], sonne_lod2=round(erg["sonne_mod"]),
            himmel_mesh=d["jahr"]["himmel"], himmel_lod2=round(erg["himmel"], 2)))
    ok = [z for z in zeilen if z["status"] == "ok"]
    felder = sorted({k for z in zeilen for k in z}, key=lambda k: list(ok[0]).index(k) if ok and k in ok[0] else 99)
    with open(a.csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=felder)
        w.writeheader()
        w.writerows(zeilen)
    print(f"{len(zeilen)} Mesh-Angebote, {len(ok)} übertragen")
    by = {}
    for z in ok:
        by.setdefault(z["geschoss"], []).append((z["kwh_kwp_lod2"] - z["kwh_kwp_mesh"]) / z["kwh_kwp_mesh"])
    for g in sorted(by, key=lambda g: (g is None, g)):
        print(f"  Geschoss {g}: {len(by[g])} Balkone, Abweichung Median {np.median(by[g]) * 100:+.0f} %")
    if ok:
        km = np.array([z["kwh_kwp_mesh"] for z in ok], float)
        kl = np.array([z["kwh_kwp_lod2"] for z in ok], float)
        vm = np.array([z["verlust_mesh"] for z in ok])
        vl = np.array([z["verlust_lod2"] for z in ok])
        rel = (kl - km) / km
        print(f"Ertrag kWh/kWp   Mesh Mittel {km.mean():.0f}   LoD2 Mittel {kl.mean():.0f}")
        print(f"Abweichung LoD2 vs. Mesh: Median {np.median(rel) * 100:+.1f} %, "
              f"mittlerer Betrag {np.mean(np.abs(rel)) * 100:.1f} %, "
              f"innerhalb ±10 %: {np.mean(np.abs(rel) <= 0.10) * 100:.0f} %, innerhalb ±20 %: {np.mean(np.abs(rel) <= 0.20) * 100:.0f} %")
        print(f"Schattenverlust  Mesh Mittel {vm.mean() * 100:.0f} %   LoD2 Mittel {vl.mean() * 100:.0f} %")
        print(f"Rangkorrelation (Spearman): {np.corrcoef(np.argsort(np.argsort(km)), np.argsort(np.argsort(kl)))[0, 1]:.2f}")
        for z in sorted(ok, key=lambda z: abs(z["kwh_kwp_lod2"] - z["kwh_kwp_mesh"]), reverse=True)[:6]:
            print("  größte Abweichung:", z)


if __name__ == "__main__":
    main()
