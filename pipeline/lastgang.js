// Minutengenauer Haushalts-Lastgang (synthetisch, reproduzierbar)
// =================================================================
// Ein gemittelter Standardlastgang (BDEW H25, stündlich) ist der Durchschnitt vieler Haushalte und
// daher glatt: Er "verbraucht" mittags immer ein bisschen und überschätzt den Eigenverbrauch eines
// Balkonkraftwerks. Ein echter Haushalt hat eine Grundlast (Kühlschrank, Router, Standby) und
// darüber kurze, hohe Spitzen (Wasserkocher, Herd, Waschmaschine) zu typischen Tageszeiten.
// Dieses Modul erzeugt einen solchen Haushalt minutengenau für ein Jahr:
//   - Grundlast: Standby + Kühlschrank-Kompressor im Takt
//   - Licht nach Sonnenauf-/-untergang (Berlin)
//   - Morgenroutine, Mittag-/Abendessen, Spül- und Waschmaschine, TV/PC, Staubsauger
//   - Werktag/Wochenende, optional "tagsüber zu Hause" (Homeoffice, Rente, Elternzeit)
//   - zwei Wochen Sommerurlaub (nur Grundlast)
// Anschließend werden die zeitabhängigen Verbraucher so skaliert, dass der Jahresverbrauch stimmt.
// Zeitachse: Minute 0 = 1. Januar 00:00 MEZ (wie die stündlichen PV-Werte aus angebot.py, 8760 h).

export const MINUTEN = 525600;

function rng(seed) {             // mulberry32
  let a = seed >>> 0;
  return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ t >>> 15, t | 1); t ^= t + Math.imul(t ^ t >>> 7, t | 61); return ((t ^ t >>> 14) >>> 0) / 4294967296; };
}

// zuhause: Anteil der Werktage, an denen tagsüber jemand da ist (0 = nie, 0,4 = z. B. zwei Homeoffice-Tage, 1 = immer)
export function lastgang({kwh = 2500, personen = 2, zuhause = 0.4, jahr = 2025, seed = 11} = {}) {
  const R = rng(seed + personen * 101 + Math.round(zuhause * 10) * 7);
  const uni = (a, b) => a + (b - a) * R();
  const tage = 365, L = tage * 1440;
  const basis = new Float32Array(L), flex = new Float32Array(L);
  const add = (arr, start, dauer, w) => { const s = Math.max(0, Math.round(start)), e = Math.min(L, s + Math.round(dauer)); for (let i = s; i < e; i++) arr[i] += w; };
  // getaktet (Herd, Backofen): an/aus im Wechsel
  const takt = (start, dauer, w, an = 0.6, periode = 6) => { for (let t = 0; t < dauer; t += periode) add(flex, start + t, Math.min(periode * an, dauer - t), w); };
  const p = Math.max(1, Math.min(5, personen));
  const wt0 = new Date(Date.UTC(jahr, 0, 1)).getUTCDay();              // 0 = Sonntag
  const urlaub = Math.floor(uni(182, 225));                            // 14 Tage zwischen Juli und Mitte August

  // Grundlast: Standby/Router/Kleingeräte + Kühlschrank (Kompressor ~90 W, taktend)
  const standby = 30 + 8 * p;
  for (let i = 0; i < L; i++) basis[i] = standby;
  for (let t = uni(0, 40); t < L;) { const an = uni(12, 20); add(basis, t, an, 85 + 10 * Math.min(p, 3)); t += an + uni(25, 45); }

  for (let d = 0; d < tage; d++) {
    const t0 = d * 1440, wtag = (wt0 + d) % 7, we = wtag === 0 || wtag === 6;
    if (d >= urlaub && d < urlaub + 14) continue;
    const hoffice = !we && R() < zuhause, daheim = we || hoffice;
    // Tageslicht in Berlin (MEZ): Tageslänge 7,6 h bis 16,8 h, Mittag ≈ 12:10
    const lang = 12.2 + 4.6 * Math.sin(2 * Math.PI * (d - 79) / 365);
    const auf = (12.15 - lang / 2) * 60, unter = (12.15 + lang / 2) * 60;
    const wach = (we ? uni(7.5, 9.5) : uni(5.75, 7.25)) * 60;
    const bett = (we ? uni(23, 24.5) : uni(22.25, 23.5)) * 60;
    const weg = !daheim ? uni(7.25, 8.25) * 60 : null, zurueck = !daheim ? uni(16.5, 18.5) * 60 : null;
    // Licht (in MEZ gerechnet; die Sommerzeit verschiebt unten den ganzen Tag)
    const licht = 35 + 25 * p;
    if (wach < auf) add(flex, t0 + wach, Math.min(auf + 30, weg ?? 1e9) - wach, licht);
    const abend = Math.max(unter - 30, daheim ? 0 : zurueck);
    if (abend < bett) add(flex, t0 + abend, bett - abend, licht * (abend < 20 * 60 ? 1.2 : 1));
    // Morgenroutine
    add(flex, t0 + wach + uni(5, 15), 3, 2000);                                      // Wasserkocher / Kaffee
    if (R() < 0.4) add(flex, t0 + wach + uni(10, 25), 3, 900);                       // Toaster
    for (let k = 0; k < p; k++) if (R() < 0.35) add(flex, t0 + wach + uni(15, 45), 5, 1600);   // Föhn
    // Mittag nur, wenn jemand da ist
    if (daheim && R() < 0.7) takt(t0 + uni(11.75, 13.25) * 60, uni(20, 40), 1800);
    if (daheim && R() < 0.5) add(flex, t0 + uni(14.5, 16.5) * 60, 3, 2000);          // Kaffee am Nachmittag
    // Abendessen
    const essen = (daheim ? uni(17.5, 19.5) : Math.max(zurueck + 20, uni(18, 19.75) * 60) / 60) * 60;
    if (R() < 0.85) takt(t0 + essen, uni(25, 55), 2000, R() < 0.25 ? 0.75 : 0.55);
    if (R() < 0.4) add(flex, t0 + uni(7, 22) * 60, 4, 1000);                         // Mikrowelle
    // TV / Unterhaltung, PC
    const tv = Math.max(essen + 45, uni(19.25, 20.75) * 60);
    if (R() < 0.85) add(flex, t0 + tv, Math.max(30, bett - tv - uni(0, 30)), 90 + 15 * p);
    if (we && R() < 0.35) add(flex, t0 + uni(14, 17) * 60, uni(60, 150), 110);
    if (hoffice) add(flex, t0 + uni(8.25, 9.25) * 60, uni(6.5, 8.5) * 60, 95);   // Homeoffice-Arbeitsplatz
    add(flex, t0 + uni(18, 22) * 60, uni(60, 150), 45);                              // Laptop/Handys laden
    // Waschmaschine: ~1,5 + 1,2 je weitere Person pro Woche
    if (R() < (1.5 + 1.2 * (p - 1)) / 7) {
      const s = t0 + (daheim ? uni(9, 17) : (R() < 0.3 ? uni(6, 7.5) : uni(18.5, 21))) * 60;
      add(flex, s, 15, 2100); add(flex, s + 15, 60, 150); add(flex, s + 75, 10, 450);
    }
    // Geschirrspüler (ab 2 Personen fast täglich)
    if (R() < Math.min(0.9, 0.25 + 0.2 * p)) {
      const s = t0 + ((daheim && R() < 0.35) ? uni(13, 15.5) : uni(19.5, 22)) * 60;
      add(flex, s, 18, 2000); add(flex, s + 18, 50, 70); add(flex, s + 68, 12, 2000);
    }
    if (we && R() < 0.3 || !we && daheim && R() < 0.12) add(flex, t0 + uni(10, 17) * 60, uni(15, 30), 800);   // Staubsauger
    if (R() < 0.15 * p) add(flex, t0 + uni(9, 21) * 60, uni(10, 40), 1200);                                    // Bügeln, Backofen extra …
  }

  // auf Jahresverbrauch skalieren (nur zeitabhängige Verbraucher)
  let sb = 0, sf = 0;
  for (let i = 0; i < L; i++) { sb += basis[i]; sf += flex[i]; }
  const ziel = kwh * 60000;                          // Wmin
  const k = Math.max(0.3, (ziel - sb) / Math.max(sf, 1));
  // MEZ -> Zeitachse; Sommerzeit: lokale Uhrzeit = MEZ + 1 h, d. h. alle Gewohnheiten 60 min früher in MEZ
  const ms = (m, d) => Date.UTC(jahr, m, d);
  const letzterSo = m => { const t = new Date(Date.UTC(jahr, m + 1, 0)); return t.getUTCDate() - t.getUTCDay(); };
  const sz0 = Math.round((ms(2, letzterSo(2)) - ms(0, 1)) / 60000) + 120, sz1 = Math.round((ms(9, letzterSo(9)) - ms(0, 1)) / 60000) + 120;
  const out = new Float32Array(MINUTEN);
  for (let i = 0; i < MINUTEN; i++) {
    const j = Math.min(L - 1, i >= sz0 && i < sz1 ? i + 60 : i);
    out[i] = basis[i] + flex[j] * k;
  }
  return out;
}

// stündliche PV-Mittelwerte (W) -> Minuten, linear zwischen Stundenmitten
export function pvMinuten(pvStunde) {
  const n = pvStunde.length, out = new Float32Array(n * 60);
  for (let h = 0; h < n; h++) {
    const a = pvStunde[h], vor = h ? pvStunde[h - 1] : a, nach = h < n - 1 ? pvStunde[h + 1] : a;
    for (let m = 0; m < 60; m++) {
      const x = (m + 0.5) / 60 - 0.5;
      out[h * 60 + m] = Math.max(0, x < 0 ? a + (a - vor) * x : a + (nach - a) * x);
    }
  }
  // energieerhaltend korrigieren (Spitzen werden sonst leicht abgeschnitten)
  for (let h = 0; h < n; h++) {
    let s = 0; for (let m = 0; m < 60; m++) s += out[h * 60 + m];
    const f = s > 0 ? pvStunde[h] * 60 / s : 0;
    for (let m = 0; m < 60; m++) out[h * 60 + m] *= f;
  }
  return out;
}

// Eigenverbrauch (kWh/Jahr) inkl. optionalem Speicher, minutengenau
export function eigenverbrauch(pvMin, last, {kapKwh = 0, maxW = 800, eta = 0.92, standbyW = 6} = {}) {
  let direkt = 0, ausSpeicher = 0, soc = 0;
  const cap = kapKwh * 60000;                       // Wmin
  for (let i = 0; i < pvMin.length; i++) {
    const p = pvMin[i], l = last[i];
    if (p >= l) {
      direkt += l;
      if (cap) soc = Math.min(cap, soc + Math.min(p - l, maxW) * eta);
    } else {
      direkt += p;
      if (cap && soc > 0) { const d = Math.min((l - p) / eta, soc, maxW / eta); soc -= d; ausSpeicher += d * eta; }   // Entladeverluste
    }
  }
  let kwh = (direkt + ausSpeicher) / 60000;
  if (cap) kwh -= standbyW * 8.76;
  return Math.max(0, kwh);
}
