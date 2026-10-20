#!/bin/bash
# LoD2-Gebäudemodelle Berlin (1-km-Kacheln, dl-de/zero 2.0) für ein Rechteck laden.
# Standard: Innenstadt (Charlottenburg bis Friedrichshain, Wedding bis Neukölln).
# Nutzung: pipeline/lod2_download.sh [ZIELORDNER] [OST_VON OST_BIS NORD_VON NORD_BIS]
ZIEL="${1:-$HOME/Downloads/lod2}"
E0="${2:-382}"; E1="${3:-396}"; N0="${4:-5813}"; N1="${5:-5825}"
BASIS="https://gdi.berlin.de/data/a_lod2/atom"
mkdir -p "$ZIEL"
n=0; neu=0; fehlt=0
for e in $(seq "$E0" "$E1"); do
  for nn in $(seq "$N0" "$N1"); do
    n=$((n + 1))
    f="LoD2_${e}_${nn}.zip"
    if [ -s "$ZIEL/$f" ]; then continue; fi
    if curl -fsS -o "$ZIEL/$f.teil" "$BASIS/$f"; then
      mv "$ZIEL/$f.teil" "$ZIEL/$f"; neu=$((neu + 1)); echo "geladen: $f"
    else
      rm -f "$ZIEL/$f.teil"; fehlt=$((fehlt + 1))
    fi
  done
done
echo "Fertig: $n Kacheln geprüft, $neu neu geladen, $fehlt nicht vorhanden."
du -sh "$ZIEL"
