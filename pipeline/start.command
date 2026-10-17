#!/bin/bash
# Startet den Fassaden-Explorer lokal. Doppelklick im Finder genügt.
cd "$(dirname "$0")"
PORT=8765
echo "Fassaden-Explorer läuft auf http://localhost:$PORT  (Beenden: Ctrl+C oder Fenster schließen)"
( sleep 1; open "http://localhost:$PORT" ) &
python3 -m http.server "$PORT" --bind 127.0.0.1
