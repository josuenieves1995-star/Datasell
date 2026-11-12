#!/usr/bin/env bash
# Runs on an Android emulator in CI: install the app, open it, and check that
# the Python server, pages, a check, the database and the backup all work.
set -uo pipefail
APK="$1"
fail() { echo "SMOKE TEST FAILED: $*"; adb logcat -d | grep -iE "python|datasell|AndroidRuntime|chaquopy" | tail -150; exit 1; }

adb install -r "$APK" || fail "install"
adb logcat -c
adb shell am start -W -n com.datasell.app/.MainActivity || fail "launch"

PORT=""
for _ in $(seq 1 90); do
  PORT=$(adb logcat -d | grep -oE "Datasell running on port [0-9]+" | tail -1 | grep -oE "[0-9]+$" || true)
  [ -n "$PORT" ] && break
  sleep 2
done
[ -n "$PORT" ] || fail "the Python server never started"
echo "Server is on port $PORT"
adb forward tcp:8123 "tcp:$PORT" || fail "port forward"
BASE=http://127.0.0.1:8123

curl -sf "$BASE/check" | grep -q "Is it worth it" || fail "check page"
curl -sf "$BASE/settings" | grep -q "Back up now" || fail "settings page"
curl -sf "$BASE/fast" | grep -q "Fast sellers" || fail "fast sellers page"
curl -sf "$BASE/static/style.css" | grep -q "verdict-box" || fail "static files"

# Save an API key, then a text check: it must give eBay links, then a verdict
# once the numbers from eBay are entered. The app itself never loads eBay.
curl -sf -o /dev/null -d "api_key=sk-ant-smoketest000000" "$BASE/settings" || fail "save key"
curl -sf "$BASE/settings" | grep -q "sk-ant-smo" || fail "key not saved"
curl -sfL -d "query=levis 501 jeans" "$BASE/check" -o /tmp/result.html || fail "text check"
grep -q "Check the solds" /tmp/result.html || fail "no eBay links step"
grep -q "LH_Sold=1" /tmp/result.html || fail "no sold link"
CHECK_URL=$(curl -s -o /dev/null -w "%{redirect_url}" -d "query=carhartt b17 jeans" "$BASE/check")
CHECK_ID=$(echo "$CHECK_URL" | grep -oE "[0-9]+$")
curl -sfL -d "price=48&sold=1200&active=800&cost=10" "$BASE/check/$CHECK_ID/recheck" -o /tmp/verdict.html || fail "verdict"
grep -q "Buy it" /tmp/verdict.html || fail "no Buy verdict"

# "Run update now" exercises the daily job and the backup copy (no saved
# searches, so nothing is fetched from eBay).
curl -sf -o /dev/null -X POST "$BASE/searches/run" || fail "run update"
for _ in $(seq 1 60); do
  adb shell ls /sdcard/Documents/Datasell/ 2>/dev/null | grep -q "datasell.db" && break
  sleep 2
done
adb shell ls -l /sdcard/Documents/Datasell/ || fail "no backup in Documents/Datasell"
adb shell ls /sdcard/Documents/Datasell/ | grep -q "datasell_solds.csv" || fail "no CSV backup"
curl -sf "$BASE/searches" | grep -q "Recent runs" || fail "run not recorded"

# The background worker must be registered.
adb shell dumpsys jobscheduler | grep -q "com.datasell.app" || fail "nightly worker not scheduled"

if adb logcat -d | grep -E "FATAL EXCEPTION|Traceback" ; then fail "errors in the log"; fi
echo "SMOKE TEST PASSED"
