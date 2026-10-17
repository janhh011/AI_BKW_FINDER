# bkw_finden

Findet Balkone und Fassaden mit hohem Potenzial für Balkonkraftwerke
in Berlin, auf Basis des offenen 3D-Meshmodells der Stadt.

## Stand: Schritt 1 – Fassaden und Ausrichtung

Die Pipeline nimmt eine Kachel des Berliner 3D-Meshmodells (OBJ + Texturen),
findet alle Gebäudefassaden, bestimmt ihre Himmelsrichtung und erzeugt eine
lokale Webseite mit Luftbild und Fassaden-Overlays. Südost, Süd und Südwest
sind farbig markiert. Für jede Fassade wird eine Frontalansicht aus dem Mesh
gerendert, als Material für die Balkonerkennung in Schritt 2.

Kein KI-Modell: klassische Geometrie- und Bildverarbeitung
(numpy, OpenCV, scipy, pyproj).

### Ablauf

1. Mesh laden, Flächennormalen berechnen
2. Von oben rastern (eigener Z-Buffer-Renderer): Oberflächenmodell + Luftbild
3. Geländemodell per morphologischem Opening schätzen, Höhe über Gelände
4. Gebäude von Bäumen trennen (Höhe, Rauigkeit, Grünindex)
5. Gebäudeumrisse (OpenCV-Konturen, vereinfacht) = Fassadenkanten
6. Je Fassade: Außennormale, wahre Himmelsrichtung (Meridiankonvergenz
   korrigiert), Höhe, Wandnachweis gegen die Wanddreiecke im Mesh
7. Frontalansicht je Fassade (nur Geometrie 1,5 m hinter bis 3 m vor der
   Fassade, dadurch verschwinden die meisten Bäume)
8. Statische Webseite (index.html + data.js), keine externen Abhängigkeiten

## Benutzung

Daten: eine Kachel aus dem Berlin 3D Downloadportal herunterladen
(Ordner mit `.obj`, `.mtl` und `.jpg`), z. B. nach `data/<kachel>/`.

    pip3 install -r requirements.txt
    python3 pipeline/fassaden_pipeline.py data/<kachel> webapp

Danach `webapp/start.command` doppelklicken (macOS) oder:

    cd webapp && python3 -m http.server 8765

und http://localhost:8765 öffnen. Laufzeit ca. 30 s pro Kachel.

## Werte in der Webseite

- Azimut: wahre Himmelsrichtung der Außenseite (180° = Süd)
- Höhe: Höhe über Gelände
- Fläche: Länge × Höhe, grobe Schätzung
- Wandnachweis: Anteil der Fassadenlänge mit echten Wandflächen im Mesh
  in dieser Richtung; niedrig = von Bäumen verdeckt oder Fehlerkennung

## Bekannte Grenzen

- Wo Bäume über Dachkanten ragen, werden Umrisse zackig oder fehlen.
- Sauberere Umrisse wären mit dem LoD2-Gebäudemodell möglich (geplant).

## Schritt 2: Balkonerkennung mit der Claude API

`pipeline/balkone_claude.py` schickt die Frontalansichten per Message Batches API
an Claude. Das Modell meldet nur Boxen, Typ (Balkon/Loggia/unsicher) und Geschoss.
Breite, Höhe und Lage jedes Balkons werden im Code aus der Box und dem bekannten
Bildmaßstab berechnet (Genauigkeit ca. ±0,3–0,5 m).

    pip3 install anthropic
    echo "ANTHROPIC_API_KEY=..." > .env        # wird nicht committet
    python3 pipeline/balkone_claude.py submit --classes S --model sonnet
    python3 pipeline/balkone_claude.py status
    python3 pipeline/balkone_claude.py collect

Ergebnisse landen in `webapp/balkone.json` / `balkone.js`, Prüfbilder in `_analyse/claude/`.

Stand Kachel 3858_58171_-002: alle 95 Südfassaden mit Sonnet 5.5 ausgewertet,
Kosten 0,26 $ (ca. 0,27 US-Cent pro Fassade, Batch-Preise Okt. 2026).

Opus und Sonnet 5.5 unterstützen kein erzwungenes Werkzeug (`tool_choice: tool`),
daher `tool_choice: auto` mit Anweisung im Systemprompt.

### Webseite

Klick auf eine Fassade öffnet eine Eintauch-Ansicht mit der Frontalansicht.
Erkannte Balkone sind anklickbar und zeigen Breite, Höhe, Geschoss, Unterkante,
einen Modulvorschlag (Standardmodul 1,72 × 1,13 m, max. 2 Module) und eine grobe
Ertragsschätzung für senkrecht montierte Module ohne Verschattung.

### Fassadenerkennung: Höhensprünge

Neben Außenwänden erkennt die Pipeline jetzt auch Wände über niedrigeren
Nachbardächern (Höhensprünge). Abdeckung der südgerichteten Wandfläche im Mesh:
84 % (vorher 73 %). Rest: Kachelrand, Wände unter Baumkronen, Stufen unter 3 m.

## Nächste Schritte

- Auswertung auf SO/SW ausweiten
- Verschattung durch Nachbargebäude über das Jahr
- LoD2-Umrisse statt Mesh-Umrisse

Rohdaten und erzeugte Ausgaben sind nicht im Repository (siehe .gitignore).
