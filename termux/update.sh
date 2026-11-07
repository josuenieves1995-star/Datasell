#!/data/data/com.termux/files/usr/bin/bash
# Get the latest version of Datasell and restart it. Your data is not touched.
cd "$(dirname "$0")/.."
git pull --ff-only
bash termux/stop.sh
bash termux/start.sh
