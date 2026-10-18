#!/usr/bin/env python3
"""Prüft für jede Angebotsseite, ob die Module auf einer gezeichneten Balkonplatte sitzen."""
import json, math, os, sys

web = sys.argv[1] if len(sys.argv) > 1 else "webapp"
adir = os.path.join(web, "angebot")
fehler = 0
n = 0
for name in sorted(os.listdir(adir)):
    p = os.path.join(adir, name, "daten.json")
    if not os.path.exists(p):
        continue
    d = json.load(open(p, encoding="utf-8"))
    m = json.load(open(os.path.join(adir, name, "modell.json"), encoding="utf-8"))
    th = m["drehung"]
    aktiv = [b for b in m["balkone"] if b.get("aktiv")]
    n += 1
    if not aktiv:
        print(f"{name}: keine aktive Balkonplatte"); fehler += 1; continue
    b = aktiv[0]
    for v in d.get("varianten") or [{"module": d["module"]}]:
        for q in v["module"]:
            for x, y, z in q:
                dx, dz = x - b["c"][0], z - b["c"][2]
                u = dx * math.cos(th) - dz * math.sin(th)
                w = dx * math.sin(th) + dz * math.cos(th)
                ok_u = abs(u) <= b["breite"] / 2 + 0.05
                ok_w = abs(w) <= b["tiefe"] / 2 + 0.2
                ok_y = b["c"][1] - 0.1 <= y <= b["c"][1] + 2.0
                if not (ok_u and ok_w and ok_y):
                    print(f"{name}: Modulecke außerhalb (u={u:.2f}/{b['breite']/2:.2f}, w={w:.2f}/{b['tiefe']/2:.2f}, "
                          f"y={y:.2f} Platte {b['c'][1]:.2f})")
                    fehler += 1
                    break
print(f"{n} Angebote geprüft, {fehler} Probleme")
