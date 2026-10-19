#!/bin/bash
# Startet balki strom lokal. Doppelklick im Finder genügt.
#   http://localhost:PORT/            Nutzerseite (Adresse -> Balkon -> Angebot)
#   http://localhost:PORT/intern.html Fassaden-Explorer (intern)
# Mit Server (server/app.py) gehen auch: Balkone auf Abruf suchen, Balkon selbst einzeichnen.
cd "$(dirname "$0")"
PORT=8765
while lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; do
  PORT=$((PORT + 1))
done
KACHEL=$(ls -d ../data/*/ 2>/dev/null | head -1)
( sleep 2; open "http://localhost:$PORT" ) &
if [ -f ../server/app.py ] && [ -n "$KACHEL" ]; then
  echo "balki strom (mit Server) startet auf http://localhost:$PORT  (Beenden: Ctrl+C)"
  python3 ../server/app.py "$KACHEL" . --port "$PORT"
else
  echo "balki strom (nur statisch) läuft auf http://localhost:$PORT  (Beenden: Ctrl+C)"
  python3 -m http.server "$PORT" --bind 127.0.0.1
fi
