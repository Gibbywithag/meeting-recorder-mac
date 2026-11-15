#!/bin/zsh
# Builds Meeting Recorder and installs it as the ONE copy in /Applications (no copy is left in this folder).
# The app is self-contained: the AI scripts ship inside it, and its Python env + settings (.env with the
# Gemini key) live in ~/Library/Application Support/MeetingRecorder. This folder is only needed to rebuild.
# Run again after any Swift or Python change. Quit the app first.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"
echo "[build] swift build (release)…"
(cd app && swift build -c release)
BIN="app/.build/release/MeetingRecorder"
if pgrep -x MeetingRecorder >/dev/null; then
  echo "[build] Meeting Recorder is open. Quit it (menu bar icon > Quit) and run this again."; exit 1
fi
STAGE="$(mktemp -d)"
APP="$STAGE/MeetingRecorder.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/MeetingRecorder"
mkdir -p "$APP/Contents/Resources/engine"
cp gemini_pass.py live_pass.py report_pdf.py speakers.py video_pass.py transcript_quality.py "$APP/Contents/Resources/engine/"
cp app/Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

# Python env + settings, outside the bundle so rebuilding or replacing the app never loses them.
SUPPORT="$HOME/Library/Application Support/MeetingRecorder"
mkdir -p "$SUPPORT"
if [ ! -x "$SUPPORT/.venv/bin/python" ]; then
  PY=""
  for v in python3.14 python3.13 python3.12 python3.11; do
    for d in /opt/homebrew/bin /usr/local/bin; do [ -x "$d/$v" ] && { PY="$d/$v"; break 2; }; done
  done
  [ -n "$PY" ] || { echo "[build] ERROR: need Python 3.11+ for the AI step. Install it with: brew install python@3.13"; exit 1; }
  echo "[build] creating Python env in $SUPPORT/.venv with $PY …"
  "$PY" -m venv "$SUPPORT/.venv"
fi
"$SUPPORT/.venv/bin/pip" install -q --disable-pip-version-check google-genai python-dotenv
if [ ! -f "$SUPPORT/.env" ]; then
  if [ -f .env ]; then cp .env "$SUPPORT/.env"; echo "[build] copied settings (.env) to $SUPPORT"
  else cp .env.example "$SUPPORT/.env"; echo "[build] no key yet: the app will ask for your Gemini API key the first time you press Start"; fi
  chmod 600 "$SUPPORT/.env"
fi
command -v ffmpeg >/dev/null || echo "[build] WARNING: ffmpeg not found (brew install ffmpeg) — the AI step needs it"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>MeetingRecorder</string>
  <key>CFBundleDisplayName</key><string>Meeting Recorder</string>
  <key>CFBundleIdentifier</key><string>com.gibby.meetingrecorder</string>
  <key>CFBundleExecutable</key><string>MeetingRecorder</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>2</string>
  <key>CFBundleShortVersionString</key><string>1.1</string>
  <key>LSMinimumSystemVersion</key><string>15.0</string>
  <key>LSUIElement</key><false/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAudioCaptureUsageDescription</key><string>Records meeting audio from FaceTime and other apps.</string>
  <key>NSMicrophoneUsageDescription</key><string>Records your voice during the meeting.</string>
  <key>NSScreenCaptureUsageDescription</key><string>Records the screen during the meeting so the AI can see what was discussed.</string>
  <key>CFBundleDocumentTypes</key><array><dict>
    <key>CFBundleTypeName</key><string>Client video</string>
    <key>CFBundleTypeRole</key><string>Viewer</string>
    <key>LSHandlerRank</key><string>Alternate</string>
    <key>LSItemContentTypes</key><array><string>public.movie</string></array>
  </dict></array>
</dict></plist>
PLIST
# Sign with a stable identity when one exists, so macOS keeps the Screen Recording / Microphone grants across rebuilds.
# (Ad-hoc signatures change every build and reset the permission each time.)
IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null | grep -oE '"Developer ID Application: [^"]+"|"Apple Development: [^"]+"' | head -1 | tr -d '"' || true)
if [ -n "$IDENTITY" ]; then
  echo "[build] signing with: $IDENTITY"
  codesign --force --deep --sign "$IDENTITY" "$APP"
else
  echo "[build] WARNING: no signing identity found; ad-hoc signing (permission will reset on every rebuild)"
  codesign --force --deep --sign - "$APP"
fi
codesign --verify --deep --strict "$APP" && echo "[build] signature verified"
FINAL=/Applications/MeetingRecorder.app
rm -rf "$FINAL"
ditto "$APP" "$FINAL"
rm -rf "$STAGE"
codesign --verify --deep --strict "$FINAL" && echo "[build] signature verified"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$FINAL"
echo "[build] OK -> $FINAL (the only copy). Open it with: open -a MeetingRecorder"
