# Styleguide „Solar Blue“ (v1.0, Oktober 2026)

Vollständige Brand Guidelines als PDF: [STYLEGUIDE_Solar-Blue.pdf](STYLEGUIDE_Solar-Blue.pdf).
Hier die wichtigsten Werte zum direkten Übernehmen in den Code.

## Farben

| Name | Hex | Einsatz |
|---|---|---|
| Navy | `#13243A` | Text, Logo, dunkle Flächen |
| Solar Blue | `#2E6BF0` | Primär: Buttons, Links, Panel |
| Ersparnis | `#19B37A` | Nur für €- und CO₂-Werte |
| Sonne | `#FFC233` | Highlight, Sonnenstunden |
| Mist | `#F2F5FA` | Seitengrund, Karten |

Tints: Blue 10 % `#EAF0FE` · Ersparnis-Tint `#E3F6EE` · Sonne-Tint `#FFF4D6`
Verteilung: Mist 50 · Weiß 22 · Navy 16 · Solar Blue 8 · Ersparnis 2 · Sonne 2

**Kontrast (WCAG)**

- Navy auf Weiß / Mist: 15 : 1, für jeden Text
- Weiß auf Solar Blue: 4,7 : 1, für Buttons und Text ab 16 px
- Navy auf Sonne: 9,7 : 1, für Badges
- Ersparnis-Grün auf Weiß: 2,7 : 1, nur als Fläche, nie als Text
- Grüner Text auf Ersparnis-Tint: `#0B6B49`, 6 : 1

```css
:root {
  --navy: #13243A;
  --solar-blue: #2E6BF0;
  --ersparnis: #19B37A;
  --sonne: #FFC233;
  --mist: #F2F5FA;
  --blue-10: #EAF0FE;
  --ersparnis-tint: #E3F6EE;
  --ersparnis-text: #0B6B49;
  --sonne-tint: #FFF4D6;
}
```

## Typografie

- **Sora** (400 / 600): Überschriften, Zahlen, Logo
- **Figtree** (400 / 600): Fließtext und alles, was gelesen wird

Beide stehen unter der SIL Open Font License.

| Stil | Schrift | Größe / Zeilenhöhe | Laufweite |
|---|---|---|---|
| Display | Sora 600 | 64 / 64 | −4 % |
| H1 | Sora 600 | 40 / 44 | −3 % |
| H2 | Sora 600 | 28 / 34 | −2 % |
| Zahl / KPI | Sora 600 | 48, tabellarische Ziffern | |
| Fließtext | Figtree 400 | 16 / 24 | |
| Klein / Label | Figtree 600 | 13 / 18 | |

## Komponenten

- 8-px-Raster. Abstände: 4 · 8 · 16 · 24 · 40 · 64
- Radius: 8 px für Buttons und Felder, 12 px für Karten, 22 px für das App-Icon (6 px für kleine Elemente)
- Eine Hauptaktion pro Screen, immer in Solar Blue

## Logo

- Bildmarke: isometrischer Balkon mit Solarmodul. Wortmarke in Sora.
- Das Panel ist immer Solar Blue. Logo nie auf Solar-Blue-Fläche setzen.
- Mindestgröße: Bildmarke allein ab 28 px (8 mm Druck), mit Wortmarke ab 140 px Breite (35 mm Druck). Unter 32 px entfallen die Panel-Linien.
- Nicht erlaubt: Panel umfärben, verzerren, Schatten, Verläufe oder Effekte.

## Icons & Bild

- Linien-Icons, 2 px, runde Enden, 24-px-Raster
- Grafiken in Isometrie (30°), Panels immer Solar Blue
- Fotos: echte Berliner Balkone bei Tageslicht, keine Stock-Models

## Tonalität

- **Du, nicht Sie:** nahbar wie ein Nachbar, der sich auskennt
- **Zahl zuerst:** erst das Ergebnis, dann die Erklärung
- **Ehrlich rechnen:** Annahmen immer offen zeigen

> So: „Dein Balkon spart rund [000] € im Jahr. Hier siehst du, wie wir darauf kommen.“
> Nicht so: „Durch die Installation einer Steckersolargeräte-Lösung lassen sich Einsparpotenziale realisieren.“
