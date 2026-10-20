#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hauswände, Punktwolke und Luftbild aus offenen Daten (statt Mesh)
=================================================================
Quellen (alle Datenlizenz Deutschland – Zero 2.0):
  LoD2-Gebäude (CityGML, 1-km-Kacheln)      -> Gebäude, Hauswände, Schatten durch Gebäude
  bDOM (bildbasiertes Oberflächenmodell 1 m) -> Bäume (Oberfläche minus Gelände, außerhalb der Gebäude)
  DOP20 2025 (Luftbild 20 cm, JPEG 2000)     -> Karte und Dachtextur

Ergebnis in <webapp>, kompatibel mit index.html und angebot.py:
  data.js            Hauswände (FASSADEN) + Metadaten
  luftbild.jpg       Luftbild des Gebiets
  fassaden/fXXXX.jpg schematische Frontansicht je Hauswand (Umriss + Geschosse)
  balkone.json       leer (Balkone kommen per Einzeichnen oder, wo es Fotos gibt, per Analyse)
  _cache/punkte.npz  Punktwolke für die Schattenrechnung (Gebäude, Bäume, Gelände)

Nutzung:
  python3 pipeline/lod2_pipeline.py data/downloads webapp_lod2 --bbox 385500,5816800,386500,5817800
"""
import argparse
import glob
import io
import json
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict

import cv2
import numpy as np
from PIL import Image, ImageDraw
from pyproj import Transformer
from scipy.ndimage import minimum_filter, uniform_filter, binary_dilation

Image.MAX_IMAGE_PIXELS = None
EPSG = "EPSG:25833"
SECTORS = ["N", "NO", "O", "SO", "S", "SW", "W", "NW"]
TARGET = ("SO", "S", "SW")
RAND = 250.0          # m Umgebung für Schatten (Horizont reicht bis 220 m)
PUNKT = 0.8           # m Abstand der Gebäudepunkte
ORTHO_RES = 0.25      # m/Pixel Luftbild für die Karte
IMG_RES = 0.05        # m/Pixel schematische Fassadenansicht
MIN_LEN, MIN_H = 2.5, 2.5

NS = {"gml": "http://www.opengis.net/gml", "bldg": "http://www.opengis.net/citygml/building/1.0",
      "core": "http://www.opengis.net/citygml/1.0"}
G = "{http://www.opengis.net/gml}"
B = "{http://www.opengis.net/citygml/building/1.0}"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------ LoD2 lesen
def lod2_kacheln(ordner, box):
    x0, y0, x1, y1 = box
    out = []
    for e in range(int(x0 // 1000), int(x1 // 1000) + 1):
        for n in range(int(y0 // 1000), int(y1 // 1000) + 1):
            p = os.path.join(ordner, f"LoD2_{e}_{n}.zip")
            if os.path.exists(p):
                out.append(p)
            else:
                log(f"  LoD2-Kachel fehlt: {os.path.basename(p)}")
    return out


def poly_punkte(poly):
    pl = poly.find(f"{G}exterior/{G}LinearRing/{G}posList")
    if pl is None or not pl.text:
        return None
    v = np.array(pl.text.split(), float)
    dim = int(pl.get("srsDimension", 3))
    v = v.reshape(-1, dim)[:, :3]
    if len(v) > 3 and np.allclose(v[0], v[-1]):
        v = v[:-1]
    return v if len(v) >= 3 else None


def lies_lod2(dateien, box):
    """-> {gebaeude_id: {"wand": [poly], "dach": [poly], "boden": [poly]}} im Gebiet (doppelte IDs nur einmal)."""
    x0, y0, x1, y1 = box
    geb = {}
    for zp in dateien:
        with zipfile.ZipFile(zp) as z:
            name = next(n for n in z.namelist() if n.lower().endswith((".xml", ".gml")))
            with z.open(name) as fh:
                for ev, el in ET.iterparse(fh, events=("end",)):
                    if el.tag != "{http://www.opengis.net/citygml/1.0}cityObjectMember":
                        continue
                    b = el.find(f"{B}Building")
                    if b is None:
                        el.clear()
                        continue
                    gid = b.get(f"{G}id")
                    if gid in geb:
                        el.clear()
                        continue
                    d = {"wand": [], "dach": [], "boden": []}
                    for art, tag in (("wand", "WallSurface"), ("dach", "RoofSurface"), ("boden", "GroundSurface")):
                        for s in b.iter(f"{B}{tag}"):
                            for poly in s.iter(f"{G}Polygon"):
                                v = poly_punkte(poly)
                                if v is not None:
                                    d[art].append(v)
                    alle = [p for k in d.values() for p in k]
                    if alle:
                        a = np.vstack(alle)
                        if a[:, 0].max() >= x0 and a[:, 0].min() <= x1 and a[:, 1].max() >= y0 and a[:, 1].min() <= y1:
                            geb[gid] = d
                    el.clear()
    return geb


# ------------------------------------------------------------------ Geometrie-Hilfen
def newell(v):
    n = np.zeros(3)
    for i in range(len(v)):
        a, b = v[i], v[(i + 1) % len(v)]
        n += [(a[1] - b[1]) * (a[2] + b[2]), (a[2] - b[2]) * (a[0] + b[0]), (a[0] - b[0]) * (a[1] + b[1])]
    L = np.linalg.norm(n)
    return n / L if L > 0 else n


def im_polygon(px, py, poly):
    """Punkt in 2D-Polygon (Strahlverfahren)."""
    inn = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-12) + xi:
            inn = not inn
        j = i
    return inn


def flaeche_abtasten(v, schritt):
    """Gleichmäßige Punkte auf einem ebenen 3D-Polygon."""
    n = newell(v)
    if not n.any():
        return v
    a = v[1] - v[0]
    a = a - n * (a @ n)
    if np.linalg.norm(a) < 1e-6:
        return v
    e1 = a / np.linalg.norm(a)
    e2 = np.cross(n, e1)
    uv = np.c_[(v - v[0]) @ e1, (v - v[0]) @ e2]
    lo, hi = uv.min(0), uv.max(0)
    w, h = int((hi[0] - lo[0]) / schritt) + 2, int((hi[1] - lo[1]) / schritt) + 2
    if w * h > 4_000_000:
        return v
    m = np.zeros((h, w), np.uint8)
    pts = np.round((uv - lo) / schritt).astype(np.int32)
    cv2.fillPoly(m, [pts], 1)
    iy, ix = np.nonzero(m)
    p = v[0] + np.outer(ix * schritt + lo[0], e1) + np.outer(iy * schritt + lo[1], e2)
    return np.vstack([p, v])


# ------------------------------------------------------------------ Höhenraster (bDOM)
def lies_bdom(ordner, box):
    """bDOM-Kacheln (xyz-Text, 1 m) -> Raster dom[iy, ix] mit Ursprung (x0, y1) oben links, NaN = keine Daten."""
    x0, y0, x1, y1 = box
    W, H = int(x1 - x0), int(y1 - y0)
    dom = np.full((H, W), np.nan, np.float32)
    for e in range(int(x0 // 2000) * 2, int(x1 // 1000) + 1, 2):
        for n in range(int(y0 // 2000) * 2, int(y1 // 1000) + 1, 2):
            p = os.path.join(ordner, f"bDOM_{e}_{n}.zip")
            if not os.path.exists(p):
                continue
            with zipfile.ZipFile(p) as z:
                name = z.namelist()[0]
                a = np.loadtxt(io.TextIOWrapper(z.open(name)), dtype=np.float64)
            ix = np.floor(a[:, 0] - x0).astype(np.int64)
            iy = np.floor(y1 - a[:, 1]).astype(np.int64)
            s = (ix >= 0) & (ix < W) & (iy >= 0) & (iy < H)
            dom[iy[s], ix[s]] = a[s, 2]
            log(f"  bDOM {os.path.basename(p)}: {s.sum():,} Punkte im Gebiet")
    return dom


# ------------------------------------------------------------------ Luftbild (DOP20 JPEG 2000)
def dop_index(ordner, cache):
    if os.path.exists(cache):
        return json.load(open(cache))
    idx = {}
    for zp in sorted(glob.glob(os.path.join(ordner, "*.zip"))):
        with zipfile.ZipFile(zp) as z:
            for n in z.namelist():
                m = re.search(r"_(\d{3})_(\d{4})_", os.path.basename(n))
                if m and n.lower().endswith(".jp2"):
                    idx[f"{m.group(1)}_{m.group(2)}"] = [zp, n]
    json.dump(idx, open(cache, "w"))
    return idx


def jp2_lesen(zp, name):
    """Die Luftbild-ZIPs nutzen Deflate64, das Pythons zipfile nicht kann -> notfalls 'unzip -p'."""
    try:
        with zipfile.ZipFile(zp) as z:
            return z.read(name)
    except NotImplementedError:
        import subprocess
        return subprocess.run(["unzip", "-p", zp, name], check=True, capture_output=True).stdout


def luftbild(ordner, box, res, cache_dir):
    """Ausschnitt des Luftbilds (RGB) mit Auflösung res; DOP-Kacheln 2 x 2 km."""
    x0, y0, x1, y1 = box
    W, H = int(round((x1 - x0) / res)), int(round((y1 - y0) / res))
    out = Image.new("RGB", (W, H), (205, 214, 201))
    idx = dop_index(ordner, os.path.join(cache_dir, "dop_index.json"))
    for e in range(int(x0 // 2000) * 2, int(x1 // 1000) + 1, 2):
        for n in range(int(y0 // 2000) * 2, int(y1 // 1000) + 1, 2):
            k = f"{e}_{n}"
            if k not in idx:
                log(f"  Luftbild-Kachel {k} fehlt")
                continue
            zp, name = idx[k]
            roh = jp2_lesen(zp, name)
            if True:
                im = Image.open(io.BytesIO(roh))
                faktor = max(0, int(math.floor(math.log2(res / 0.2 + 1e-9))))
                im.reduce = faktor                       # JPEG 2000: niedrigere Auflösungsstufe direkt laden
                im.load()
            im = im.convert("RGB") if im.mode not in ("RGB",) else im
            tres = 0.2 * (2 ** faktor)
            # Kachel: links e*1000, oben (n+2)*1000
            kx0, ky1 = e * 1000.0, (n + 2) * 1000.0
            sx0, sy0 = max(x0, kx0), max(y0, ky1 - im.height * tres)
            sx1, sy1 = min(x1, kx0 + im.width * tres), min(y1, ky1)
            if sx1 <= sx0 or sy1 <= sy0:
                continue
            crop = im.crop((int((sx0 - kx0) / tres), int((ky1 - sy1) / tres), int((sx1 - kx0) / tres), int((ky1 - sy0) / tres)))
            crop = crop.resize((int(round((sx1 - sx0) / res)), int(round((sy1 - sy0) / res))), Image.LANCZOS)
            out.paste(crop, (int(round((sx0 - x0) / res)), int(round((y1 - sy1) / res))))
            log(f"  Luftbild {k} eingesetzt")
    return out


# ------------------------------------------------------------------ Hauswände
def waende(geb, hoehe_raster, rinfo, box_kern, gn):
    """Wandflächen -> zusammengefasste Hauswände (eine je Gebäude und Richtung/Flucht)."""
    rx0, ry1, rres = rinfo
    H, W = hoehe_raster.shape

    def nachbar_hoehe(x, y):
        ix, iy = int((x - rx0) / rres), int((ry1 - y) / rres)
        if 0 <= ix < W and 0 <= iy < H:
            return float(hoehe_raster[iy, ix])
        return -1e9

    segs = []
    for gid, d in geb.items():
        boden = [p[:, :2] for p in d["boden"]]
        for v in d["wand"]:
            n3 = newell(v)
            if abs(n3[2]) > 0.3:
                continue
            nh = n3[:2] / (np.linalg.norm(n3[:2]) or 1)
            t = np.array([-nh[1], nh[0]])
            u = v[:, :2] @ t
            mid = v[:, :2].mean(0)
            # CityGML-Flächen sind nach außen orientiert. Nur umdrehen, wenn die Normale eindeutig ins eigene Haus zeigt:
            # vorne im Grundriss, hinten nicht (vorne allein reicht nicht – z. B. Ladenvorbau unter der Fassade)
            if boden and any(im_polygon(*(mid + 0.6 * nh), b) for b in boden) \
                    and not any(im_polygon(*(mid - 0.6 * nh), b) for b in boden):
                nh, t, u = -nh, -t, -u
            a = mid + t * (u.min() - mid @ t)
            b = mid + t * (u.max() - mid @ t)
            segs.append(dict(gid=gid, a=a, b=b, n=nh, zmin=float(v[:, 2].min()), zmax=float(v[:, 2].max()), poly=v))

    # je Gebäude kollineare, aneinanderstoßende Stücke gleicher Richtung zusammenfassen
    proG = defaultdict(list)
    for s in segs:
        proG[s["gid"]].append(s)
    gruppen = []
    for gid, ss in proG.items():
        frei = list(ss)
        while frei:
            g = [frei.pop()]
            geaendert = True
            while geaendert:
                geaendert = False
                n0 = g[0]["n"]
                t0 = np.array([-n0[1], n0[0]])
                lo = min(min(s["a"] @ t0, s["b"] @ t0) for s in g)
                hi = max(max(s["a"] @ t0, s["b"] @ t0) for s in g)
                off = g[0]["a"] @ n0
                for s in list(frei):
                    if s["n"] @ n0 < math.cos(math.radians(5)):
                        continue
                    if abs(s["a"] @ n0 - off) > 0.4:
                        continue
                    u0, u1 = sorted((s["a"] @ t0, s["b"] @ t0))
                    if u0 > hi + 0.5 or u1 < lo - 0.5:
                        continue
                    g.append(s)
                    frei = [x for x in frei if x is not s]
                    geaendert = True
            gruppen.append(g)

    fass = []
    for g in gruppen:
        n0 = g[0]["n"]
        t0 = np.array([-n0[1], n0[0]])
        off = float(np.mean([s["a"] @ n0 for s in g]))
        lo = min(min(s["a"] @ t0, s["b"] @ t0) for s in g)
        hi = max(max(s["a"] @ t0, s["b"] @ t0) for s in g)
        L = hi - lo
        zmin = min(s["zmin"] for s in g)
        zmax = max(s["zmax"] for s in g)
        A = n0 * off + t0 * lo
        Bp = n0 * off + t0 * hi
        # Brandwand / Wand an Nachbarhaus: nur der Teil über dem Nachbardach ist frei
        nb = []
        for f in (0.15, 0.5, 0.85):
            p = A + (Bp - A) * f + n0 * 0.8
            nb.append(nachbar_hoehe(*p))
        nachbar = float(np.median(nb))
        kind = "umriss"
        unten = zmin
        if nachbar > zmin + 1.0:
            unten = nachbar
            kind = "stufe"
        h = zmax - unten
        if L < MIN_LEN or h < MIN_H:
            continue
        mx, my = (A + Bp) / 2
        if not (box_kern[0] <= mx <= box_kern[2] and box_kern[1] <= my <= box_kern[3]):
            continue
        grid_az = math.degrees(math.atan2(n0[0], n0[1]))
        az = (grid_az - gn) % 360
        cls = SECTORS[int(((az + 22.5) % 360) // 45)]
        fass.append(dict(cls=cls, az=round(az, 1), len=round(L, 1), h=round(h, 1), area=round(L * h, 1), ev=1.0,
                         kind=kind, a=[round(float(A[0]), 2), round(float(A[1]), 2)],
                         b=[round(float(Bp[0]), 2), round(float(Bp[1]), 2)],
                         n=[round(float(n0[0]), 4), round(float(n0[1]), 4)],
                         ground=round(zmin, 2), frei_ab=round(unten, 2), top=round(zmax, 2), target=cls in TARGET,
                         gebaeude=g[0]["gid"], _polys=[s["poly"] for s in g]))
    return fass


def fassadenbild(f, pfad):
    """Schematische Frontansicht: Wandumriss (mit Giebel), Geschosslinien, Bodenlinie."""
    t = np.array([-f["n"][1], f["n"][0]])
    left = np.array(f["a"]) - 0.5 * t
    ztop = f["top"] + 0.5
    W = int(math.ceil((f["len"] + 1.0) / IMG_RES))
    Hh = int(math.ceil((ztop - f["ground"] + 1.0) / IMG_RES))
    im = Image.new("RGB", (W, Hh), (226, 231, 222))
    maske = np.zeros((Hh, W), np.uint8)
    for v in f["_polys"]:
        u = (v[:, :2] - left) @ t
        pts = np.c_[u / IMG_RES, (ztop - v[:, 2]) / IMG_RES].round().astype(np.int32)
        if len(pts) >= 3:
            cv2.fillPoly(maske, [pts], 255)
    yb = int(round((ztop - f["ground"]) / IMG_RES))
    yf = int(round((ztop - f["frei_ab"]) / IMG_RES))
    maske[yb:] = 0
    a = np.asarray(im).copy()
    a[maske > 0] = (244, 238, 226)
    if yf < yb:                                     # unterer Teil liegt am Nachbarhaus an
        nb = maske.copy()
        nb[:yf] = 0
        a[nb > 0] = (200, 204, 196)
        maske[yf:] = 0
    a[yb:] = (214, 220, 208)
    cs, _ = cv2.findContours(maske, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(a, cs, -1, (176, 166, 146), 3)
    im = Image.fromarray(a)
    im.save(pfad, quality=80)
    return dict(img_res=IMG_RES, img_ztop=round(ztop, 2), img_left=[round(float(left[0]), 2), round(float(left[1]), 2)],
                img_t=[round(float(t[0]), 4), round(float(t[1]), 4)], img_quelle="schema")


# ------------------------------------------------------------------ Hauptprogramm
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("downloads", help="Ordner mit lod2/, bdom/, luftbild/")
    ap.add_argument("webapp")
    ap.add_argument("--bbox", required=True, help="xmin,ymin,xmax,ymax in EPSG:25833 (Kerngebiet mit Hauswänden)")
    a = ap.parse_args()
    t0 = time.time()
    kern = [float(v) for v in a.bbox.split(",")]
    box = [kern[0] - RAND, kern[1] - RAND, kern[2] + RAND, kern[3] + RAND]
    os.makedirs(os.path.join(a.webapp, "_cache"), exist_ok=True)
    os.makedirs(os.path.join(a.webapp, "fassaden"), exist_ok=True)

    to_ll = Transformer.from_crs(EPSG, "EPSG:4326", always_xy=True)
    to_grid = Transformer.from_crs("EPSG:4326", EPSG, always_xy=True)
    cx, cy = (kern[0] + kern[2]) / 2, (kern[1] + kern[3]) / 2
    lon0, lat0 = to_ll.transform(cx, cy)
    nx_, ny_ = to_grid.transform(lon0, lat0 + 0.001)
    gn = math.degrees(math.atan2(nx_ - cx, ny_ - cy))

    log("LoD2 lesen")
    geb = lies_lod2(lod2_kacheln(os.path.join(a.downloads, "lod2"), box), box)
    log(f"  {len(geb):,} Gebäude")

    log("Gebäudepunkte abtasten")
    pts, dachz = [], []
    for d in geb.values():
        for v in d["wand"] + d["dach"]:
            pts.append(flaeche_abtasten(v, PUNKT))
        for v in d["dach"]:
            dachz.append(v)
    Pg = np.vstack(pts)
    log(f"  {len(Pg):,} Gebäudepunkte")

    # Gebäudehöhen-Raster (1 m) für Nachbarwände und zum Ausmaskieren im bDOM
    W, H = int(box[2] - box[0]), int(box[3] - box[1])
    hb = np.full((H, W), -1e9, np.float32)
    ix = np.clip(((Pg[:, 0] - box[0])).astype(np.int64), 0, W - 1)
    iy = np.clip(((box[3] - Pg[:, 1])).astype(np.int64), 0, H - 1)
    np.maximum.at(hb, (iy, ix), Pg[:, 2].astype(np.float32))
    geb_maske = np.zeros((H, W), np.uint8)
    for d in geb.values():
        for v in d["boden"] + d["dach"]:
            p = np.c_[(v[:, 0] - box[0]), (box[3] - v[:, 1])].round().astype(np.int32)
            cv2.fillPoly(geb_maske, [p], 1)
    hb[geb_maske == 0] = -1e9

    log("Hauswände ableiten")
    fass = waende(geb, hb, (box[0], box[3], 1.0), kern, gn)
    log(f"  {len(fass):,} Hauswände (davon {sum(f['target'] for f in fass):,} nach S/SO/SW)")

    log("bDOM lesen (Bäume, Gelände)")
    dom = lies_bdom(os.path.join(a.downloads, "bdom"), box)
    ok = ~np.isnan(dom)
    gross = np.where(ok & (geb_maske == 0), dom, np.inf)
    gel = minimum_filter(gross, size=25)
    gel2 = minimum_filter(gross, size=81)
    gel = np.where(np.isfinite(gel), gel, gel2)
    gel = np.where(np.isfinite(gel), gel, np.nanmin(dom[ok]) if ok.any() else 30.0)
    gel = uniform_filter(gel, size=15)
    ndom = np.where(ok, dom - gel, 0)
    geb_rand = binary_dilation(geb_maske > 0, iterations=2)
    baum = ok & (ndom > 2.5) & ~geb_rand
    by, bx = np.nonzero(baum)
    top = dom[by, bx]
    hoehe = ndom[by, bx]
    # Krone als Körper: Oberfläche und darunter bis ca. 45 % der Höhe, alle 1 m
    tb = []
    for k in range(0, 8):
        z = top - k * 1.0
        s = z > top - 0.45 * hoehe
        if not s.any():
            break
        tb.append(np.c_[box[0] + bx[s] + 0.5, box[3] - by[s] - 0.5, z[s]])
    Pt = np.vstack(tb) if tb else np.zeros((0, 3))
    # Gelände (grob, 3 m), damit Bodenhöhe und Bereinigung funktionieren
    gy, gx = np.mgrid[1:H:3, 1:W:3]
    Pb = np.c_[box[0] + gx.ravel() + 0.5, box[3] - gy.ravel() - 0.5, gel[gy, gx].ravel()]
    log(f"  {len(Pt):,} Baumpunkte, {len(Pb):,} Geländepunkte")

    P = np.vstack([Pg, Pt, Pb])
    istbaum = np.r_[np.zeros(len(Pg), bool), np.ones(len(Pt), bool), np.zeros(len(Pb), bool)]
    origin = np.floor(P.min(0))
    np.savez_compressed(os.path.join(a.webapp, "_cache", "punkte.npz"), P=(P - origin).astype(np.float32),
                        origin=origin, baum=istbaum)
    for alt in ("wolke_bereinigt.npz", "baum_dreiecke.npy", "mesh.npz"):
        p = os.path.join(a.webapp, "_cache", alt)
        if os.path.exists(p):
            os.replace(p, p + ".alt")

    log("Fassadenansichten (schematisch)")
    for i, f in enumerate(sorted(fass, key=lambda f: (-f["target"], f["a"][0], f["a"][1]))):
        f["id"] = i
        name = f"f{i:04d}.jpg"
        f.update(fassadenbild(f, os.path.join(a.webapp, "fassaden", name)))
        f["img"] = f"fassaden/{name}"
        lo, la = to_ll.transform(*f["a"])
        lo2, la2 = to_ll.transform(*f["b"])
        f["ll"] = [[round(la, 7), round(lo, 7)], [round(la2, 7), round(lo2, 7)]]
        del f["_polys"]

    log("Luftbild")
    ob = luftbild(os.path.join(a.downloads, "luftbild"), kern, ORTHO_RES, os.path.join(a.webapp, "_cache"))
    ob.save(os.path.join(a.webapp, "luftbild.jpg"), quality=82, optimize=True)

    meta = dict(tile="lod2", crs=EPSG, bounds=[round(v, 2) for v in kern], center_ll=[round(lat0, 6), round(lon0, 6)],
                ortho=dict(file="luftbild.jpg", w=ob.width, h=ob.height, res=ORTHO_RES),
                north_grid_az=round(gn, 3), target_classes=list(TARGET), generated=time.strftime("%Y-%m-%d %H:%M"),
                source="LoD2 Berlin, bDOM 2026, DOP20 2025 (Geoportal Berlin, dl-de/zero-2-0)")
    fass.sort(key=lambda f: f["id"])
    with open(os.path.join(a.webapp, "data.js"), "w", encoding="utf-8") as fh:
        fh.write("window.FASSADEN = ")
        json.dump(dict(meta=meta, facades=fass), fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";\n")
    for name, inhalt in (("balkone.json", "{}"), ("balkone.js", "window.BALKONE = {};\n"),
                         ("angebote.js", "window.ANGEBOTE = [];\n"), ("ertrag.js", "window.ERTRAG = null;\n")):
        p = os.path.join(a.webapp, name)
        if not os.path.exists(p):
            open(p, "w").write(inhalt)
    here = os.path.dirname(os.path.abspath(__file__))
    import shutil
    for fn in ("index.html", "intern.html", "robots.txt", "start.command"):
        if os.path.exists(os.path.join(here, fn)):
            shutil.copy(os.path.join(here, fn), os.path.join(a.webapp, fn))
    log(f"Fertig in {time.time() - t0:.0f} s -> {a.webapp}")


if __name__ == "__main__":
    main()
