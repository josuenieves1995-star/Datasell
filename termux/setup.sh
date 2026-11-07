#!/data/data/com.termux/files/usr/bin/bash
# One-time setup of Datasell on an Android phone. Run inside Termux:
#   bash ~/datasell/termux/setup.sh
set -e
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$HOME/datasell-data"

echo "== 1/4 Installing Python (a few minutes) =="
pkg update -y
pkg install -y python
# Prebuilt phone packages when Termux has them; the app works without these two.
pkg install -y python-pillow 2>/dev/null || echo "(Pillow not available; photos are sent as-is)"
pkg install -y python-lxml 2>/dev/null || echo "(lxml not available; using the built-in HTML parser)"

echo "== 2/4 Installing Python libraries =="
pip install flask requests beautifulsoup4
if ! pip install anthropic; then
  echo "The Claude library needs to be built on this phone. Installing Rust (one time, slow)..."
  pkg install -y rust binutils
  pip install anthropic
fi

echo "== 3/4 Phone storage =="
echo "Android will ask to allow Termux to access files: tap Allow."
[ -d "$HOME/storage/shared" ] || termux-setup-storage
sleep 4
mkdir -p "$DATA_DIR"
BACKUP_DIR="$HOME/storage/shared/Documents/Datasell"
mkdir -p "$BACKUP_DIR" 2>/dev/null || echo "Couldn't create $BACKUP_DIR yet; run 'termux-setup-storage' and re-run this script."

echo "== 4/4 Settings =="
if [ ! -f "$HOME/.datasell.env" ] || ! grep -q ANTHROPIC_API_KEY "$HOME/.datasell.env"; then
  echo "Paste your Claude API key (from console.anthropic.com) and press Enter."
  echo "Leave it empty to skip; you can still check items by typing what they are."
  read -r -s -p "API key: " KEY; echo
fi
TZ_NAME="$(getprop persist.sys.timezone 2>/dev/null || true)"
{
  echo "export DATASELL_DB=\"$DATA_DIR/datasell.db\""
  echo "export DATASELL_BACKUP_DIR=\"$BACKUP_DIR\""
  [ -n "$TZ_NAME" ] && echo "export DATASELL_TZ=\"$TZ_NAME\""
  if [ -n "${KEY:-}" ]; then
    echo "export ANTHROPIC_API_KEY=\"$KEY\""
  elif [ -f "$HOME/.datasell.env" ]; then
    grep ANTHROPIC_API_KEY "$HOME/.datasell.env" || true
  fi
} > "$HOME/.datasell.env.new"
mv "$HOME/.datasell.env.new" "$HOME/.datasell.env"
chmod 600 "$HOME/.datasell.env"

# Home-screen shortcut (needs the Termux:Widget app) and start-on-boot (Termux:Boot app).
mkdir -p "$HOME/.shortcuts" "$HOME/.termux/boot"
printf '#!/data/data/com.termux/files/usr/bin/bash\nbash "%s/termux/start.sh"\n' "$APP_DIR" > "$HOME/.shortcuts/Datasell"
printf '#!/data/data/com.termux/files/usr/bin/bash\nbash "%s/termux/start.sh" --no-browser\n' "$APP_DIR" > "$HOME/.termux/boot/datasell"
chmod +x "$HOME/.shortcuts/Datasell" "$HOME/.termux/boot/datasell"

echo
echo "Done! Start Datasell with:  bash $APP_DIR/termux/start.sh"
echo "Your data: $DATA_DIR (backup + CSV each night in Documents/Datasell)"
