#!/bin/bash
# Startet balki strom lokal. Doppelklick im Finder genügt.
#   http://localhost:PORT/            Nutzerseite (Adresse -> Balkon -> Angebot)
#   http://localhost:PORT/intern.html Fassaden-Explorer (intern)
# Mit Server (server/app.py) gehen auch: Balkone auf Abruf suchen, Balkon selbst einzeichnen.
# Beim ersten Start wird im Projektordner eine eigene Python-Umgebung (.venv) mit allen Paketen angelegt.
cd "$(dirname "$0")"
WEBAPP="$(pwd)"
ROOT="$(cd .. && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"

PORT=8765
while lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; do
  PORT=$((PORT + 1))
done

statisch() {
  echo ""
  echo "balki strom (nur statisch, ohne Balkonsuche/Einzeichnen) läuft auf http://localhost:$PORT  (Beenden: Ctrl+C)"
  ( sleep 1; open "http://localhost:$PORT" ) &
  python3 -m http.server "$PORT" --bind 127.0.0.1
  exit
}

KACHEL=$(ls -d "$ROOT"/data/*/ 2>/dev/null | head -1)
if [ ! -f "$ROOT/server/app.py" ] || [ -z "$KACHEL" ]; then
  statisch
fi

# Python-Umgebung einmalig einrichten
if [ ! -x "$PY" ]; then
  echo "Erster Start: richte Python-Umgebung ein (dauert 1–3 Minuten) …"
  python3 -m venv "$VENV" || { echo "Konnte keine Python-Umgebung anlegen."; statisch; }
fi
if ! "$PY" -c "import pvlib, numpy, pandas, scipy, pyproj, PIL, anthropic" 2>/dev/null; then
  echo "Installiere Pakete …"
  "$PY" -m pip install --upgrade pip >/dev/null
  if ! "$PY" -m pip install -r "$ROOT/requirements.txt" anthropic; then
    echo "Installation fehlgeschlagen – starte ohne Server."
    statisch
  fi
fi

echo ""
echo "balki strom (mit Server) startet auf http://localhost:$PORT  (Beenden: Ctrl+C)"
echo "Der erste Start lädt Stadtmodell und Wetter, das dauert ein paar Sekunden."
( for i in $(seq 1 60); do
    curl -s -o /dev/null "http://localhost:$PORT/api/status" && { open "http://localhost:$PORT"; exit; }
    sleep 1
  done ) &
"$PY" "$ROOT/server/app.py" "$KACHEL" "$WEBAPP" --port "$PORT"
