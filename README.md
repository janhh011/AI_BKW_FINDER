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
- Fassaden zwischen aneinandergebauten Häusern unterschiedlicher Höhe
  fehlen noch.
- Sauberere Umrisse wären mit dem LoD2-Gebäudemodell möglich (geplant).

## Nächste Schritte

- Balkonerkennung auf den Frontalansichten (multimodales LLM oder
  Zero-Shot-Detektor als Start, später eigenes YOLO-Modell)
- Verschattung durch Nachbargebäude über das Jahr
- LoD2-Umrisse statt Mesh-Umrisse

Rohdaten und erzeugte Ausgaben sind nicht im Repository (siehe .gitignore).
