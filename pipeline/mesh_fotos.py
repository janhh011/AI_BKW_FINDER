#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fotoansichten aus dem Mesh für die LoD2-Hauswände
=================================================
Die Hauswände kommen aus dem LoD2-Modell (genaue Lage, Außenseite eindeutig). Wo eine Mesh-Kachel
vorliegt, wird für jede Wand eine echte Frontalansicht aus dem Mesh gerendert und ersetzt die
schematische Ansicht:
  - nur Geometrie in einem schmalen Streifen um die Wand (0,8 m dahinter bis 3 m davor, für Balkone)
  - Rückseiten werden verworfen (Dreiecke, die vom Betrachter weg zeigen) – so erscheint nie die
    Innen- oder Rückseite eines Hauses
  - Wände, die das Mesh nur teilweise abdeckt (Kachelrand), behalten die schematische Ansicht

Zusätzlich wird das Mesh als _cache/mesh.npz abgelegt; angebot.py nutzt es dann für die Balkontiefe
und die Foto-Szene.

Nutzung:
  python3 pipeline/mesh_fotos.py data/3858_58171_-002 webapp_lod2
"""
import json
import math
import os
import shutil
import sys
import time

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fassaden_pipeline import find_tile_files, load_mesh, face_normals, rasterize, shade  # noqa: E402

Image.MAX_IMAGE_PIXELS = None
VORNE = 3.0          # m vor der Wand mitrendern (Balkone, Geländer)
HINTEN = 0.8         # m hinter der Wandlinie (Loggien, Fensterlaibungen)
MAX_W = 1600         # px
MIN_ABDECKUNG = 0.6  # Anteil der freien Wandfläche, der im Mesh vorhanden sein muss
HG = (236, 236, 236)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def main(tile_dir, web):
    t0 = time.time()
    txt = open(os.path.join(web, "data.js"), encoding="utf-8").read()
    D = json.loads(txt[txt.index("=") + 1:].strip().rstrip(";"))
    obj = find_tile_files(tile_dir)
    cache = os.path.join(web, "_cache", "mesh.npz")
    V, F, VT, FT, mat, tex_files = load_mesh(obj, cache)
    log("Texturen laden")
    tex = [np.asarray(Image.open(p).convert("RGB")) for p in tex_files]
    nrm3, _ = face_normals(V, F)
    cen = V[F].mean(1)
    xmin, ymin = V[:, 0].min() + 1, V[:, 1].min() + 1
    xmax, ymax = V[:, 0].max() - 1, V[:, 1].max() - 1

    # schematische Ansichten sichern, damit ein erneuter Lauf darauf zurückfallen kann
    n_foto = n_teil = 0
    for f in D["facades"]:
        a, b = np.array(f["a"]), np.array(f["b"])
        if f.get("img_schema"):
            f["img"], f["img_quelle"] = f["img_schema"], "schema"
        if not all(xmin <= p[0] <= xmax and ymin <= p[1] <= ymax for p in (a, b)):
            continue
        n = np.array(f["n"], float)
        t = np.array(f["img_t"], float)
        L = f["len"]
        res = max(0.05, (L + 1.0) / MAX_W)
        ztop = f["top"] + 0.5
        zlo = f["ground"] - 0.5
        u_c = (cen[:, :2] - a) @ t
        o_c = (cen[:, :2] - a) @ n
        nh = nrm3[:, :2] @ n
        sel = (u_c > -0.5) & (u_c < L + 0.5) & (o_c > -HINTEN) & (o_c < VORNE) \
            & (cen[:, 2] > zlo) & (cen[:, 2] < ztop + 1.0) & (nh > -0.25)     # keine Rückseiten
        if sel.sum() < 50:
            continue
        left = np.array(f["img_left"], float)
        W = int(math.ceil((L + 1.0) / res))
        H = int(math.ceil((ztop - f["ground"] + 1.0) / res))
        px = ((V[:, :2] - left) @ t) / res
        py = (ztop - V[:, 2]) / res
        depth = -((V[:, :2] - a) @ n)
        zb, tid, b0, b1 = rasterize(px, py, depth, F, sel, W, H)
        # Abdeckung im freien Teil der Wand (über Nachbardach, unter Traufe)
        y0 = int((ztop - f["top"]) / res)
        y1 = int((ztop - f.get("frei_ab", f["ground"])) / res)
        x0, x1 = int(0.5 / res), int((L + 0.5) / res)
        teil = tid[max(0, y0):max(y0 + 1, y1), x0:max(x0 + 1, x1)]
        if teil.size == 0 or (teil >= 0).mean() < MIN_ABDECKUNG:
            n_teil += 1
            continue
        img = shade(tid, b0, b1, F, FT, VT, mat, tex, bg=HG)
        name = f"f{f['id']:04d}_foto.jpg"
        Image.fromarray(img).save(os.path.join(web, "fassaden", name), quality=82)
        f["img_schema"] = f.get("img_schema") or f["img"]
        f.update(img=f"fassaden/{name}", img_quelle="mesh", img_res=round(res, 5), img_ztop=round(ztop, 2))
        n_foto += 1
        if n_foto % 50 == 0:
            log(f"  {n_foto} Fotoansichten")
    D["meta"]["source"] = D["meta"]["source"].split(" + Mesh")[0] + " + Mesh Berlin 3D (Fassadenfotos)"
    D["meta"]["mesh_tile"] = os.path.basename(os.path.abspath(tile_dir))
    with open(os.path.join(web, "data.js"), "w", encoding="utf-8") as fh:
        fh.write("window.FASSADEN = ")
        json.dump(D, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";\n")
    for alt in ("baum_dreiecke.npy",):
        p = os.path.join(web, "_cache", alt)
        if os.path.exists(p):
            os.replace(p, p + ".alt")
    log(f"{n_foto} Hauswände mit Fotoansicht, {n_teil} nur teilweise im Mesh (bleiben schematisch); "
        f"{time.time() - t0:.0f} s")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
