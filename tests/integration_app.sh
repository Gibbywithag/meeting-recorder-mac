#!/bin/zsh
# Drives the installed /Applications/MeetingRecorder.app headlessly: launch, SIGUSR1 to start, play a sound, SIGUSR1 to stop, wait for summary.md.
set -uo pipefail
cd "$(dirname "$0")/.."
pkill -x MeetingRecorder 2>/dev/null; sleep 1
open -a /Applications/MeetingRecorder.app; sleep 3
PID=$(pgrep -x MeetingRecorder | head -1); [ -n "$PID" ] || { echo "FAIL: app not running"; exit 1; }
echo "app pid $PID"
MARKER=$(mktemp /tmp/meeting-recorder-test.XXXXXX)
trap 'rm -f "$MARKER"' EXIT
kill -USR1 $PID; sleep 4
DIR=$(ls -td ~/Meetings/*/ | head -1)
[ "$DIR/log.txt" -nt "$MARKER" ] || { echo "FAIL: no fresh session"; exit 1; }
grep -q "capture started" "$DIR/log.txt" || { cat "$DIR/log.txt"; exit 1; }
echo "folder $DIR"
afplay /System/Library/Sounds/Hero.aiff; sleep 3
kill -USR2 $PID; sleep 1.5            # open the panel
screencapture -x /tmp/app_panel.png   # proof screenshot: menu bar timer + open panel
kill -USR2 $PID; sleep 0.5            # close it again
kill -USR1 $PID
for i in $(seq 1 120); do
  if [ "$DIR/summary.md" -nt "$MARKER" ] && [ "$DIR/transcript.md" -nt "$MARKER" ] && grep -q "gemini_pass exited 0" "$DIR/log.txt"; then echo "PASS: summary.md after ${i}x2s"; grep -E "USR1|STATUS|gemini_pass exited|Done|rror" "$DIR/log.txt" | tail -8; exit 0; fi
  if grep -q "exited [1-9]" "$DIR/log.txt" 2>/dev/null; then echo "FAIL: gemini_pass failed"; tail -20 "$DIR/log.txt"; exit 1; fi
  sleep 2
done
echo "FAIL: timeout"; tail -20 "$DIR/log.txt"; exit 1
