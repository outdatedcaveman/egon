#!/usr/bin/env bash
# Self-driving restore loop: cold-restart Chrome, run restore script until it
# exits (Chrome will eventually die), force-stop+restart Chrome, repeat.
# Stops when checkpoint reaches the manifest length (1345).
#
# Run from egon/ root: bash scripts/restore_loop.sh

set -u
EGON_ROOT="C:/Users/bruno/Claude Code/egon"
ADB="$EGON_ROOT/panop_output/platform-tools/platform-tools/adb.exe"
DEVICE="192.168.0.3:5555"
PY="$EGON_ROOT/.venv/Scripts/python.exe"
CHECKPOINT="$EGON_ROOT/state/restore/checkpoint.json"
TOTAL=1345
MAX_ROUNDS=12

round=0
while [ $round -lt $MAX_ROUNDS ]; do
    round=$((round + 1))
    echo
    echo "================================================================="
    echo "  ROUND $round  ($(date '+%H:%M:%S'))"
    echo "================================================================="

    # Check checkpoint progress
    if [ -f "$CHECKPOINT" ]; then
        next_idx=$(grep -o '"next_idx": *[0-9]*' "$CHECKPOINT" | grep -o '[0-9]*')
        echo "  checkpoint: idx $next_idx of $TOTAL"
        if [ "$next_idx" -ge "$TOTAL" ]; then
            echo "  ALL DONE — exiting loop"
            break
        fi
    fi

    # Cold-restart Chrome
    echo "  [cold-restart Chrome]"
    "$ADB" -s "$DEVICE" shell am force-stop com.android.chrome > /dev/null 2>&1
    sleep 4
    "$ADB" -s "$DEVICE" shell input keyevent KEYCODE_WAKEUP > /dev/null 2>&1
    "$ADB" -s "$DEVICE" shell monkey -p com.android.chrome -c android.intent.category.LAUNCHER 1 > /dev/null 2>&1

    # Wait for DevTools socket
    echo "  [waiting for DevTools to come up]"
    "$ADB" -s "$DEVICE" forward --remove tcp:9222 > /dev/null 2>&1
    "$ADB" -s "$DEVICE" forward tcp:9222 localabstract:chrome_devtools_remote > /dev/null 2>&1
    for i in $(seq 1 20); do
        if curl -sS --max-time 3 http://127.0.0.1:9222/json/version > /dev/null 2>&1; then
            echo "  DevTools UP after ${i}s"
            break
        fi
        sleep 1
    done

    # Run restore (exits when WS dies)
    echo "  [running restore from idx $next_idx]"
    "$PY" "$EGON_ROOT/scripts/restore_closed_tabs_v2.py" 2>&1 | tail -3

    # brief cool-down before next force-stop
    sleep 5
done

# Final summary
echo
if [ -f "$CHECKPOINT" ]; then
    final=$(grep -o '"next_idx": *[0-9]*' "$CHECKPOINT" | grep -o '[0-9]*')
    echo "FINAL: idx $final of $TOTAL  (rounds=$round)"
fi
echo "Final Chrome tab count:"
curl -sS --max-time 8 http://127.0.0.1:9222/json/list 2>/dev/null | python -c "import sys,re; print('  tabs:', len(re.findall(r'\"type\":\\s*\"page\"', sys.stdin.read())))" 2>/dev/null || echo "  (devtools unreachable)"
