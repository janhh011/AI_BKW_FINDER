# Backlog

Stand: 23.10.2026. Quelle: Review (Sicherheit, Architektur, Robustheit/Datenschutz). Bereits erledigt ist unten aufgeführt.

## Vor dem öffentlichen Start

- [ ] **Impressum und Datenschutzerklärung** (Hetzner/OVH und Anthropic als Auftragsverarbeiter nennen; Speicherung eingezeichneter Balkone und Klicks mit Löschfrist beschreiben, `server/app.py`-Docstring „keine personenbezogenen Daten“ korrigieren).
- [ ] **Schriften selbst hosten** (Sora, Figtree statt Google Fonts) – überträgt sonst IP-Adressen an Google.
- [ ] **Produktbilder**: Shopify-Bilder sind ohne Lizenz eingebunden → Bilder/Feed aus dem Partnerprogramm oder eigene Illustrationen.
- [ ] **Preise**: fest eingetragen („Stand 19.10.2026“) → Partner-Feed, Datum und Versandkosten sichtbar, verkaufenden Shop nennen.
- [ ] **Werbekennzeichnung**: Partnerhinweis gut lesbar (Kontrast ≥ 4,5:1) und auch an den Produktlinks in den Karten; Auswahl nur eines Shops offenlegen.
- [ ] **Garantie-Angaben** je Komponente prüfen (Wechselrichter fehlt teils, Speicher 10 Jahre).
- [ ] **Mesh-Lizenz** (Berlin 3D) vor öffentlicher/kommerzieller Nutzung klären; bis dahin Mesh nur in der Demo.
- [ ] **Content-Security-Policy**: Inline-Skripte in Dateien auslagern, `onerror`/`onclick`-Attribute durch Listener ersetzen, dann CSP mit `connect-src 'self'`, `frame-ancestors 'none'`.
- [ ] **Barrierefreiheit**: Hauswände per Tastatur wählbar, Fokus nach dem Neuzeichnen des Panels halten, Kontraste der Hinweistexte (`.hinweis` 2,5:1, `.fine` 4,4:1), Paket-Auswahl mit Pfeiltasten, Ladeoverlay mit Fokus.
- [ ] Statischer Modus: früher sagen, dass der Server fehlt (nicht erst nach dem Einzeichnen).
- [ ] Zustand in die URL (Adresse/Wand/Balkon), damit Zurück/Neuladen nicht alles verliert.
- [ ] Mobil (320 px): 3D-Bühne überladen (Sonnen-Box, Leistungsanzeige, Wandbild überlappen).

## Rechnung und Daten

- [ ] **Sonnenstunden**: Jetzt als „Stunden direkte Sonne bei klarem Himmel“ beschriftet; besser wettergewichtete Stunden ausweisen.
- [ ] **Wetter**: synthetisches Referenzjahr → PVGIS-TMY für Berlin einlesen (`--wetter`).
- [ ] **Bodenreflexion** in engen Höfen nicht wie auf freiem Feld rechnen (an Himmelssicht koppeln).
- [ ] **Phantomgebäude im 3D-Modell**: Für den Schatten werden LoD2-Teile über dem bDOM schon verworfen, im angezeigten Modell noch nicht.
- [ ] **Schattenquelle**: Gebäude für Schatten aus bDOM/Mesh statt LoD2 (Entscheidung offen).
- [ ] **Speicherpaket**: Austausch nach ca. 10 Jahren einrechnen statt nur Hinweis.
- [ ] **Kleine Module** (< 1,8 m Geländer) in den Katalog aufnehmen.
- [ ] Wirtschaftlichkeitsparameter (Katalog, Strompreis, MwSt., Steigerung, Degradation, Jahr 2025) in eine gemeinsame `params.json` für Python und JS.

## Skalierung / Architektur (vor dem zweiten Kiez)

- [ ] **Stabile Hauswand-IDs** (Hash aus LoD2-Gebäude-ID, Ausrichtung, gerundeten Endpunkten) – heute ändern sich IDs bei jedem Pipeline-Lauf; Balkone, Angebote und Links zeigen dann auf falsche Wände. Balkone ebenfalls per Hash statt Listenindex.
- [ ] **Angebote versionieren** (Modell- und Datenstand im Schlüssel), atomar schreiben (tmp-Ordner + rename).
- [ ] **SQLite (WAL)** statt JSON-Caches: erkannte Balkone, eingezeichnete Balkone, Angebote, Klicks, Claude-Budget, später Anfragen; Migrationen über `PRAGMA user_version`.
- [ ] **Kacheln + räumlicher Index**: 1-km²-Kacheln (float32, nach Zellen sortiert, `mmap`), je Angebot nur ±220 m laden statt der ganzen Punktwolke (heute ~650 MB je km², Innenstadt wäre ~17 GB RAM).
- [ ] **Prozess-Pool** (begrenzte Warteschlange, gleiche Anfragen nur einmal rechnen) statt globaler Sperre.
- [ ] **Statische Daten in Kacheln** (`walls.json` je Kachel statt einer `data.js`; Innenstadt wäre ~180 MB), unveränderliche URLs mit Release-Kennung, Cache-Header.
- [ ] **Adresssuche über Server** (`/api/adresse`, SQLite FTS) für ganz Berlin (~400 000 Adressen).
- [ ] **Pipeline-CLI** je Kachel, idempotent, Release mit Manifest; bDOM/DOP vorab in `.npy` umwandeln.
- [ ] API auf Starlette/uvicorn, Health-Check, Konfiguration über Umgebungsvariablen, Backups (Litestream/restic).
- [ ] Wiederholte Arbeit je Angebot vermeiden (`simuliere` = eine der Varianten, Balkongeometrie cachen).
- [ ] Tests: synthetische Testszene (Horizont = atan(h/d)), Koordinaten-Konventionen, Zeitachse, ID-Stabilität, Pixel↔Meter, parallele Schreibzugriffe, API-Validierung.

## Leistung auf schwachen Geräten (noch nicht gemessen)

- [ ] Foto-Ansicht: Mesh-Texturen pro Angebot zuschneiden (heute ~36 MB, Ziel 3–5 MB), nur sichtbare Dreiecke, Kamera-Korridor.
- [ ] Karte: Hauswände auf Canvas statt ~9 400 SVG-Elementen.
- [ ] 3D: Schattenkarte kleiner als 4096², Sichtprüfung seltener, Render-Schleife pausieren, wenn nichts passiert.
- [ ] Verbrauchsmodell (525 600 Werte) in einen Web Worker.

## Produkt / UX

- [ ] Startseite: Adresse auf Straßenkarte (basemap.de oder selbst gehostetes OSM), dann 3D-Ausschnitt nur um die Adresse.
- [ ] Angebotsseite: fester Blickkorridor statt freier 360°-Drehung.

## Erledigt (23.10.2026)

- Caddy-Beispielkonfiguration korrigiert (`_cache/`, versteckte Dateien, Hilfsdateien liefern 404; mit Caddy 2.8 geprüft), Timeouts, Body-Limit, systemd-Sandbox.
- Server: Zeitlimit je Verbindung, begrenzte gleichzeitige Verbindungen, Rechen-Warteschlange mit Zeitlimit (503), Obergrenze für Nutzer-Angebote auf Platte, Boxen auf 25 cm gerundet, Bäume auf 0,5 m.
- Limits je IPv4 bzw. IPv6-/64, kein Komplett-Reset der Zähler mehr.
- Strenge Prüfung: `fullmatch` mit ASCII-Ziffern, kein NaN/Infinity, keine internen Fehlertexte an Nutzer, Nullbyte-Pfade abgefangen.
- Schreibzugriffe auf die JSON-Caches unter Sperre (Wettlauf behoben), Claude-Tagesbudget überlebt Neustarts.
- Modellausgabe (Balkonerkennung) auf feste Typen/Wertebereiche geprüft, Geschoss im Frontend als Zahl geprüft.
- Angebotsseite: nur gültige Angebots-IDs (keine fremden Daten über den Link), verständliche Meldung bei unbekanntem/kaputtem Angebot, läuft ohne WebGL (Zahlen + Wandansicht), three.js selbst gehostet, schmale Balkone ohne passendes Set zeigen den Ertrag statt abzustürzen, 320 px ohne horizontales Scrollen.
- Zahlen: Schattenverlust bei skalierten Paketen korrigiert, 20-Jahres-Werte eindeutig (brutto/netto), Sonnenstunden ehrlich beschriftet, Strompreis als Arbeitspreis, Speicherpaket nicht mehr „lohnt sich am meisten“, kleines Modul nicht mehr mit 450 Wp gerechnet.
- Texte: 0 % MwSt. ohne „du sparst“, Garantie „auf Module“, kein „Foto hochladen“, kein „±15 %“.
- Adresssuche: genaue Hausnummer zuerst (5 ≠ 50), Zusätze (24 a), ohne Umlaute/ß, „Str.“, Postleitzahl als weicher Filter, Enter springt nur bei eindeutigem Treffer; API-Fehler und Zeitüberschreitungen verständlich.
