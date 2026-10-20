#!/bin/bash
# Offene Geodaten Berlin (Datenlizenz Deutschland – Zero 2.0) nach data/downloads/ laden.
#
#   bash pipeline/geodaten_download.sh dom        # Oberflächenmodell DOM1 (Laser 2021, 1 m) – für Bäume/Höhen
#   bash pipeline/geodaten_download.sh luftbild   # Luftbilder DOP20 2025 (ganz Berlin, ca. 19 GB)
#   bash pipeline/geodaten_download.sh lod2       # LoD2-Gebäude (ruft pipeline/lod2_download.sh auf)
#   bash pipeline/geodaten_download.sh alles
#
# Gebiet für DOM und LoD2 (km-Koordinaten EPSG:25833), Standard = Innenstadt:
#   OST_VON=382 OST_BIS=396 NORD_VON=5813 NORD_BIS=5825 bash pipeline/geodaten_download.sh dom
# Abgebrochene Downloads werden beim nächsten Aufruf fortgesetzt.
set -u
PROJEKT="$(cd "$(dirname "$0")/.." && pwd)"
DL="$PROJEKT/data/downloads"
E0="${OST_VON:-382}"; E1="${OST_BIS:-396}"; N0="${NORD_VON:-5813}"; N1="${NORD_BIS:-5825}"

hole() {   # URL ZIELDATEI
  if [ -s "$2" ]; then echo "vorhanden: $(basename "$2")"; return 0; fi
  if curl -fL --retry 3 -C - -o "$2.teil" "$1"; then mv "$2.teil" "$2"; echo "geladen:   $(basename "$2")"
  else echo "fehlt:     $(basename "$2")"; fi
}

dom() {    # DOM1: 2x2-km-Kacheln, benannt nach der Südwest-Ecke (gerade km-Werte)
  mkdir -p "$DL/dom"
  local e n
  for e in $(seq $((E0 - E0 % 2)) 2 "$E1"); do
    for n in $(seq $((N0 - N0 % 2)) 2 "$N1"); do
      hole "https://gdi.berlin.de/data/dom/atom/DOM1_${e}_${n}.zip" "$DL/dom/DOM1_${e}_${n}.zip"
    done
  done
  du -sh "$DL/dom"
}

luftbild() {   # DOP20 2025: nur als 9 Regionspakete erhältlich -> ganz Berlin
  mkdir -p "$DL/luftbild"
  local r
  for paar in Mitte:Mitte Nord:Nord Nordost:Nordost Nordwest:Nordwest Ost:Ost \
              S%C3%BCd:Sued S%C3%BCdost:Suedost S%C3%BCdwest:Suedwest West:West; do
    hole "https://gdi.berlin.de/data/dop_2025_fruehjahr/atom/${paar%%:*}.zip" "$DL/luftbild/${paar##*:}.zip"
  done
  hole "https://gdi.berlin.de/data/dop_2025_fruehjahr/atom/Blattschnitt2x2km_2025.gif" "$DL/luftbild/Blattschnitt2x2km_2025.gif"
  du -sh "$DL/luftbild"
}

lod2() { bash "$PROJEKT/pipeline/lod2_download.sh" "$DL/lod2" "$E0" "$E1" "$N0" "$N1"; }

case "${1:-}" in
  dom) dom ;;
  luftbild) luftbild ;;
  lod2) lod2 ;;
  alles) lod2; dom; luftbild ;;
  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
