# Balki-Strom

Findet Balkone mit Potenzial für Balkonkraftwerke im 3D-Stadtmodell von Berlin und erstellt daraus ein persönliches Angebot – mit Verschattung, Preis und Amortisation.

<p align="center">
  <img src="docs/1_karte.png" width="32%" alt="Karte: Fassade mit erkannten Balkonen und Modulvorschlag">
  <img src="docs/2_laden.png" width="32%" alt="Ladebildschirm: Angebot wird erstellt">
  <img src="docs/3_angebot.png" width="32%" alt="Angebotsseite mit 3D-Modell, Ausstattungen und Preisen">
</p>
<p align="center"><sub>Balkon auf der Karte wählen → Angebot wird erstellt → Angebot mit 3D-Modell, Sonnenstand und Preisen</sub></p>

## Ablauf

1. **Fassaden** – aus einer Mesh-Kachel werden Gebäudeumrisse abgeleitet, jede Wand bekommt ihre Himmelsrichtung und eine Frontalansicht.
2. **Balkone** – Claude markiert Balkone und Loggien per Batch API; Maße kommen aus dem Bildmaßstab.
3. **Sonne und Schatten** – für jeden Modulpunkt wird der Horizont aus Nachbarhäusern und Bäumen vermessen und der Ertrag stündlich über ein Jahr simuliert (pvlib, Lastprofil BDEW H25).
4. **Angebot** – drei Ausstattungen mit Preis, Ersparnis und Amortisation, dazu ein 3D-Hausmodell mit Sonnenverlauf und Live-Leistung.

## Nutzung

```bash
pip3 install -r requirements.txt anthropic

# 1. Fassaden aus einer Kachel des Berlin 3D Downloadportals
python3 pipeline/fassaden_pipeline.py data/<kachel> webapp

# 2. Balkone erkennen (API-Key in .env: ANTHROPIC_API_KEY=...)
python3 pipeline/balkone_claude.py submit --classes S
python3 pipeline/balkone_claude.py collect

# 3. Ertrag simulieren und Angebotsseiten erzeugen
python3 pipeline/angebot.py data/<kachel> webapp --alle-angebote

# 4. Webseite starten
webapp/start.command
```

## Stand

- Eine Kachel in Berlin-Wilmersdorf: 95 Südfassaden ausgewertet, 111 Balkone mit Angebot.
- Wetter aus einem synthetischen Referenzjahr (PVGIS-TMY über `--wetter` einlesbar); Ertrag etwa ±15 %, Maße ±0,3–0,5 m.
- Preise sind Platzhalter.

## Warum ich das baue

Balkonkraftwerke sind günstig, legal und in einer Stunde montiert – trotzdem hat die Mehrheit keins. Meine Hypothese: Es scheitert selten an der Technik, sondern am Weg dorthin. Wer sich nicht für Solar interessiert, recherchiert weder Wattzahlen noch Halterungen oder Ausrichtung.

Deshalb drehe ich den Prozess um: Statt dass Menschen ein passendes Set suchen müssen, findet die Software den passenden Balkon. Ausrichtung, Maße und Ertrag sind schon berechnet, bevor jemand überhaupt fragt. Daraus entsteht ein individuelles Angebot für genau diesen Balkon, mit konkretem Modulvorschlag und einer Vorabrechnung, was es spart.

Parallel zu diesem Repo baue ich genau diese individualisierten Angebote. Mich interessiert dabei die übergeordnete Frage, wie sich KI und offene Daten nutzen lassen, um physische Produkte wie Solarmodule und Balkonkraftwerke vorab auf eine konkrete Person zuzuschneiden. So wird aus einem Standardprodukt ein Angebot, das man nicht mehr selbst recherchieren muss.

Das Projekt ist ein laufendes Experiment und kein fertiges Produkt. Feedback und Gespräche sind willkommen.

– Jan-Hendrik Höltkemeier · M.Sc. IMES, TU Berlin
