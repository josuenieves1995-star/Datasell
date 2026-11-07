#!/data/data/com.termux/files/usr/bin/bash
# Stop Datasell on the phone.
pkill -f "python -m datasell serve" && echo "Datasell stopped." || echo "Datasell wasn't running."
termux-wake-unlock 2>/dev/null || true
