# bkw_finden

Findet Balkone mit Potenzial für Balkonkraftwerke – automatisch aus dem offenen 3D-Stadtmodell von Berlin.

![Karte mit Südfassaden und Balkonanzahl](docs/karte.jpg)

![Fassadenansicht mit Balkon, Maßen und Modulvorschlag](docs/balkon.jpg)

![Erkannte Loggien und Balkone mit berechneter Breite](docs/erkennung.jpg)

## Ablauf

1. **Fassaden** – aus einer Mesh-Kachel werden Gebäudeumrisse und Höhensprünge abgeleitet, jede Wand bekommt ihre Himmelsrichtung.
2. **Frontalansichten** – je Fassade wird ein Bild aus dem Mesh gerendert, Bäume davor werden ausgeblendet.
3. **Balkone** – Claude markiert Balkone und Loggien per Batch API; Breite und Höhe werden aus dem Bildmaßstab berechnet.
4. **Webseite** – Karte, Filter nach Ausrichtung, Eintauch-Ansicht je Fassade mit Modulvorschlag und Ertragsschätzung.

## Nutzung

```bash
pip3 install -r requirements.txt anthropic

# 1. Fassaden aus einer Kachel des Berlin 3D Downloadportals
python3 pipeline/fassaden_pipeline.py data/<kachel> webapp

# 2. Balkone erkennen (API-Key in .env: ANTHROPIC_API_KEY=...)
python3 pipeline/balkone_claude.py submit --classes S
python3 pipeline/balkone_claude.py collect

# 3. Webseite starten
webapp/start.command
```

## Stand

- Eine Kachel in Berlin-Wilmersdorf, alle 95 Südfassaden ausgewertet (Sonnet 5.5, 0,26 $).
- Maße ±0,3–0,5 m. Ertrag ohne Verschattung – das ist der nächste Schritt.
