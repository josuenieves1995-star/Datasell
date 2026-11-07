#!/data/data/com.termux/files/usr/bin/bash
# Start Datasell on the phone (if it isn't running) and open it in the browser.
#   bash ~/datasell/termux/start.sh               start + open the browser
#   bash ~/datasell/termux/start.sh --no-browser  start only (used at boot)
cd "$(dirname "$0")/.."
[ -f "$HOME/.datasell.env" ] && . "$HOME/.datasell.env"
PORT="${DATASELL_PORT:-8080}"
mkdir -p "$HOME/datasell-data"

# Keep the phone from pausing Termux, so the nightly update can run.
termux-wake-lock 2>/dev/null || true

if ! python -c "import socket; socket.create_connection(('127.0.0.1', $PORT), 1)" 2>/dev/null; then
  nohup python -m datasell serve --port "$PORT" >> "$HOME/datasell-data/server.log" 2>&1 &
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    python -c "import socket; socket.create_connection(('127.0.0.1', $PORT), 1)" 2>/dev/null && break
  done
fi

if [ "$1" != "--no-browser" ]; then
  termux-open-url "http://127.0.0.1:$PORT/check"
fi
echo "Datasell is running at http://127.0.0.1:$PORT (log: ~/datasell-data/server.log)"
