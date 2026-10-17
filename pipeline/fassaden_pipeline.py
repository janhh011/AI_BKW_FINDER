#!/usr/bin/env python3
"""
Fassaden-Pipeline, Schritt 1
============================
Leitet aus einer Kachel des Berliner 3D-Meshmodells (OBJ + Texturen) die
Gebäudefassaden ab, bestimmt ihre Himmelsrichtung und erzeugt eine lokale
Webseite mit Luftbild und Fassaden-Overlays.

Ablauf
  1. Mesh laden (OBJ/MTL/JPG), Ergebnis wird als .npz zwischengespeichert
  2. Von oben rastern: Oberflächenmodell (DSM) + Luftbild
  3. Geländemodell (DTM) schätzen, Höhe über Gelände (nDSM) berechnen
  4. Gebäude von Bäumen trennen (Höhe, Rauigkeit, Grünanteil)
  5. Gebäudeumrisse als Polygone, Kanten = Fassaden
  6. Je Fassade: Außennormale, wahre Himmelsrichtung, Länge, Höhe,
     Wandnachweis (Anteil der Kante, an dem das Mesh echte Wandflächen
     in dieser Richtung hat)
  7. Je Fassade eine Frontalansicht aus der Mesh-Textur rendern
  8. Webseite schreiben (index.html, data.js, Bilder, start.command)

Aufruf
  python3 fassaden_pipeline.py <kachel-ordner> <ausgabe-ordner>

Abhängigkeiten
  numpy, pillow, opencv-python, scipy, pyproj
"""
import sys
import os
import json
import glob
import time
import math
import shutil

import numpy as np
import cv2
from PIL import Image
from scipy.spatial import cKDTree
from pyproj import Transformer

Image.MAX_IMAGE_PIXELS = None

# ----------------------------------------------------------------------------
# Parameter
# ----------------------------------------------------------------------------
EPSG_SRC = "EPSG:25833"   # ETRS89 / UTM Zone 33N (Berliner Geodaten)
RES = 0.25                # m/px  Analyse-Raster (Höhenmodell, Maske)
ORTHO_RES = 0.15          # m/px  Luftbild für die Webseite
DTM_CELL = 1.0            # m     Zellgröße für die Geländeschätzung
DTM_WINDOW = 61           # m     Fenster der Geländeschätzung (größer als breiteste Gebäude)
MIN_HEIGHT = 3.5          # m     Mindesthöhe über Gelände für "Gebäude"
ROUGH_WIN = 2.0           # m     Fenster für Rauigkeit
ROUGH_STD = 0.45          # m     Std-Abweichung ab der ein Pixel "rau" ist
TREE_ROUGH_FRAC = 0.65    #       Anteil rauer Pixel im Umfeld -> Baum (auch ohne Grün)
TREE_ROUGH_FRAC_GREEN = 0.15  #   Anteil rauer Pixel, ab dem grüne Flächen als Baum gelten
TREE_FRAC_WIN = 5.0       # m     Umfeld für den Rauigkeitsanteil
GREEN_EXG = 0.06          #       Grünindex (2G-R-B)/(R+G+B) ab dem "grün"
MIN_BUILDING_AREA = 40.0  # m²    kleinste Gebäudefläche
MAX_HOLE_FILL = 25.0      # m²    kleinere Löcher in der Maske werden gefüllt
SIMPLIFY = 0.8            # m     Toleranz der Kantenvereinfachung
MIN_EDGE = 3.0            # m     kürzeste Fassade
BORDER_MARGIN = 3.0       # m     Kanten so nah am Kachelrand werden verworfen
EVIDENCE_RADIUS = 1.5     # m     Suchradius für Wanddreiecke
EVIDENCE_ANGLE = 35.0     # Grad  max. Abweichung Wandnormale <-> Fassadennormale
FACADE_RES = 0.05         # m/px  Frontalansicht
FACADE_MAX_W = 1600       # px    max. Bildbreite Frontalansicht
FACADE_FRONT = 3.0        # m     so weit vor der Fassade wird Geometrie mitgerendert (Balkone)
FACADE_BACK = 1.5         # m     so weit hinter der Fassadenlinie
TARGET_CLASSES = ("SO", "S", "SW")

SECTORS = ["N", "NO", "O", "SO", "S", "SW", "W", "NW"]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------------
# 1. Mesh laden
# ----------------------------------------------------------------------------
def find_tile_files(tile_dir):
    objs = sorted(glob.glob(os.path.join(tile_dir, "*.obj")))
    if not objs:
        sys.exit(f"Keine .obj-Datei in {tile_dir}")
    return objs[0]


def parse_mtl(mtl_path):
    mats = {}
    cur = None
    with open(mtl_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            p = line.strip().split(None, 1)
            if not p:
                continue
            if p[0] == "newmtl":
                cur = p[1].strip()
                mats[cur] = None
            elif p[0] == "map_Kd" and cur is not None:
                mats[cur] = p[1].strip()
    return mats


def load_mesh(obj_path, cache_path):
    if os.path.exists(cache_path) and os.path.getmtime(cache_path) > os.path.getmtime(obj_path):
        log(f"Mesh aus Cache: {cache_path}")
        d = np.load(cache_path, allow_pickle=True)
        return (d["V"], d["F"], d["VT"], d["FT"], d["mat"], list(d["tex_files"]))

    log(f"Lese {obj_path}")
    tile_dir = os.path.dirname(obj_path)
    V, VT, F, FT, M = [], [], [], [], []
    mat_names = []
    mat_index = {}
    mtl_file = None
    cur = 0
    with open(obj_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("v "):
                V.append(line.split()[1:4])
            elif line.startswith("vt "):
                VT.append(line.split()[1:3])
            elif line.startswith("f "):
                parts = line.split()[1:]
                idx = [p.split("/") for p in parts]
                # Polygone in Dreiecke zerlegen (Fächer)
                for k in range(1, len(idx) - 1):
                    tri = (idx[0], idx[k], idx[k + 1])
                    F.append([int(t[0]) for t in tri])
                    FT.append([int(t[1]) if len(t) > 1 and t[1] else 0 for t in tri])
                    M.append(cur)
            elif line.startswith("usemtl"):
                name = line.split(None, 1)[1].strip()
                if name not in mat_index:
                    mat_index[name] = len(mat_names)
                    mat_names.append(name)
                cur = mat_index[name]
            elif line.startswith("mtllib"):
                mtl_file = line.split(None, 1)[1].strip()

    mats = parse_mtl(os.path.join(tile_dir, mtl_file)) if mtl_file else {}
    tex_files = [os.path.join(tile_dir, mats.get(n) or "") for n in mat_names]

    V = np.asarray(V, np.float64)
    VT = np.asarray(VT, np.float64) if VT else np.zeros((1, 2))
    F = np.asarray(F, np.int64) - 1
    FT = np.asarray(FT, np.int64) - 1
    M = np.asarray(M, np.int16)
    np.savez_compressed(cache_path, V=V, F=F, VT=VT, FT=FT, mat=M,
                        tex_files=np.array(tex_files, dtype=object))
    log(f"Mesh: {len(V):,} Punkte, {len(F):,} Dreiecke, {len(tex_files)} Texturen")
    return V, F, VT, FT, M, tex_files


def face_normals(V, F):
    n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    area2 = np.linalg.norm(n, axis=1)
    n = n / (area2[:, None] + 1e-12)
    return n, area2 / 2


# ----------------------------------------------------------------------------
# Rasterisierung (Z-Buffer) und Texturierung
# ----------------------------------------------------------------------------
def rasterize(px, py, depth, F, sel, W, H):
    """Rastert die ausgewählten Dreiecke. Kleinere Tiefe = näher am Betrachter.
    Liefert Tiefe, Dreiecks-ID und zwei baryzentrische Koordinaten je Pixel."""
    zb = np.full((H, W), np.inf, np.float64)
    tid = np.full((H, W), -1, np.int64)
    b0 = np.zeros((H, W), np.float32)
    b1 = np.zeros((H, W), np.float32)
    idx = np.nonzero(sel)[0]
    tx = px[F[idx]]
    ty = py[F[idx]]
    tz = depth[F[idx]]
    for k, i in enumerate(idx):
        xs, ys, zs = tx[k], ty[k], tz[k]
        x0 = max(int(math.floor(xs.min())), 0)
        x1 = min(int(math.ceil(xs.max())), W - 1)
        y0 = max(int(math.floor(ys.min())), 0)
        y1 = min(int(math.ceil(ys.max())), H - 1)
        if x1 < x0 or y1 < y0:
            continue
        den = (ys[1] - ys[2]) * (xs[0] - xs[2]) + (xs[2] - xs[1]) * (ys[0] - ys[2])
        if abs(den) < 1e-12:
            continue
        gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        l0 = ((ys[1] - ys[2]) * (gx - xs[2]) + (xs[2] - xs[1]) * (gy - ys[2])) / den
        l1 = ((ys[2] - ys[0]) * (gx - xs[2]) + (xs[0] - xs[2]) * (gy - ys[2])) / den
        l2 = 1.0 - l0 - l1
        m = (l0 >= -1e-3) & (l1 >= -1e-3) & (l2 >= -1e-3)
        if not m.any():
            continue
        dz = l0 * zs[0] + l1 * zs[1] + l2 * zs[2]
        sub = zb[y0:y1 + 1, x0:x1 + 1]
        m &= dz < sub
        if not m.any():
            continue
        sub[m] = dz[m]
        tid[y0:y1 + 1, x0:x1 + 1][m] = i
        b0[y0:y1 + 1, x0:x1 + 1][m] = l0[m]
        b1[y0:y1 + 1, x0:x1 + 1][m] = l1[m]
    return zb, tid, b0, b1


def shade(tid, b0, b1, F, FT, VT, mat, tex, bg=(255, 255, 255)):
    out = np.empty(tid.shape + (3,), np.uint8)
    out[:] = bg
    m = tid >= 0
    if not m.any():
        return out
    t = tid[m]
    l0 = b0[m]
    l1 = b1[m]
    l2 = 1.0 - l0 - l1
    uv = VT[FT[t]]
    u = l0 * uv[:, 0, 0] + l1 * uv[:, 1, 0] + l2 * uv[:, 2, 0]
    v = l0 * uv[:, 0, 1] + l1 * uv[:, 1, 1] + l2 * uv[:, 2, 1]
    col = np.zeros((len(t), 3), np.uint8)
    mt = mat[t]
    for k, T in enumerate(tex):
        s = mt == k
        if not s.any() or T is None:
            continue
        th, tw = T.shape[:2]
        ui = np.clip((u[s] * tw).astype(np.int64), 0, tw - 1)
        vi = np.clip(((1.0 - v[s]) * th).astype(np.int64), 0, th - 1)
        col[s] = T[vi, ui]
    out[m] = col
    return out


# ----------------------------------------------------------------------------
# Raster-Hilfsfunktionen
# ----------------------------------------------------------------------------
def box_mean(a, win_px):
    k = max(int(round(win_px)) | 1, 1)
    return cv2.blur(a.astype(np.float32), (k, k), borderType=cv2.BORDER_REPLICATE)


def remove_small(mask, min_px):
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    keep = np.zeros(n, bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= min_px
    return keep[lab]


def fill_small_holes(mask, max_px):
    inv = (~mask).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(inv, connectivity=4)
    H, W = mask.shape
    out = mask.copy()
    for i in range(1, n):
        x, y, w, h, a = stats[i]
        touches = x == 0 or y == 0 or x + w >= W or y + h >= H
        if a <= max_px and not touches:
            out[lab == i] = True
    return out


# ----------------------------------------------------------------------------
# Hauptprogramm
# ----------------------------------------------------------------------------
def main(tile_dir, out_dir):
    t_start = time.time()
    tile_dir = os.path.abspath(tile_dir)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    img_dir = os.path.join(out_dir, "fassaden")
    os.makedirs(img_dir, exist_ok=True)
    cache_dir = os.path.join(out_dir, "_cache")
    os.makedirs(cache_dir, exist_ok=True)

    obj = find_tile_files(tile_dir)
    tile_name = os.path.splitext(os.path.basename(obj))[0]
    V, F, VT, FT, mat, tex_files = load_mesh(obj, os.path.join(cache_dir, "mesh.npz"))
    log("Lade Texturen")
    tex = [np.asarray(Image.open(p).convert("RGB")) if os.path.exists(p) else None for p in tex_files]
    n, area = face_normals(V, F)

    xmin, ymin = V[:, 0].min(), V[:, 1].min()
    xmax, ymax = V[:, 0].max(), V[:, 1].max()

    # ---------------------------------------------------------------- 2. Raster von oben
    W = int(math.ceil((xmax - xmin) / RES))
    H = int(math.ceil((ymax - ymin) / RES))
    px = (V[:, 0] - xmin) / RES
    py = (ymax - V[:, 1]) / RES
    log(f"Rastere Höhenmodell {W}x{H} px ({RES} m/px)")
    zb, tid, b0, b1 = rasterize(px, py, -V[:, 2], F, np.ones(len(F), bool), W, H)
    valid = tid >= 0
    dsm = np.where(valid, -zb, np.nan).astype(np.float32)
    rgb = shade(tid, b0, b1, F, FT, VT, mat, tex)

    # Luftbild für die Webseite
    OW = int(math.ceil((xmax - xmin) / ORTHO_RES))
    OH = int(math.ceil((ymax - ymin) / ORTHO_RES))
    log(f"Rastere Luftbild {OW}x{OH} px ({ORTHO_RES} m/px)")
    opx = (V[:, 0] - xmin) / ORTHO_RES
    opy = (ymax - V[:, 1]) / ORTHO_RES
    ozb, otid, ob0, ob1 = rasterize(opx, opy, -V[:, 2], F, n[:, 2] > 0.05, OW, OH)
    ortho = shade(otid, ob0, ob1, F, FT, VT, mat, tex, bg=(20, 22, 26))
    Image.fromarray(ortho).save(os.path.join(out_dir, "luftbild.jpg"), quality=85)
    del ozb, otid, ob0, ob1

    # ---------------------------------------------------------------- 3. Gelände
    log("Schätze Geländemodell")
    cell = int(round(DTM_CELL / RES))
    Hc, Wc = H // cell, W // cell
    big = np.where(valid, dsm, np.inf)[:Hc * cell, :Wc * cell]
    coarse = big.reshape(Hc, cell, Wc, cell).min(axis=(1, 3)).astype(np.float32)
    finite = np.isfinite(coarse)
    fill = np.nanmedian(np.where(finite, coarse, np.nan))
    coarse_f = np.where(finite, coarse, fill).astype(np.float32)
    kw = int(round(DTM_WINDOW / DTM_CELL)) | 1
    kern = cv2.getStructuringElement(cv2.MORPH_RECT, (kw, kw))
    # Opening: Erosion (Minimum) + Dilatation entfernt alles, was schmaler als das Fenster ist
    ground = cv2.morphologyEx(cv2.erode(coarse_f, kern), cv2.MORPH_DILATE, kern)
    ground = cv2.GaussianBlur(ground, (0, 0), sigmaX=8)
    dtm = cv2.resize(ground, (Wc * cell, Hc * cell), interpolation=cv2.INTER_LINEAR)
    dtm = np.pad(dtm, ((0, H - Hc * cell), (0, W - Wc * cell)), mode="edge")
    ndsm = np.where(valid, dsm - dtm, 0).astype(np.float32)

    # ---------------------------------------------------------------- 4. Gebäude vs. Bäume
    log("Trenne Gebäude von Bäumen")
    win = ROUGH_WIN / RES
    z = np.where(valid, dsm, dtm).astype(np.float32)
    mu = box_mean(z, win)
    sd = np.sqrt(np.maximum(box_mean(z * z, win) - mu * mu, 0))
    rough = sd > ROUGH_STD
    rough_frac = box_mean(rough.astype(np.float32), TREE_FRAC_WIN / RES)
    rgbf = rgb.astype(np.float32)
    exg = (2 * rgbf[..., 1] - rgbf[..., 0] - rgbf[..., 2]) / (rgbf.sum(-1) + 1.0)
    green = box_mean(exg, 1.0 / RES) > GREEN_EXG
    # Dachkanten sind ebenfalls "rau" (Höhensprung), aber nur in einem schmalen Band.
    # Baum = grün und etwas rau, oder flächig sehr rau.
    tree = (green & (rough_frac > TREE_ROUGH_FRAC_GREEN)) | (rough_frac > TREE_ROUGH_FRAC)

    building = valid & (ndsm > MIN_HEIGHT) & ~tree
    k1 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    building = cv2.morphologyEx(building.astype(np.uint8), cv2.MORPH_OPEN, k1).astype(bool)
    building = cv2.morphologyEx(building.astype(np.uint8), cv2.MORPH_CLOSE, k1).astype(bool)
    building = remove_small(building, MIN_BUILDING_AREA / RES ** 2)
    building = fill_small_holes(building, MAX_HOLE_FILL / RES ** 2)

    # Masken-Overlay für die Webseite (transparent PNG)
    ov = np.zeros((H, W, 4), np.uint8)
    ov[building] = (80, 170, 255, 90)
    ov[tree & valid & (ndsm > MIN_HEIGHT)] = (70, 200, 90, 90)
    Image.fromarray(ov).save(os.path.join(out_dir, "maske.png"), optimize=True)

    # ---------------------------------------------------------------- 5. Umrisse -> Kanten
    log("Extrahiere Gebäudeumrisse")
    contours, _ = cv2.findContours(building.astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)

    def to_utm(c, r):
        return xmin + (c + 0.5) * RES, ymax - (r + 0.5) * RES

    def to_px(x, y):
        return (x - xmin) / RES - 0.5, (ymax - y) / RES - 0.5

    def sample(arr, x, y, default=0):
        c, r = to_px(np.asarray(x), np.asarray(y))
        c = np.round(c).astype(int)
        r = np.round(r).astype(int)
        ok = (c >= 0) & (c < W) & (r >= 0) & (r < H)
        out = np.full(c.shape, default, dtype=arr.dtype)
        out[ok] = arr[r[ok], c[ok]]
        return out

    # ---------------------------------------------------------------- 6. Wandnachweis vorbereiten
    vert = np.abs(n[:, 2]) < 0.3
    cen = V[F].mean(axis=1)
    wall_xy = cen[vert, :2]
    wall_z = cen[vert, 2]
    wall_n = n[vert, :2]
    wall_n = wall_n / (np.linalg.norm(wall_n, axis=1)[:, None] + 1e-12)
    tree_kd = cKDTree(wall_xy)
    cos_lim = math.cos(math.radians(EVIDENCE_ANGLE))

    # Richtung "wahrer Norden" im Gitter (Meridiankonvergenz)
    to_ll = Transformer.from_crs(EPSG_SRC, "EPSG:4326", always_xy=True)
    to_grid = Transformer.from_crs("EPSG:4326", EPSG_SRC, always_xy=True)
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    lon0, lat0 = to_ll.transform(cx, cy)
    nx_, ny_ = to_grid.transform(lon0, lat0 + 0.001)
    north_grid_az = math.degrees(math.atan2(nx_ - cx, ny_ - cy))  # Gitterazimut des wahren Nordens

    facades = []
    fid = 0
    for cnt in contours:
        if len(cnt) < 4:
            continue
        approx = cv2.approxPolyDP(cnt, SIMPLIFY / RES, True)[:, 0, :]
        if len(approx) < 3:
            continue
        pts = [to_utm(c, r) for c, r in approx]
        for k in range(len(pts)):
            ax, ay = pts[k]
            bx, by = pts[(k + 1) % len(pts)]
            dx, dy = bx - ax, by - ay
            L = math.hypot(dx, dy)
            if L < MIN_EDGE:
                continue
            tx, ty = dx / L, dy / L
            # Kachelrand
            if min(ax, bx) - xmin < BORDER_MARGIN or xmax - max(ax, bx) < BORDER_MARGIN \
                    or min(ay, by) - ymin < BORDER_MARGIN or ymax - max(ay, by) < BORDER_MARGIN:
                continue
            ns = max(int(L / 0.5), 3)
            s = (np.arange(ns) + 0.5) / ns
            sx, sy = ax + s * dx, ay + s * dy
            # Außennormale: die Seite, auf der keine Gebäudemaske ist
            cand = np.array([ty, -tx])
            o1 = sample(building, sx + cand[0] * 1.0, sy + cand[1] * 1.0, False).mean()
            o2 = sample(building, sx - cand[0] * 1.0, sy - cand[1] * 1.0, False).mean()
            nrm = cand if o1 < o2 else -cand
            # außen muss Mesh vorhanden sein, sonst Rand/Datenlücke
            out_valid = sample(valid, sx + nrm[0] * 2.0, sy + nrm[1] * 2.0, False).mean()
            if out_valid < 0.6:
                continue
            # Höhen
            h_in = sample(ndsm, sx - nrm[0] * 1.5, sy - nrm[1] * 1.5, np.float32(0))
            height = float(np.median(h_in))
            if height < MIN_HEIGHT:
                continue
            g_out = sample(dtm.astype(np.float32), sx + nrm[0] * 2.0, sy + nrm[1] * 2.0, np.float32(np.nan))
            ground_z = float(np.nanmedian(g_out))
            top_z = ground_z + float(np.percentile(h_in, 80))
            # Wandnachweis
            hits = 0
            for qi, nb in enumerate(tree_kd.query_ball_point(np.c_[sx, sy], EVIDENCE_RADIUS)):
                if not nb:
                    continue
                nb = np.asarray(nb)
                ok = (wall_n[nb] @ nrm > cos_lim) & (wall_z[nb] > ground_z + 1.5) & (wall_z[nb] < top_z + 1.0)
                if ok.any():
                    hits += 1
            evidence = hits / ns
            # Himmelsrichtung
            grid_az = math.degrees(math.atan2(nrm[0], nrm[1]))
            az = (grid_az - north_grid_az) % 360
            cls = SECTORS[int(((az + 22.5) % 360) // 45)]
            lla = to_ll.transform(ax, ay)
            llb = to_ll.transform(bx, by)
            facades.append(dict(
                id=fid, cls=cls, az=round(az, 1), len=round(L, 1), h=round(height, 1),
                area=round(L * height, 0), ev=round(evidence, 2),
                a=[round(ax, 2), round(ay, 2)], b=[round(bx, 2), round(by, 2)],
                n=[round(float(nrm[0]), 4), round(float(nrm[1]), 4)],
                ll=[[round(lla[1], 7), round(lla[0], 7)], [round(llb[1], 7), round(llb[0], 7)]],
                ground=round(ground_z, 2), top=round(top_z, 2),
                target=cls in TARGET_CLASSES,
            ))
            fid += 1
    log(f"{len(facades)} Fassaden, davon {sum(f['target'] for f in facades)} nach SO/S/SW")

    # ---------------------------------------------------------------- 7. Frontalansichten
    log("Rendere Frontalansichten")
    cxy = cen[:, :2]
    for i, fc in enumerate(facades):
        A = np.array(fc["a"])
        B = np.array(fc["b"])
        L = fc["len"]
        t = (B - A) / max(np.linalg.norm(B - A), 1e-9)
        nn = np.array(fc["n"])
        # Blick von außen: links im Bild ist links aus Sicht des Betrachters
        # Betrachter schaut in Richtung d = -n; "rechts" für ihn ist d im Uhrzeigersinn
        # gedreht: (d_y, -d_x) = (-n_y, n_x)
        right = np.array([-nn[1], nn[0]])
        if right @ t < 0:
            A, B = B, A
            t = -t
        u_c = (cxy - A) @ t
        o_c = (cxy - A) @ nn
        zlo = fc["ground"] - 1.0
        zhi = fc["top"] + 3.0
        sel = (u_c > -1.0) & (u_c < L + 1.0) & (o_c > -FACADE_BACK) & (o_c < FACADE_FRONT) \
            & (cen[:, 2] > zlo) & (cen[:, 2] < zhi)
        if sel.sum() < 10:
            fc["img"] = None
            continue
        res = max(FACADE_RES, (L + 1.0) / FACADE_MAX_W)
        uv_ = (V[:, :2] - A) @ t
        ov_ = (V[:, :2] - A) @ nn
        fw = int(math.ceil((L + 1.0) / res))
        fh = int(math.ceil((zhi - zlo) / res))
        fpx = (uv_ + 0.5) / res
        fpy = (zhi - V[:, 2]) / res
        fz, ftid, fb0, fb1 = rasterize(fpx, fpy, -ov_, F, sel, fw, fh)
        img = shade(ftid, fb0, fb1, F, FT, VT, mat, tex, bg=(236, 236, 236))
        # leere Ränder oben abschneiden
        rows = np.nonzero((ftid >= 0).any(axis=1))[0]
        if len(rows):
            img = img[max(rows[0] - 4, 0):rows[-1] + 4]
        name = f"f{fc['id']:04d}.jpg"
        Image.fromarray(img).save(os.path.join(img_dir, name), quality=82)
        fc["img"] = f"fassaden/{name}"
        if (i + 1) % 50 == 0:
            log(f"  {i + 1}/{len(facades)}")

    # ---------------------------------------------------------------- 8. Webseite
    meta = dict(
        tile=tile_name,
        crs=EPSG_SRC,
        bounds=[round(xmin, 2), round(ymin, 2), round(xmax, 2), round(ymax, 2)],
        center_ll=[round(lat0, 6), round(lon0, 6)],
        ortho=dict(file="luftbild.jpg", w=OW, h=OH, res=ORTHO_RES),
        mask=dict(file="maske.png", w=W, h=H, res=RES),
        north_grid_az=round(north_grid_az, 3),
        params=dict(MIN_HEIGHT=MIN_HEIGHT, SIMPLIFY=SIMPLIFY, MIN_EDGE=MIN_EDGE,
                    EVIDENCE_RADIUS=EVIDENCE_RADIUS, EVIDENCE_ANGLE=EVIDENCE_ANGLE),
        target_classes=list(TARGET_CLASSES),
        generated=time.strftime("%Y-%m-%d %H:%M"),
        source="Mesh (Berlin 3D-Meshmodell, Befliegung 2025)",
    )
    with open(os.path.join(out_dir, "data.js"), "w", encoding="utf-8") as f:
        f.write("window.FASSADEN = ")
        json.dump(dict(meta=meta, facades=facades), f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    with open(os.path.join(out_dir, "fassaden.geojson"), "w", encoding="utf-8") as f:
        json.dump(dict(type="FeatureCollection", features=[dict(
            type="Feature",
            geometry=dict(type="LineString", coordinates=[fc["ll"][0][::-1], fc["ll"][1][::-1]]),
            properties={k: fc[k] for k in ("id", "cls", "az", "len", "h", "area", "ev", "target", "img")},
        ) for fc in facades]), f, ensure_ascii=False)

    here = os.path.dirname(os.path.abspath(__file__))
    for fn in ("index.html", "start.command"):
        src = os.path.join(here, fn)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(out_dir, fn))
            if fn.endswith(".command"):
                os.chmod(os.path.join(out_dir, fn), 0o755)
    log(f"Fertig in {time.time() - t_start:.0f} s -> {out_dir}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
