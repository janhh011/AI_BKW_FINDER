#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Durchrechnung je Balkon für die Angebotsseite.

1. Punktwolke aus dem 3D-Mesh; Bäume werden über den Grünanteil der Textur markiert.
2. Horizont je Modulpunkt: Gebäude blockieren die Sonne, Bäume lassen je nach Jahreszeit
   einen Teil durch (belaubt / unbelaubt).
3. Stündliche Simulation über ein Referenzjahr mit pvlib: Sonnenstand, Einstrahlung auf das
   senkrechte Modul am Geländer, Verschattung, Modultemperatur, 800-W-Wechselrichter.
4. Kennzahlen aller Balkone -> webapp/ertrag.js (für den Fassaden-Explorer).
5. Für ausgewählte Balkone eine Angebotsseite mit 3D-Szene -> webapp/angebot/<fassade>_<balkon>/

Nutzung:
  python3 pipeline/angebot.py data/<kachel> webapp
  python3 pipeline/angebot.py data/<kachel> webapp --szene 376:0 --szene 12:1
  python3 pipeline/angebot.py data/<kachel> webapp --wetter pvgis_tmy.json

Wetter: ohne --wetter wird ein synthetisches Referenzjahr erzeugt, das auf mittlere
Monatssummen der Globalstrahlung in Berlin kalibriert ist. Besser ist ein echtes typisches
Jahr von PVGIS (JSON oder CSV), z. B.:
  https://re.jrc.ec.europa.eu/api/v5_2/tmy?lat=52.494&lon=13.321&outputformat=json
"""
import argparse
import json
import math
import os
import shutil
import sys
import time

import numpy as np
import pandas as pd
import pvlib
from PIL import Image, ImageFilter
from pyproj import Geod, Transformer

Image.MAX_IMAGE_PIXELS = None

# ------------------------------------------------------------------ Parameter
MODUL = dict(w=1.72, h=1.13, wp=430, gap=0.04)   # Standardmodul quer am Geländer
KLEIN = dict(w=1.15, h=1.03, wp=250)              # kleines Modul für schmale Balkone
MAX_MODULE = 2
WR_AC = 800.0            # W, Grenze Steckersolargerät
WR_ETA = 0.96
VERLUSTE = 0.03          # Kabel, Mismatch, Verschmutzung
GAMMA = -0.0035          # Temperaturkoeffizient Leistung 1/K
ALBEDO = 0.15            # städtischer Boden
TAU_BELAUBT = 0.30       # Lichtdurchlass Baumkrone Mai–Oktober (Kronen sind nicht dicht, eher optimistisch)
TAU_KAHL = 0.65          # Lichtdurchlass Baumkrone November–April
# Bereinigung der Punktwolke für die Verschattung: nur eindeutige Hindernisse zählen
BAUM_MIN_FLAECHE = 20    # m², kleinere Grünflecken (Hecken, Balkonpflanzen, Fehlklassifikation) zählen nicht
BAUM_MIN_HOEHE = 6.0     # m über Gelände
BAU_MIN_FLAECHE = 30     # m², kleinere Aufbauten/Störungen im Mesh zählen nicht
BAU_MIN_HOEHE = 2.5      # m über Gelände
HORIZONT_MIN_PUNKTE = 6  # so viele Punkte müssen in einem Sektor höher liegen, damit er als verdeckt zählt
NAH_MIN = 1.8            # m, nähere Punkte zählen nicht als Schattenwerfer (eigener Balkon, Mesh-Rauschen)
GREEN_EXG = 0.06         # Grünindex-Schwelle für Baum-Dreiecke (wie in der Fassaden-Pipeline)
PUNKT_ABSTAND = 0.6      # m, Dichte der Punktwolke
HORIZONT_RADIUS = 220.0  # m
AZ_STEP = 2              # Grad je Horizont-Sektor
NB = 360 // AZ_STEP
RAND_WARNUNG = 60.0      # m, Balkone näher am Kachelrand haben unvollständigen Horizont
SZENE_RADIUS = 85.0      # m, Ausschnitt der 3D-Szene
JAHR = 2025
AUSSTELLWINKEL = 30      # Grad, verstellbare Geländerhalterung (Paket "Optimiert")
WP_OPTIMIERT = 450       # Wp, bifaziales Glas-Glas-Modul gleicher Größe

# Mittlere Monatssummen Globalstrahlung horizontal (kWh/m²) und Lufttemperatur (°C), Raum Berlin,
# gerundete Klimamittel. Linke-Trübung typisch Mitteleuropa, Stadt.
GHI_MONAT = [21, 38, 77, 123, 155, 163, 160, 136, 92, 52, 23, 15]
T_MONAT = [1.0, 1.9, 5.0, 10.1, 14.5, 17.7, 19.8, 19.4, 15.1, 10.0, 5.4, 2.1]
LINKE_MONAT = [3.0, 3.3, 3.6, 4.0, 4.2, 4.4, 4.5, 4.4, 4.0, 3.6, 3.2, 3.0]

AZC = (np.arange(NB) + 0.5) * AZ_STEP
ELC = np.arange(0.5, 90.0, 1.0)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------ Eingaben
def lade_eingaben(tile_dir, web):
    txt = open(os.path.join(web, "data.js"), encoding="utf-8").read()
    D = json.loads(txt[txt.index("=") + 1:].strip().rstrip(";"))
    p = os.path.join(web, "balkone.json")
    if not os.path.exists(p):
        sys.exit("balkone.json fehlt – zuerst balkone_claude.py collect ausführen")
    BK = json.load(open(p, encoding="utf-8"))
    mp = os.path.join(web, "_cache", "mesh.npz")
    if not os.path.exists(mp):           # LoD2-Version: kein Foto-Mesh, nur Punktwolke (_cache/punkte.npz)
        return D, BK, None, None, None, None, None, []
    d = np.load(mp, allow_pickle=True)
    tex = [os.path.join(tile_dir, os.path.basename(str(t))) for t in d["tex_files"]]
    missing = [t for t in tex if not os.path.exists(t)]
    if missing:
        sys.exit(f"Texturen fehlen: {missing}")
    return D, BK, d["V"], d["F"], d["VT"], d["FT"], d["mat"], tex


def baum_dreiecke(F, VT, FT, mat, tex, cache):
    """Baum-Markierung je Dreieck über den Grünanteil der Textur."""
    if os.path.exists(cache):
        return np.load(cache)
    log("Markiere Baum-Dreiecke")
    # Farbe je Dreieck: Mittel aus Schwerpunkt und Ecken
    uvs = VT[FT]                                   # (n, 3, 2)
    uvs = np.concatenate([uvs, uvs.mean(1, keepdims=True)], axis=1)
    rgb = np.zeros((len(F), 3), np.float32)
    for m, p in enumerate(tex):
        s = mat == m
        if not s.any():
            continue
        im = np.asarray(Image.open(p).convert("RGB"))
        h, w, _ = im.shape
        u = uvs[s]
        px = np.clip(np.round(u[..., 0] * (w - 1)).astype(np.int64), 0, w - 1)
        py = np.clip(np.round((1 - u[..., 1]) * (h - 1)).astype(np.int64), 0, h - 1)
        rgb[s] = im[py, px].astype(np.float32).mean(1)
        del im
    exg = (2 * rgb[:, 1] - rgb[:, 0] - rgb[:, 2]) / (rgb.sum(1) + 1e-6)
    tri_baum = exg > GREEN_EXG
    np.save(cache, tri_baum)
    return tri_baum


def bereinige_wolke(P, baum, cache):
    """Nur eindeutige Hindernisse behalten: zusammenhängende Gebäude (>= 30 m², nach Öffnen)
    und große Bäume (>= 20 m² Krone, >= 6 m hoch). Kleinteiliges (Hecken, Balkonkästen,
    Mesh-Artefakte, Laternen, Autos) fällt für Schatten und 3D-Modell weg.
    Rückgabe: Maske der Punkte, die bleiben."""
    sig = [BAUM_MIN_FLAECHE, BAUM_MIN_HOEHE, BAU_MIN_FLAECHE, BAU_MIN_HOEHE, len(P)]
    if os.path.exists(cache):
        z = np.load(cache)
        if list(z["sig"]) == sig:
            return z["keep"]
    from scipy.ndimage import minimum_filter, binary_opening, binary_dilation, label, maximum
    res = 1.0
    x0, y0 = P[:, 0].min(), P[:, 1].min()
    ix = ((P[:, 0] - x0) / res).astype(np.int64)
    iy = ((P[:, 1] - y0) / res).astype(np.int64)
    ny, nx = iy.max() + 1, ix.max() + 1
    zmin = np.full((ny, nx), np.inf)
    np.minimum.at(zmin, (iy, ix), P[:, 2])
    zmin[~np.isfinite(zmin)] = np.nanmax(np.where(np.isfinite(zmin), zmin, np.nan))
    gelaende = minimum_filter(zmin, size=31)                       # tiefster Punkt im 31-m-Fenster
    h = P[:, 2] - gelaende[iy, ix]
    acht = np.ones((3, 3), bool)
    # Bäume
    hb = np.full((ny, nx), -1.0)
    np.maximum.at(hb, (iy[baum], ix[baum]), h[baum])
    lab, n = label(hb >= 2.0, structure=acht)
    flaeche = np.bincount(lab.ravel(), minlength=n + 1) * res * res
    hoehe = np.zeros(n + 1)
    if n:
        hoehe[1:] = maximum(hb, lab, index=np.arange(1, n + 1))
    ok_t = (flaeche >= BAUM_MIN_FLAECHE) & (hoehe >= BAUM_MIN_HOEHE)
    ok_t[0] = False
    baum_zelle = ok_t[lab]
    # Gebäude
    hg = np.full((ny, nx), -1.0)
    np.maximum.at(hg, (iy[~baum], ix[~baum]), h[~baum])
    gm = binary_opening(hg >= BAU_MIN_HOEHE, structure=acht)
    lab2, n2 = label(gm, structure=acht)
    ok_g = np.bincount(lab2.ravel(), minlength=n2 + 1) * res * res >= BAU_MIN_FLAECHE
    ok_g[0] = False
    bau_zelle = binary_dilation(ok_g[lab2], structure=acht)          # Wandpunkte am Rand mitnehmen
    keep = np.where(baum, baum_zelle[iy, ix] & (h >= 1.0), bau_zelle[iy, ix] | (h < BAU_MIN_HOEHE))
    np.savez_compressed(cache, keep=keep, sig=np.array(sig, float))
    log(f"Punktwolke bereinigt: {(~keep[baum]).mean() * 100:.0f} % der Baumpunkte und "
        f"{(~keep[~baum]).mean() * 100:.0f} % der übrigen Punkte entfernt (kleine/unklare Hindernisse)")
    return keep


def punktwolke(V, F, tri_baum, cache):
    """Gleichmäßig verteilte Punkte auf allen Dreiecken + Baum-Markierung je Punkt."""
    if os.path.exists(cache):
        z = np.load(cache)
        return z["P"].astype(np.float64) + z["origin"], z["baum"]
    log("Baue Punktwolke aus dem Mesh")
    T = V[F]
    area = 0.5 * np.linalg.norm(np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]), axis=1)
    k = np.clip(np.ceil(area / PUNKT_ABSTAND ** 2), 1, 400).astype(np.int64)
    idx = np.repeat(np.arange(len(F)), k)
    rng = np.random.default_rng(0)
    r1 = np.sqrt(rng.random(len(idx)))
    r2 = rng.random(len(idx))
    P = (T[idx, 0] * (1 - r1)[:, None] + T[idx, 1] * (r1 * (1 - r2))[:, None]
         + T[idx, 2] * (r1 * r2)[:, None])
    P = np.concatenate([P, V])
    baum = np.concatenate([tri_baum[idx], np.zeros(len(V), bool)])
    origin = np.floor(P.min(0))
    np.savez_compressed(cache, P=(P - origin).astype(np.float32), origin=origin, baum=baum)
    log(f"Punktwolke: {len(P):,} Punkte, {baum.mean() * 100:.0f} % Baum")
    return P, baum


# ------------------------------------------------------------------ Wetter
def zeitachse():
    idx = pd.date_range(f"{JAHR}-01-01 00:00", periods=8760, freq="h", tz="Europe/Berlin")
    return idx, idx + pd.Timedelta(minutes=30)


def synth_wetter(lat, lon, mid):
    loc = pvlib.location.Location(lat, lon, tz="Europe/Berlin", altitude=40)
    sp = loc.get_solarposition(mid)
    tl = pd.Series(np.array(LINKE_MONAT)[mid.month - 1], index=mid)
    cs = loc.get_clearsky(mid, model="ineichen", solar_position=sp, linke_turbidity=tl)
    ghi_cs = cs["ghi"].to_numpy()
    rng = np.random.default_rng(42)
    month = mid.month.to_numpy()
    doy = mid.dayofyear.to_numpy()
    kd_h = np.zeros(len(mid))
    K_h = np.zeros(len(mid))
    for m in range(1, 13):
        sel = month == m
        days = np.unique(doy[sel])
        target = GHI_MONAT[m - 1] * 1000.0
        K = min(target / ghi_cs[sel].sum(), 0.92)
        c = 1.6                                    # klein = viele klare und viele trübe Tage
        kd = rng.beta(K * c, (1 - K) * c, len(days))
        day_of = {d: i for i, d in enumerate(days)}
        di = np.array([day_of[d] for d in doy[sel]])
        for _ in range(8):                         # exakt auf die Monatssumme skalieren
            g = (ghi_cs[sel] * kd[di]).sum()
            kd = np.clip(kd * target / max(g, 1e-6), 0.04, 1.0)
        kd_h[sel] = kd[di]
        K_h[sel] = K
    ghi = ghi_cs * kd_h
    er = pvlib.irradiance.erbs(ghi, sp["zenith"], mid)
    dhi = np.clip(er["dhi"].to_numpy(), 0, None)
    dni = np.clip(np.nan_to_num(er["dni"].to_numpy()), 0, cs["dni"].to_numpy() * 1.05)
    hour = mid.hour.to_numpy() + 0.5
    summer = ((month >= 4) & (month <= 9)).astype(float)
    temp = (np.array(T_MONAT)[month - 1] + 3.5 * np.cos(2 * np.pi * (hour - 15) / 24)
            + (2 + 3 * summer) * (kd_h - K_h))
    w = pd.DataFrame(dict(ghi=ghi, dni=dni, dhi=dhi, temp_air=temp, wind_speed=2.0), index=mid)
    return w, sp, "synthetisches Referenzjahr (kalibriert auf Berliner Monatsmittel der Einstrahlung)"


def pvgis_wetter(path, lat, lon, mid):
    r = pvlib.iotools.read_pvgis_tmy(path, map_variables=True)
    data = r[0] if isinstance(r, tuple) else r
    data = data.copy()
    data.index = pd.DatetimeIndex([t.replace(year=JAHR) for t in data.index])
    if data.index.tz is None:
        data.index = data.index.tz_localize("UTC")
    data = data.tz_convert("Europe/Berlin").sort_index()
    data = data[~data.index.duplicated()]
    data = data.reindex(mid, method="nearest")
    loc = pvlib.location.Location(lat, lon, tz="Europe/Berlin", altitude=40)
    sp = loc.get_solarposition(mid)
    w = data[["ghi", "dni", "dhi", "temp_air", "wind_speed"]].astype(float)
    return w, sp, "PVGIS typisches Referenzjahr (TMY)"


# ------------------------------------------------------------------ Geometrie
def modul_vorschlag(breite):
    fit = int(math.floor((breite + MODUL["gap"]) / (MODUL["w"] + MODUL["gap"])))
    n = min(fit, MAX_MODULE)
    if n >= 1:
        return dict(n=n, fit=fit, w=MODUL["w"], h=MODUL["h"], wp_modul=MODUL["wp"], wp=n * MODUL["wp"],
                    text=f"{n} × Standardmodul ({MODUL['w']:.2f} × {MODUL['h']:.2f} m, je {MODUL['wp']} Wp)"
                    .replace(".", ","))
    if breite >= KLEIN["w"] + 0.05:
        return dict(n=1, fit=0, w=KLEIN["w"], h=KLEIN["h"], wp_modul=KLEIN["wp"], wp=KLEIN["wp"],
                    text=f"1 × kleines Modul ({KLEIN['w']:.2f} m breit, {KLEIN['wp']} Wp)".replace(".", ","))
    return None


def balkon_geometrie(f, b, P, baum, n_module=None):
    res = f["img_res"]
    L0 = np.array(f["img_left"], float)
    t = np.array(f["img_t"], float)
    n = np.array(f["n"], float)
    x0, y0, x1, y1 = b["box"]
    s0, s1 = x0 * res, x1 * res
    zt = f["img_ztop"] - y0 * res
    zb = f["img_ztop"] - y1 * res
    c2 = L0 + t * (s0 + s1) / 2

    # Lage der Geländer-Vorderkante aus dem Mesh schätzen
    near = (np.abs(P[:, 0] - c2[0]) < 12) & (np.abs(P[:, 1] - c2[1]) < 12)
    Q = P[near]
    qb = baum[near]
    u = (Q[:, :2] - L0) @ t
    o = (Q[:, :2] - L0) @ n
    # Balkonkörper inkl. Platte darunter und Brüstungsoberkante: alles, was vor der Wand liegt
    s = (u > s0 + 0.2) & (u < s1 - 0.2) & (Q[:, 2] > zb - 0.4) & (Q[:, 2] < zt + 0.2) & (o > -0.5) & (o < 4.0) & ~qb
    default = {"Balkon": 1.2, "Loggia": 0.1}.get(b.get("typ"), 0.5)
    rand = 0.05
    if f.get("img_quelle") == "schema" or b.get("tiefe_m"):
        # Gebäudemodell ohne Balkone (LoD2): Tiefe aus der Eingabe oder typischer Wert
        tiefe = float(b.get("tiefe_m") or default)
    elif s.sum() >= 30:
        # Vorderkante des Balkons: fast der äußerste Punkt (robust gegen einzelne Ausreißer),
        # Module hängen mit etwas Abstand außen davor – nie im Balkon
        tiefe = float(np.percentile(o[s], 97))
        rand = 0.15
    else:
        tiefe = default
    tiefe = float(np.clip(tiefe, 0.05, 3.5))
    off = tiefe + rand

    mv = modul_vorschlag(b["breite_m"])
    if mv is None:
        return None
    if n_module is not None and mv["fit"] >= n_module:
        mv = dict(mv, n=n_module, wp=n_module * MODUL["wp"],
                  text=f"{n_module} × Standardmodul ({MODUL['w']:.2f} × {MODUL['h']:.2f} m, je {MODUL['wp']} Wp)"
                  .replace(".", ","))
    total = mv["n"] * mv["w"] + (mv["n"] - 1) * MODUL["gap"]
    u0 = (s0 + s1) / 2 - total / 2
    z_top = min(zt, zb + 1.25)
    z_bot = z_top - mv["h"]
    module = []
    for i in range(mv["n"]):
        ua = u0 + i * (mv["w"] + MODUL["gap"])
        ub = ua + mv["w"]
        pa = L0 + t * ua + n * off
        pb = L0 + t * ub + n * off
        ecken = [[pa[0], pa[1], z_bot], [pb[0], pb[1], z_bot], [pb[0], pb[1], z_top], [pa[0], pa[1], z_top]]
        punkte = []
        for fu in (1 / 6, 0.5, 5 / 6):
            for fz in (0.25, 0.75):
                pp = L0 + t * (ua + fu * mv["w"]) + n * off
                punkte.append([pp[0], pp[1], z_bot + fz * mv["h"]])
        module.append(dict(ecken=np.array(ecken), punkte=np.array(punkte)))
    mitte = L0 + t * (s0 + s1) / 2 + n * off
    return dict(module=module, vorschlag=mv, tiefe=tiefe, n=n, t=t,
                mitte=np.array([mitte[0], mitte[1], (z_bot + z_top) / 2]), zb=zb, zt=zt)


def horizont_vorfilter(geo, P, baum):
    c = geo["mitte"]
    d = P[:, :2] - c[:2]
    s = (np.abs(d[:, 0]) < HORIZONT_RADIUS) & (np.abs(d[:, 1]) < HORIZONT_RADIUS) \
        & (P[:, 2] > geo["zb"] - 0.5) & (d @ geo["n"] > 0.2)
    return P[s], baum[s]


def horizont(p, nrm, Q, qb, gn_true):
    d = Q - p
    dh = np.hypot(d[:, 0], d[:, 1])
    front = d[:, :2] @ nrm
    # Teile direkt am Modul (Balkonplatte darüber, Brüstung, Loggia-Wände) sind im Mesh unsicher und
    # werden ignoriert: es zählen nur Hindernisse ab NAH_MIN Meter Abstand vor dem Modul.
    ok = (front > NAH_MIN) & (dh > NAH_MIN) & (d[:, 2] > 0) & (dh < HORIZONT_RADIUS)
    d, dh, bt = d[ok], dh[ok], qb[ok]
    el = np.degrees(np.arctan2(d[:, 2], dh))
    azt = (np.degrees(np.arctan2(d[:, 0], d[:, 1])) + gn_true) % 360
    bi = (azt // AZ_STEP).astype(np.int64) % NB
    return _horizont_robust(bi[~bt], el[~bt]), _horizont_robust(bi[bt], el[bt])


def _horizont_robust(bi, el, n=HORIZONT_MIN_PUNKTE):
    """Je Richtungssektor die Höhe, über der noch mindestens n Punkte liegen.
    Einzelne Ausreißer im Mesh (Antennen, Kanten, Rauschen) werfen so keinen Schatten."""
    h = np.zeros(NB)
    if len(el) == 0:
        return h
    o = np.lexsort((-el, bi))
    bi, el = bi[o], el[o]
    start = np.searchsorted(bi, np.arange(NB))
    ende = np.searchsorted(bi, np.arange(NB), side="right")
    for k in range(NB):
        c = ende[k] - start[k]
        if c >= n:
            h[k] = el[start[k] + n - 1]
    return h


def himmel_faktor(hb, ht, az_mod, tau, neigung=90.0):
    """Anteil des für die Modulfläche sichtbaren Himmels (isotrop gewichtet); neigung 90° = senkrecht."""
    A = np.radians(AZC - az_mod)[:, None]
    E = np.radians(ELC)[None, :]
    b = math.radians(neigung)
    ns = math.sin(b) * np.cos(E) * np.cos(A) + math.cos(b) * np.sin(E)
    w = np.clip(ns, 0, None) * np.cos(E)
    Bb = ELC[None, :] < hb[:, None]
    Bt = (ELC[None, :] < ht[:, None]) & ~Bb
    tot = w.sum()
    return float((tot - (w * Bb).sum() - (w * Bt).sum() * (1 - tau)) / tot)


# ------------------------------------------------------------------ Simulation
class Sim:
    def __init__(self, wetter, sp, mid):
        self.w = wetter
        self.sp = sp
        self.mid = mid
        self.el = (90 - sp["apparent_zenith"]).to_numpy()
        self.az = sp["azimuth"].to_numpy()
        self.bi = (self.az // AZ_STEP).astype(np.int64) % NB
        self.month = mid.month.to_numpy()
        self.belaubt = (self.month >= 5) & (self.month <= 10)
        self.tau = np.where(self.belaubt, TAU_BELAUBT, TAU_KAHL)
        self.dni_extra = pvlib.irradiance.get_extra_radiation(mid).to_numpy()

    def einstrahlung(self, az_mod, neigung=90.0):
        sp, w = self.sp, self.w
        aoi = pvlib.irradiance.aoi(neigung, az_mod, sp["apparent_zenith"], sp["azimuth"]).to_numpy()
        hd = pvlib.irradiance.haydavies(neigung, az_mod, w["dhi"], w["dni"], self.dni_extra,
                                        sp["apparent_zenith"], sp["azimuth"], return_components=True)
        iso = np.nan_to_num(np.asarray(hd["poa_isotropic"], float))
        circ = np.nan_to_num(np.asarray(hd["poa_circumsolar"], float))
        beam = np.where((aoi < 90) & (self.el > 0), w["dni"].to_numpy() * np.cos(np.radians(aoi)), 0.0)
        beam = np.clip(beam, 0, None)
        ground = w["ghi"].to_numpy() * ALBEDO * (1 - math.cos(math.radians(neigung))) / 2
        iam = np.nan_to_num(pvlib.iam.ashrae(aoi, b=0.05))
        return dict(aoi=aoi, iso=iso, circ=circ, beam=beam, ground=ground, iam=iam)

    def leistung(self, E, modul_frac, modul_iso, wp_modul):
        """modul_frac: Liste je Modul (8760,) Anteil direkte Sonne; modul_iso: (8760,) Himmelsfaktor."""
        pdc = np.zeros(len(self.mid))
        for frac, isof in zip(modul_frac, modul_iso):
            direkt = (E["beam"] + E["circ"]) * frac
            glob = direkt + E["iso"] * isof + E["ground"]
            eff = direkt * E["iam"] + (E["iso"] * isof + E["ground"]) * 0.95
            tc = pvlib.temperature.faiman(glob, self.w["temp_air"].to_numpy(), self.w["wind_speed"].to_numpy())
            pdc += pvlib.pvsystem.pvwatts_dc(eff, tc, wp_modul, GAMMA) * (1 - VERLUSTE)
        pac = pvlib.inverter.pvwatts(pdc, WR_AC / WR_ETA, WR_ETA)
        return np.clip(np.nan_to_num(np.asarray(pac, float)), 0, WR_AC)

    def referenz_dach(self):
        """Plausibilitätscheck: 1 kWp, 35° Süd, unverschattet."""
        sp, w = self.sp, self.w
        poa = pvlib.irradiance.get_total_irradiance(35, 180, sp["apparent_zenith"], sp["azimuth"],
                                                    w["dni"], w["ghi"], w["dhi"], dni_extra=self.dni_extra,
                                                    model="haydavies", albedo=ALBEDO)
        g = np.nan_to_num(poa["poa_global"].to_numpy())
        tc = pvlib.temperature.faiman(g, w["temp_air"].to_numpy(), w["wind_speed"].to_numpy())
        return float((pvlib.pvsystem.pvwatts_dc(g, tc, 1000, GAMMA) * 0.86).sum() / 1000)


def simuliere_balkon(sim, geo, Q, qb, gn_true, az_mod, neigung=90.0, wp_modul=None):
    E = sim.einstrahlung(az_mod, neigung)
    nrm = geo["n"]
    mv = geo["vorschlag"]
    fracs, isos, horis = [], [], []
    for m in geo["module"]:
        fr = np.zeros(len(sim.mid))
        iso_b = iso_k = 0.0
        for p in m["punkte"]:
            hb, ht = horizont(p, nrm, Q, qb, gn_true)
            horis.append((hb, ht))
            frei = sim.el > hb[sim.bi]
            hinter_baum = sim.el <= ht[sim.bi]
            fr += np.where(frei, np.where(hinter_baum, sim.tau, 1.0), 0.0)
            iso_b += himmel_faktor(hb, ht, az_mod, TAU_BELAUBT, neigung)
            iso_k += himmel_faktor(hb, ht, az_mod, TAU_KAHL, neigung)
        k = len(m["punkte"])
        fracs.append(fr / k)
        isos.append(np.where(sim.belaubt, iso_b / k, iso_k / k))
    wpm = wp_modul or mv["wp_modul"]
    pac = sim.leistung(E, fracs, isos, wpm)
    one = np.ones(len(sim.mid))
    pac_frei = sim.leistung(E, [one] * len(fracs), [one] * len(fracs), wpm)
    hb_all = np.max([h[0] for h in horis], axis=0)
    ht_all = np.max([h[1] for h in horis], axis=0)
    # Sonnenstunden auf dem Modul (Mittel der Module, nur Stunden mit Sonne vor der Fassade)
    vor = (sim.el > 0) & (E["aoi"] < 90)
    sonne_frei = float(vor.sum())
    sonne_mod = float((np.mean(fracs, axis=0) * vor).sum())
    return dict(pac=pac, pac_frei=pac_frei, hb=hb_all, ht=ht_all,
                frac=np.mean(fracs, axis=0), sonne_frei=sonne_frei, sonne_mod=sonne_mod,
                himmel=float(np.mean([i.mean() for i in isos])))


# ------------------------------------------------------------------ Export
def exportiere_szene(out, P0, z0, V, F, VT, FT, mat, tri_baum):
    c = V[F].mean(1)
    sel = np.hypot(c[:, 0] - P0[0], c[:, 1] - P0[1]) < SZENE_RADIUS
    tri = np.nonzero(sel)[0]
    gruppe = mat[tri].astype(np.int64) * 2 + tri_baum[tri]     # sortiert nach Textur, dann Baum
    tri = tri[np.argsort(gruppe, kind="stable")]
    gruppe = mat[tri].astype(np.int64) * 2 + tri_baum[tri]
    X = V[F[tri]].reshape(-1, 3)
    pos = np.stack([X[:, 0] - P0[0], X[:, 2] - z0, -(X[:, 1] - P0[1])], axis=1).astype(np.float32)
    uv = VT[FT[tri]].reshape(-1, 2).astype(np.float32)
    with open(os.path.join(out, "scene.bin"), "wb") as fh:
        fh.write(pos.tobytes())
        fh.write(uv.tobytes())
    groups = [dict(mat=int(g // 2), baum=bool(g % 2), count=int((gruppe == g).sum() * 3)) for g in np.unique(gruppe)]
    return dict(file="scene.bin", vertices=int(len(pos)), groups=groups, radius=SZENE_RADIUS)


def lokal(p, P0, z0):
    return [round(float(p[0] - P0[0]), 3), round(float(p[2] - z0), 3), round(float(-(p[1] - P0[1])), 3)]


MODELL_RADIUS = 55.0     # m, Ausschnitt des vereinfachten Hausmodells
MODELL_RES = 0.5         # m, Rasterweite
MODELL_STUFE = 1.0       # m, Höhenstufen der Gebäudeblöcke


def luftbild_ausschnitt(out, P0, ortho, groesse):
    """Quadratischer Luftbild-Ausschnitt (Norden oben) um P0 mit Kantenlänge 'groesse' -> luftbild.jpg."""
    if not ortho:
        return None
    im = ortho["bild"]
    res = ortho["res"]
    x0, y1 = ortho["x0"], ortho["y1"]
    h = groesse / 2
    box = (int(round((P0[0] - h - x0) / res)), int(round((y1 - P0[1] - h) / res)),
           int(round((P0[0] + h - x0) / res)), int(round((y1 - P0[1] + h) / res)))
    if box[2] <= 0 or box[3] <= 0 or box[0] >= im.width or box[1] >= im.height:
        return None
    c = im.crop(box)              # außerhalb des Bildes: schwarz -> mit Bodenfarbe füllen
    if box[0] < 0 or box[1] < 0 or box[2] > im.width or box[3] > im.height:
        bg = Image.new("RGB", c.size, (223, 232, 216))
        bg.paste(im.crop((max(0, box[0]), max(0, box[1]), min(im.width, box[2]), min(im.height, box[3]))),
                 (max(0, -box[0]), max(0, -box[1])))
        c = bg
    # ruhiger Untergrund im Stil eines Architekturmodells: entsättigt, weich, leicht ins Salbeigrün gezogen
    a = np.asarray(c.filter(ImageFilter.GaussianBlur(1.2))).astype(float)
    grau = a.mean(axis=2, keepdims=True)
    a = 0.45 * a + 0.55 * grau
    a = a * 0.55 + np.array([214, 222, 207], float) * 0.45
    Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).save(os.path.join(out, "luftbild.jpg"), quality=80)
    return dict(file="luftbild.jpg", groesse=round(groesse, 2))


def lod2_gebaeude(lod2, P0, z0, R):
    """Gebäude aus dem LoD2-Modell im Umkreis R um P0 (lokale Koordinaten x Ost, y Nord, z über Gelände)."""
    d = np.hypot(lod2["mitte"][:, 0] - P0[0], lod2["mitte"][:, 1] - P0[1])
    nahe = np.unique(lod2["geb"][d < R * 1.1])
    out = []
    P, st = lod2["P"], lod2["start"]
    for g in nahe:
        flaechen = []
        hmax = 0.0
        for i in lod2["polys_von"][g]:
            v = P[st[i]:st[i + 1]] - np.array([P0[0], P0[1], z0])
            hmax = max(hmax, float(v[:, 2].max()))
            r, gg, b = (int(x) for x in lod2["farbe"][i])
            flaechen.append([int(lod2["typ"][i]), (r << 16) | (gg << 8) | b, np.round(v.ravel(), 2).tolist()])
        boden = []
        for j in lod2["boden_von"].get(int(g), []):
            q = lod2["boden"][lod2["boden_start"][j]:lod2["boden_start"][j + 1]] - P0[:2]
            if len(q) > len(boden):
                boden = np.round(q, 2).tolist()
        out.append(dict(h=round(hmax, 2), boden=boden, f=flaechen))
    return out


def illustriere(a, maske):
    """Mesh-Foto als ruhige Illustration: Flächen glätten, Farben Richtung Sand ziehen, nur deutliche
    Kanten (Fenster, Balkone) als weiche dunkle Linien. Versteckt die Schmelzkanten des Meshs."""
    import cv2
    h, w = a.shape[:2]
    x = cv2.resize(a, (max(1, w // 2), max(1, h // 2)), interpolation=cv2.INTER_AREA)
    x = cv2.edgePreservingFilter(x, flags=1, sigma_s=30, sigma_r=0.3)
    x = cv2.bilateralFilter(x, 7, 40, 7)
    x = cv2.resize(x, (w, h), interpolation=cv2.INTER_CUBIC).astype(float)
    x = x * 0.78 + np.array([240, 235, 225], float) * 0.22
    g = cv2.cvtColor(cv2.resize(a, (max(1, w // 2), max(1, h // 2)), interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
    g = cv2.GaussianBlur(g, (3, 3), 0)
    k = (cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 21, 10) == 0).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(k, 8)
    k = np.isin(lab, np.nonzero(st[:, 4] >= 25)[0][1:]).astype(np.uint8)
    k = cv2.resize(k, (w, h), interpolation=cv2.INTER_NEAREST)
    x[k > 0] *= 0.62
    x = np.clip(x, 0, 255).astype(np.uint8)
    x[~maske] = 236
    return x


def fassaden_foto(out, f, web, P0, z0):
    """Eigene Hauswand für die 3D-Ansicht: Mesh-Foto als Illustration, zugeschnitten auf den
    LoD2-Umriss der Wand (keine ausgefransten Mesh-Ränder), Hintergrund transparent."""
    if f.get("img_quelle") != "mesh" or not web:
        return None
    p = os.path.join(web, f["img"])
    if not os.path.exists(p):
        return None
    im = Image.open(p).convert("RGB")
    a = np.asarray(im)
    maske = np.abs(a.astype(int) - 236).max(axis=2) > 4
    ps = os.path.join(web, f.get("img_schema") or "")
    if f.get("img_schema") and os.path.exists(ps):
        sch = Image.open(ps).convert("RGB")
        if sch.size != im.size:
            sch = sch.resize(im.size, Image.NEAREST)
        sa = np.asarray(sch).astype(int)
        wand = (np.abs(sa - np.array([244, 238, 226])).max(axis=2) <= 12) | \
               (np.abs(sa - np.array([200, 204, 196])).max(axis=2) <= 12)
        import cv2
        wand = cv2.dilate(wand.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        luecke = wand & ~maske
        a = illustriere(a, maske & wand)
        a[luecke] = (238, 232, 221)                # Lücken im Mesh: Wandfarbe statt Loch
        maske = wand                               # sauberer Umriss aus dem LoD2-Modell
    else:
        a = illustriere(a, maske)
    alpha = np.where(maske, 255, 0).astype(np.uint8)
    Image.fromarray(np.dstack([a, alpha])).save(os.path.join(out, "fassade.webp"), quality=82)
    res = f["img_res"]
    left = np.array(f["img_left"], float) + np.array(f["n"], float) * 0.06    # knapp vor die Wand
    t = np.array(f["img_t"], float)
    zt, zb = f["img_ztop"], f["img_ztop"] - im.height * res
    r = left + t * im.width * res
    ecken = [[*left, zt], [*r, zt], [*r, zb], [*left, zb]]
    return dict(file="fassade.webp", ecken=[[round(float(x - P0[0]), 3), round(float(y - P0[1]), 3), round(float(z - z0), 3)]
                                            for x, y, z in ecken])


def wand_ansicht(out, f, web, b, geo, max_w=1000):
    """Dieselbe Wandansicht, auf der der Nutzer seinen Balkon gewählt hat, mit Balkon und Modulen in Pixeln."""
    if not web or not f.get("img"):
        return None
    p = os.path.join(web, f["img"])
    if not os.path.exists(p):
        return None
    im = Image.open(p).convert("RGB")
    k = min(1.0, max_w / im.width)
    if k < 1.0:
        im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
    im.save(os.path.join(out, "wand.jpg"), quality=84)
    res = f["img_res"]
    L0 = np.array(f["img_left"], float)
    t = np.array(f["img_t"], float)
    module = []
    for m in geo["module"]:
        e = m["ecken"]
        u = (e[:, :2] - L0) @ t / res * k
        y = (f["img_ztop"] - e[:, 2]) / res * k
        module.append([round(float(u.min()), 1), round(float(y.min()), 1), round(float(u.max()), 1), round(float(y.max()), 1)])
    return dict(file="wand.jpg", w=im.width, h=im.height, box=[round(v * k, 1) for v in b["box"]], module=module,
                quelle=f.get("img_quelle", "mesh"))


def exportiere_modell(out, P0, z0, f, balkone, P, baum, aktiv_bi=None, geo_wolke=None, ortho=None, lod2=None):
    """Vereinfachtes Modell für die Angebotsseite: Gebäude als Blöcke in 1-m-Stufen,
    Bäume als Kronen, Balkone der eigenen Fassade als Platten mit Brüstung."""
    import cv2
    from scipy.ndimage import maximum_filter, grey_closing

    R, res = MODELL_RADIUS, MODELL_RES
    n = int(2 * R / res)
    d = P[:, :2] - P0[:2]
    s = (np.abs(d[:, 0]) < R) & (np.abs(d[:, 1]) < R)
    ix = ((d[s, 0] + R) / res).astype(np.int64).clip(0, n - 1)
    iy = ((d[s, 1] + R) / res).astype(np.int64).clip(0, n - 1)
    h = P[s, 2] - z0
    bt = baum[s]
    hb = np.full((n, n), -5.0)
    ht = np.full((n, n), -5.0)
    np.maximum.at(hb, (iy[~bt], ix[~bt]), h[~bt])
    np.maximum.at(ht, (iy[bt], ix[bt]), h[bt])
    hb = grey_closing(hb, size=3)

    # Bereich vor der eigenen Fassade freiräumen – dort kommen die erkannten Balkone hin
    A = np.array(f["a"], float) - P0[:2]
    B = np.array(f["b"], float) - P0[:2]
    L = float(np.linalg.norm(B - A))
    t = (B - A) / max(L, 1e-6)
    nn = np.array(f["n"], float)
    gx = (np.arange(n) + 0.5) * res - R
    GX, GY = np.meshgrid(gx, gx)
    u = (GX - A[0]) * t[0] + (GY - A[1]) * t[1]
    o = (GX - A[0]) * nn[0] + (GY - A[1]) * nn[1]
    hb[(o > 0.15) & (o < 4.0) & (u > -0.3) & (u < L + 0.3)] = -5.0

    bau = (hb > 2.5).astype(np.uint8)
    if lod2 is not None:
        bau[:] = 0          # Gebäude kommen als echte LoD2-Geometrie (Dachformen), keine Blöcke
    bau = cv2.morphologyEx(bau, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    hmax = float(hb[bau > 0].max()) if bau.any() else 0
    bloecke = []
    unten = 0.0
    for oben in np.arange(MODELL_STUFE * 3, hmax + MODELL_STUFE, MODELL_STUFE):
        m = ((hb >= oben - MODELL_STUFE / 2) & (bau > 0)).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        if not m.any():
            break
        cs, hier = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if hier is None:
            continue
        hier = hier[0]
        def poly(c):
            c = cv2.approxPolyDP(c, 1.3, True)[:, 0, :].astype(float)
            return [[round((x + 0.5) * res - R, 2), round((y + 0.5) * res - R, 2)] for x, y in c]
        for i, c in enumerate(cs):
            if hier[i][3] != -1 or cv2.contourArea(c) * res * res < 4:
                continue
            holes = []
            j = hier[i][2]
            while j != -1:
                if cv2.contourArea(cs[j]) * res * res > 3:
                    holes.append(poly(cs[j]))
                j = hier[j][0]
            outer = poly(c)
            if len(outer) >= 3:
                bloecke.append(dict(z0=round(unten, 2), z1=round(float(oben), 2), outer=outer, holes=holes))
        unten = float(oben)

    # Baumkronen: lokale Maxima der Baumhöhe
    ht_s = np.where(ht > 3.5, ht, 0)
    peaks = (ht_s == maximum_filter(ht_s, size=int(5 / res))) & (ht_s >= BAUM_MIN_HOEHE)
    baeume = []
    for y, x in zip(*np.nonzero(peaks)):
        hh = float(ht_s[y, x])
        r = float(np.clip(hh * 0.28, 2.0, 5.0))
        bx, by = (x + 0.5) * res - R, (y + 0.5) * res - R
        if bx * nn[0] + by * nn[1] < -4:          # Bäume hinter dem eigenen Haus spielen keine Rolle -> weglassen
            continue
        baeume.append([round(bx, 2), round(by, 2), round(hh, 1), round(r, 1)])

    # Balkone der eigenen Fassade
    platten = []
    RUECK = 0.6     # Platte reicht so weit in die Wand, damit sie nie frei schwebt
    for i, b in enumerate(balkone):
        aktiv = i == aktiv_bi
        if not aktiv and (b.get("typ") == "Loggia" or b.get("sicherheit", 0) < 0.3):
            continue
        g = balkon_geometrie(f, b, *(geo_wolke or (P, baum)))
        tiefe = g["tiefe"] if g else 1.2
        res_i = f["img_res"]
        L0 = np.array(f["img_left"], float) - P0[:2]
        ti = np.array(f["img_t"], float)
        x0, y0, x1, y1 = b["box"]
        s0, s1 = x0 * res_i, x1 * res_i
        zb = f["img_ztop"] - y1 * res_i - z0
        if aktiv and g:
            # Platte direkt unter die Module legen (bei niedrigen Erkennungsboxen hängen die Module sonst darunter)
            z_mod = float(min(e[2] for m in g["module"] for e in m["ecken"])) - z0
            zb = min(zb, z_mod - 0.05)
            breite_mod = float(np.linalg.norm(g["module"][-1]["ecken"][1][:2] - g["module"][0]["ecken"][0][:2]))
            if breite_mod + 0.2 > s1 - s0:      # Platte mindestens so breit wie die Module
                m = (s0 + s1) / 2
                s0, s1 = m - breite_mod / 2 - 0.1, m + breite_mod / 2 + 0.1
        gesamt = tiefe + RUECK
        c = L0 + ti * (s0 + s1) / 2 + nn * (tiefe - RUECK) / 2
        platten.append(dict(c=[round(float(c[0]), 2), round(float(zb), 2), round(float(-c[1]), 2)],
                            breite=round(float(s1 - s0), 2), tiefe=round(float(gesamt), 2), aktiv=aktiv))
    modell = dict(radius=R, bloecke=bloecke, baeume=baeume, balkone=platten,
                  drehung=round(float(math.atan2(t[1], t[0])), 4),
                  luftbild=luftbild_ausschnitt(out, P0, ortho, 2 * R * 1.15),
                  gebaeude=lod2_gebaeude(lod2, P0, z0, R) if lod2 is not None else None)
    with open(os.path.join(out, "modell.json"), "w", encoding="utf-8") as fh:
        json.dump(modell, fh, separators=(",", ":"))
    return dict(file="modell.json", bloecke=len(bloecke), baeume=len(baeume), balkone=len(platten))


# ------------------------------------------------------------------ Hauptprogramm
class Rechner:
    """Lädt Kachel, Punktwolke und Wetter einmal und rechnet danach beliebig viele Balkone.
    Wird von main() und vom Webserver (server/app.py) genutzt."""

    def __init__(self, tile_dir, webapp, wetter=None):
        self.webapp = webapp
        D, BK, V, F, VT, FT, mat, tex = lade_eingaben(tile_dir, webapp)
        self.D, self.BK, self.V, self.F, self.VT, self.FT, self.mat, self.tex = D, BK, V, F, VT, FT, mat, tex
        self.meta = D["meta"]
        self.fac = {f["id"]: f for f in D["facades"]}
        self.mesh = V is not None
        self.tri_baum = (baum_dreiecke(F, VT, FT, mat, tex, os.path.join(webapp, "_cache", "baum_dreiecke.npy"))
                         if self.mesh else None)
        if not self.mesh and not os.path.exists(os.path.join(webapp, "_cache", "punkte.npz")):
            sys.exit("Weder mesh.npz noch punkte.npz – zuerst lod2_pipeline.py ausführen")
        self.P, self.baum = punktwolke(V, F, self.tri_baum, os.path.join(webapp, "_cache", "punkte.npz"))
        # Roh-Wolke für die Balkon-Geometrie, bereinigte Wolke für Schatten und 3D-Modell
        keep = bereinige_wolke(self.P, self.baum, os.path.join(webapp, "_cache", "wolke_bereinigt.npz"))
        self.Ps, self.baums = self.P[keep], self.baum[keep]
        # Für die Balkontiefe an Wänden mit Fotoansicht: Mesh-Dreiecke (dort sind Balkone enthalten)
        self.Pm = self.V[self.F].mean(1) if self.mesh else None
        self.xmin, self.ymin, self.xmax, self.ymax = self.meta["bounds"]
        cx, cy = (self.xmin + self.xmax) / 2, (self.ymin + self.ymax) / 2
        self.to_ll = Transformer.from_crs(self.meta["crs"], "EPSG:4326", always_xy=True)
        lon0, lat0 = self.to_ll.transform(cx, cy)
        lon1, lat1 = self.to_ll.transform(cx, cy + 100)
        self.gn_true = Geod(ellps="WGS84").inv(lon0, lat0, lon1, lat1)[0]   # wahres Azimut der Gitter-Nordrichtung
        self.idx, mid = zeitachse()
        if wetter:
            w, sp, self.wquelle = pvgis_wetter(wetter, lat0, lon0, mid)
        else:
            w, sp, self.wquelle = synth_wetter(lat0, lon0, mid)
        self.sim = Sim(w, sp, mid)
        self.ghi_y = w["ghi"].sum() / 1000
        log(f"Wetter: {self.wquelle}; Globalstrahlung {self.ghi_y:.0f} kWh/m², "
            f"Referenz Dach 35° Süd: {self.sim.referenz_dach():.0f} kWh/kWp")
        self.adir = os.path.join(webapp, "angebot")
        self.tex_names = None
        self.lod2 = None
        lp = os.path.join(webapp, "_cache", "lod2_gebaeude.npz")
        if os.path.exists(lp):
            z = np.load(lp)
            L = {k: z[k] for k in z.files}
            st = L["start"]
            L["mitte"] = np.array([L["P"][st[i]:st[i + 1], :2].mean(0) for i in range(len(st) - 1)])
            pv = {}
            for i, g in enumerate(L["geb"]):
                pv.setdefault(int(g), []).append(i)
            L["polys_von"] = pv
            bv = {}
            for j, g in enumerate(L["boden_geb"]):
                bv.setdefault(int(g), []).append(j)
            L["boden_von"] = bv
            self.lod2 = L
        self.mesh_box = (self.V[:, 0].min() + 20, self.V[:, 1].min() + 20, self.V[:, 0].max() - 20,
                         self.V[:, 1].max() - 20) if self.mesh else None
        self.ortho = None
        o = self.meta.get("ortho")
        if o and os.path.exists(os.path.join(webapp, o["file"])):
            self.ortho = dict(bild=Image.open(os.path.join(webapp, o["file"])).convert("RGB"), res=o["res"],
                              x0=self.xmin, y1=self.ymax)

    def geo_wolke(self, f):
        """Punkte für die Balkongeometrie: Mesh bei Fotoansicht aus dem Mesh, sonst Schatten-Punktwolke.
        Für das Mesh werden die Dreiecke rund um die Wand dicht abgetastet (alle 12 cm), damit Brüstung
        und Balkonplatte sicher getroffen werden – die Mittelpunkte großer Dreiecke allein sind zu grob."""
        if not (self.mesh and f.get("img_quelle") == "mesh"):
            return self.P, self.baum
        if not hasattr(self, "_mesh_lokal"):
            self._mesh_lokal = {}
        if f["id"] in self._mesh_lokal:
            return self._mesh_lokal[f["id"]]
        a, b = np.array(f["a"], float), np.array(f["b"], float)
        lo, hi = np.minimum(a, b) - 6, np.maximum(a, b) + 6
        c = self.Pm
        sel = np.nonzero((c[:, 0] > lo[0]) & (c[:, 0] < hi[0]) & (c[:, 1] > lo[1]) & (c[:, 1] < hi[1]))[0]
        T = self.V[self.F[sel]]
        area = 0.5 * np.linalg.norm(np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]), axis=1)
        k = np.clip(np.ceil(area / 0.12 ** 2), 1, 3000).astype(np.int64)
        idx = np.repeat(np.arange(len(sel)), k)
        rng = np.random.default_rng(0)
        r1, r2 = np.sqrt(rng.random(len(idx))), rng.random(len(idx))
        Q = T[idx, 0] * (1 - r1)[:, None] + T[idx, 1] * (r1 * (1 - r2))[:, None] + T[idx, 2] * (r1 * r2)[:, None]
        out = (np.vstack([Q, T.reshape(-1, 3)]), np.r_[self.tri_baum[sel][idx], np.repeat(self.tri_baum[sel], 3)])
        if len(self._mesh_lokal) > 200:
            self._mesh_lokal.clear()
        self._mesh_lokal[f["id"]] = out
        return out

    def simuliere(self, fid, b, P=None, baum=None):
        """Ein Balkon (Box im Fassadenbild) -> (Geometrie, Ergebnis, Azimut, Randlage) oder None.
        P/baum: optional eine veränderte Punktwolke (z. B. ohne vom Nutzer entfernte Bäume)."""
        P = self.Ps if P is None else P
        baum = self.baums if baum is None else baum
        f = self.fac[int(fid)]
        geo = balkon_geometrie(f, b, *self.geo_wolke(f))
        if geo is None:
            return None
        Q, qb = horizont_vorfilter(geo, P, baum)
        az_mod = (math.degrees(math.atan2(geo["n"][0], geo["n"][1])) + self.gn_true) % 360
        r = simuliere_balkon(self.sim, geo, Q, qb, self.gn_true, az_mod)
        mx, my = geo["mitte"][:2]
        rand = min(mx - self.xmin, self.xmax - mx, my - self.ymin, self.ymax - my) < RAND_WARNUNG
        return geo, r, az_mod, rand

    def bereite_ordner(self):
        """Texturen, Angebotsseite und Lastgang-Modul in webapp/angebot/ bereitstellen."""
        if self.tex_names is not None:
            return
        os.makedirs(os.path.join(self.adir, "tex"), exist_ok=True)
        self.tex_names = []
        for p in self.tex:
            dst = os.path.join(self.adir, "tex", os.path.basename(p))
            if not os.path.exists(dst):
                shutil.copyfile(p, dst)
            self.tex_names.append("tex/" + os.path.basename(p))
        here = os.path.dirname(os.path.abspath(__file__))
        for src, dst in (("angebot.html", "index.html"), ("lastgang.js", "lastgang.js"), ("partner.json", "partner.json")):
            if os.path.exists(os.path.join(here, src)):
                shutil.copyfile(os.path.join(here, src), os.path.join(self.adir, dst))

    def ohne_baeume(self, mitte, kronen, faktor=1.3):
        """Punktwolke ohne die Baumpunkte unter den angegebenen Kronen.
        kronen: [[x, y, r], ...] relativ zur Balkonmitte (Koordinaten wie in modell.json)."""
        weg = np.zeros(len(self.Ps), bool)
        for x, y, r in kronen:
            d = np.hypot(self.Ps[:, 0] - (mitte[0] + x), self.Ps[:, 1] - (mitte[1] + y))
            weg |= self.baums & (d < r * faktor + 0.5)
        return self.Ps[~weg], self.baums[~weg], int(weg.sum())

    def angebot(self, fid, bi, balkone, key=None, szene=False, details=None, P=None, baum=None, extra=None):
        """Angebotsdaten für balkone[bi] auf Fassade fid schreiben -> daten (dict) oder None.
        balkone: alle Balkone dieser Fassade (für das Modell); selbst eingezeichnete einfach anhängen."""
        self.bereite_ordner()
        fid = str(fid)
        fac, sim, gn_true = self.fac, self.sim, self.gn_true
        P = self.Ps if P is None else P
        baum = self.baums if baum is None else baum
        b = balkone[bi]
        if details is None:
            details = self.simuliere(fid, b, P, baum)
        if details is None:
            return None
        geo, r, az_mod, rand = details
        f = fac[int(fid)]
        # Varianten mit 1 bis 4 Modulen, soweit sie auf das Geländer passen
        varianten = []
        fit = geo["vorschlag"]["fit"]
        for nm in (range(1, min(fit, 4) + 1) if fit >= 1 else [1]):
            g2 = balkon_geometrie(f, b, *self.geo_wolke(f), n_module=nm if fit >= 1 else None)
            Q2, qb2 = horizont_vorfilter(g2, P, baum)
            r2 = simuliere_balkon(sim, g2, Q2, qb2, gn_true, az_mod)
            pac2 = np.round(r2["pac"]).astype(int)
            varianten.append(dict(n=g2["vorschlag"]["n"], wp=g2["vorschlag"]["wp"], wp_modul=g2["vorschlag"]["wp_modul"],
                                  kwh=round(float(pac2.sum()) / 1000, 1),
                                  kwh_frei=round(float(r2["pac_frei"].sum()) / 1000, 1),
                                  module=[[lokal(e, geo["mitte"], f["ground"]) for e in m["ecken"]]
                                          for m in g2["module"]],
                                  stunde_wh=pac2.tolist()))
        # optimierte Variante: bifaziale 450-Wp-Module, 30° ausgestellt (Modulneigung 60°)
        nm = min(max(fit, 1), 2)
        g3 = balkon_geometrie(f, b, *self.geo_wolke(f), n_module=nm if fit >= 1 else None)
        Q3, qb3 = horizont_vorfilter(g3, P, baum)
        r3 = simuliere_balkon(sim, g3, Q3, qb3, gn_true, az_mod, neigung=90 - AUSSTELLWINKEL, wp_modul=WP_OPTIMIERT)
        pac3 = np.round(r3["pac"]).astype(int)
        optimiert = dict(n=g3["vorschlag"]["n"], wp=g3["vorschlag"]["n"] * WP_OPTIMIERT, wp_modul=WP_OPTIMIERT,
                         ausstellwinkel=AUSSTELLWINKEL, kwh=round(float(pac3.sum()) / 1000, 1),
                         kwh_frei=round(float(r3["pac_frei"].sum()) / 1000, 1),
                         module=[[lokal(e, geo["mitte"], f["ground"]) for e in m["ecken"]] for m in g3["module"]],
                         stunde_wh=pac3.tolist())
        key = key or f"{fid}_{bi}"
        out = os.path.join(self.adir, key)
        os.makedirs(out, exist_ok=True)
        P0 = geo["mitte"]
        z0 = f["ground"]
        sz = None
        if self.mesh_box and self.mesh_box[0] <= P0[0] <= self.mesh_box[2] and self.mesh_box[1] <= P0[1] <= self.mesh_box[3]:
            szene = True             # Balkon liegt im Mesh: Foto-Ansicht immer mitliefern
        if szene and self.mesh:      # Foto-Mesh (ca. 4 MB) nur für ausgewählte Balkone
            sz = exportiere_szene(out, P0, z0, self.V, self.F, self.VT, self.FT, self.mat, self.tri_baum)
            sz["tex"] = self.tex_names
        alt = os.path.join(out, "daten.json")
        if sz is None and os.path.exists(alt) and os.path.exists(os.path.join(out, "scene.bin")):
            sz = json.load(open(alt, encoding="utf-8")).get("szene")   # früher erzeugtes Foto-Mesh weiter nutzen
        if extra and extra.get("ohne_baeume"):
            # Modell aus der vollständigen Punktwolke, dann genau die angeklickten Kronen weglassen
            # (sonst entstehen am Rand der entfernten Krone neue kleine Kronen)
            modell = exportiere_modell(out, P0, z0, f, balkone, self.Ps, self.baums, aktiv_bi=bi, geo_wolke=self.geo_wolke(f), ortho=self.ortho, lod2=self.lod2)
            mp = os.path.join(out, "modell.json")
            mj = json.load(open(mp, encoding="utf-8"))
            weg = extra["ohne_baeume"]
            mj["baeume"] = [t for t in mj["baeume"] if all(math.hypot(t[0] - x, t[1] - y) > 0.6 for x, y, r in weg)]
            with open(mp, "w", encoding="utf-8") as fh:
                json.dump(mj, fh, separators=(",", ":"))
            modell["baeume"] = len(mj["baeume"])
        else:
            modell = exportiere_modell(out, P0, z0, f, balkone, P, baum, aktiv_bi=bi, geo_wolke=self.geo_wolke(f), ortho=self.ortho, lod2=self.lod2)
        lon, lat = self.to_ll.transform(P0[0], P0[1])
        idx = self.idx
        pac = np.round(r["pac"]).astype(int)
        frei = np.round(r["pac_frei"]).astype(int)
        mon = pd.Series(pac, index=idx).groupby(idx.month).sum() / 1000
        mon_f = pd.Series(frei, index=idx).groupby(idx.month).sum() / 1000
        mv = geo["vorschlag"]
        daten = dict(
            id=key, fassade=int(fid), balkon=bi, typ=b["typ"], geschoss=b.get("geschoss"),
            sicherheit=b.get("sicherheit"), breite_m=b["breite_m"], hoehe_m=b["hoehe_m"],
            unterkante_m=b.get("unterkante_m"), lat=round(lat, 6), lon=round(lon, 6),
            az=round(az_mod, 1), fassade_cls=f["cls"], gitter_nord=round(gn_true, 3),
            tiefe_m=round(geo["tiefe"], 2), rand=bool(rand), quelle_balkon=b.get("quelle", "erkannt"),
            gelaender=b.get("gelaender"),
            vorschlag=dict(n=mv["n"], wp=mv["wp"], wp_modul=mv["wp_modul"], text=mv["text"],
                           w=mv["w"], h=mv["h"], fit=mv["fit"]),
            module=[[lokal(e, P0, z0) for e in m["ecken"]] for m in geo["module"]],
            normale=[round(float(geo["n"][0]), 4), 0.0, round(float(-geo["n"][1]), 4)],
            fassade_linie=[lokal([*f["a"], z0], P0, z0), lokal([*f["b"], z0], P0, z0)],
            hoehe_modul_m=round(float(P0[2] - z0), 1),
            horizont=dict(schritt=AZ_STEP, gebaeude=[round(float(x), 1) for x in r["hb"]],
                          baeume=[round(float(x), 1) for x in r["ht"]]),
            jahr=dict(kwh=round(float(pac.sum()) / 1000, 1), kwh_frei=round(float(frei.sum()) / 1000, 1),
                      kwh_kwp=round(float(pac.sum()) / mv["wp"], 0),
                      sonnenstunden=round(r["sonne_mod"]), sonnenstunden_frei=round(r["sonne_frei"]),
                      himmel=round(r["himmel"], 2)),
            monat=dict(kwh=[round(float(x), 1) for x in mon], kwh_frei=[round(float(x), 1) for x in mon_f]),
            stunde_wh=pac.tolist(),
            stunde_frei_wh=frei.tolist(),
            varianten=varianten,
            optimiert=optimiert,
            sonne_anteil=[round(float(x), 2) for x in r["frac"]],
            jahr_basis=JAHR, wetter=self.wquelle, globalstrahlung=round(self.ghi_y),
            annahmen=dict(verluste=VERLUSTE, gamma=GAMMA, wr_ac=WR_AC, wr_eta=WR_ETA, albedo=ALBEDO,
                          tau_belaubt=TAU_BELAUBT, tau_kahl=TAU_KAHL),
            szene=sz, modell=modell, fassade_foto=fassaden_foto(out, f, self.webapp, P0, z0),
            wand_ansicht=wand_ansicht(out, f, self.webapp, b, geo), quelle=self.meta.get("source"), erstellt=time.strftime("%Y-%m-%d %H:%M"),
        )
        if extra:
            daten.update(extra)
        with open(os.path.join(out, "daten.json"), "w", encoding="utf-8") as fh:
            json.dump(daten, fh, ensure_ascii=False, separators=(",", ":"))
        if sz:
            log(f"Angebot {key}: {daten['jahr']['kwh']:.0f} kWh/a (frei {daten['jahr']['kwh_frei']:.0f}), "
                f"Foto-Szene {sz['vertices']:,} Punkte")
        return daten


def balkon_aus_eingabe(f, box, geschoss=None, gelaender=None):
    """Selbst eingezeichneter Balkon (Box in Pixeln des Fassadenbilds) im Format der Erkennung."""
    res = f.get("img_res")
    x0, y0, x1, y1 = [float(v) for v in box]
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    b = dict(box=[round(x0), round(y0), round(x1), round(y1)], typ="Balkon", geschoss=geschoss,
             sicherheit=1.0, breite_m=round((x1 - x0) * res, 2), hoehe_m=round((y1 - y0) * res, 2),
             quelle="selbst eingezeichnet", gelaender=gelaender)
    if f.get("img_ztop") is not None:
        b["unterkante_m"] = round(f["img_ztop"] - y1 * res - f["ground"], 1)
        if geschoss is None:      # Balkonplatte liegt etwa auf Geschosshöhe (≈ 3,1 m je Geschoss im Altbau/Neubau-Mittel)
            b["geschoss"] = max(0, int(round(b["unterkante_m"] / 3.1)))
    return b


def schreibe_angebotsliste(webapp):
    """Liste vorhandener Angebotsseiten (für Explorer und Auswahl)."""
    adir = os.path.join(webapp, "angebot")
    vorhanden = []
    for name in sorted(os.listdir(adir)):
        p = os.path.join(adir, name, "daten.json")
        if os.path.exists(p) and "_u" not in name and "_b" not in name:   # eigene/geänderte Balkone bleiben privat
            d = json.load(open(p, encoding="utf-8"))
            vorhanden.append(dict(id=name, fassade=d["fassade"], balkon=d["balkon"], typ=d["typ"],
                                  geschoss=d.get("geschoss"), kwh=d["jahr"]["kwh"],
                                  verlust=round(1 - d["jahr"]["kwh"] / max(d["jahr"]["kwh_frei"], 1e-6), 3)))
    with open(os.path.join(webapp, "angebote.js"), "w", encoding="utf-8") as fh:
        fh.write("window.ANGEBOTE = " + json.dumps(vorhanden, ensure_ascii=False) + ";\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tile_dir")
    ap.add_argument("webapp")
    ap.add_argument("--szene", action="append", default=[], help="fassade:balkon, mehrfach möglich")
    ap.add_argument("--wetter", help="PVGIS-TMY-Datei (JSON/CSV)")
    ap.add_argument("--nur-szenen", action="store_true", help="nur die angegebenen Balkone rechnen")
    ap.add_argument("--alle-angebote", action="store_true",
                    help="Angebotsseiten für alle passenden Balkone erzeugen (Foto-Mesh nur für --szene)")
    a = ap.parse_args()

    R = Rechner(a.tile_dir, a.webapp, a.wetter)
    BK = R.BK
    szenen = set()
    for s in a.szene:
        fi, bi = s.split(":")
        szenen.add((fi, int(bi)))

    ergebnis = {}
    jobs = []
    for fid, entry in BK.items():
        for bi, b in enumerate(entry.get("balkone", [])):
            if a.nur_szenen and (fid, bi) not in szenen:
                continue
            jobs.append((fid, bi, b))
    log(f"Simuliere {len(jobs)} Balkone")
    t0 = time.time()
    details = {}
    for k, (fid, bi, b) in enumerate(jobs):
        d = R.simuliere(fid, b)
        if d is None:
            ergebnis.setdefault(fid, {})[str(bi)] = dict(passt=False)
            continue
        geo, r, az_mod, rand = d
        kwh = r["pac"].sum() / 1000
        kwh_frei = r["pac_frei"].sum() / 1000
        ergebnis.setdefault(fid, {})[str(bi)] = dict(
            passt=True, kwh=round(kwh), kwh_frei=round(kwh_frei),
            verlust=round(1 - kwh / max(kwh_frei, 1e-6), 3), wp=geo["vorschlag"]["wp"],
            n=geo["vorschlag"]["n"], az=round(az_mod, 1), rand=bool(rand),
            sonne=round(r["sonne_mod"]), himmel=round(r["himmel"], 2))
        details[(fid, bi)] = d
        if (k + 1) % 20 == 0:
            log(f"  {k + 1}/{len(jobs)} ({time.time() - t0:.0f} s)")

    # Kennzahlen für den Fassaden-Explorer
    if not a.nur_szenen:
        with open(os.path.join(a.webapp, "ertrag.js"), "w", encoding="utf-8") as fh:
            fh.write("window.ERTRAG = ")
            json.dump(dict(wetter=R.wquelle, erstellt=time.strftime("%Y-%m-%d %H:%M"), balkone=ergebnis),
                      fh, ensure_ascii=False, separators=(",", ":"))
            fh.write(";\n")
        log(f"ertrag.js geschrieben ({sum(len(v) for v in ergebnis.values())} Balkone)")
        rows = [(fid, bi, v) for fid, d in ergebnis.items() for bi, v in d.items() if v.get("passt")]
        rows.sort(key=lambda r: -r[2]["kwh"])
        print("\nfassade:balkon  typ       Wp   kWh/a  frei  Verlust  Rand")
        for fid, bi, v in rows[:25]:
            typ = BK[fid]["balkone"][int(bi)]["typ"]
            print(f"{fid:>7}:{bi:<7} {typ:<8} {v['wp']:>4} {v['kwh']:>6} {v['kwh_frei']:>5}  "
                  f"{v['verlust'] * 100:5.0f} %  {'ja' if v['rand'] else ''}")

    if not szenen and not a.alle_angebote:
        return

    angebote = set(szenen) | (set(details) if a.alle_angebote else set())
    log(f"Erzeuge {len(angebote)} Angebotsseiten")
    for fid, bi in sorted(angebote, key=lambda k: (int(k[0]), k[1])):
        if (fid, bi) not in details:
            log(f"Balkon {fid}:{bi} nicht gefunden oder zu schmal")
            continue
        R.angebot(fid, bi, BK[fid]["balkone"], szene=(fid, bi) in szenen, details=details[(fid, bi)])
    schreibe_angebotsliste(a.webapp)


if __name__ == "__main__":
    main()
