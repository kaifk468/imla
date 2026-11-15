#!/usr/bin/env bash
# Creates "Imla.app" and "Imla Settings.app" in ~/Applications (Spotlight, Launchpad,
# Dock and Applications folder all find them). Optional: start on login.
# Usage: bash scripts/install_macos.sh [--start-on-login]
# Remove: bash scripts/install_macos.sh --uninstall
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
DEST="$HOME/Applications"
AGENT="$HOME/Library/LaunchAgents/com.imla.dictation.plist"

START_ON_LOGIN=0
UNINSTALL=0
for a in "$@"; do
  case "$a" in
    --start-on-login) START_ON_LOGIN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) echo "Unknown option: $a"; exit 2 ;;
  esac
done

if [ "$UNINSTALL" = 1 ]; then
  rm -rf "$DEST/Imla.app" "$DEST/Imla Settings.app"
  launchctl unload "$AGENT" 2>/dev/null || true
  rm -f "$AGENT"
  echo "Removed Imla apps."
  exit 0
fi

if [ ! -x "$PY" ]; then
  echo "Could not find $PY"
  echo "Create the environment first, from the project folder:"
  echo "    uv sync --python 3.12"
  exit 1
fi

make_app() { # app-name bundle-id exec-args
  local name="$1" bid="$2" args="$3"
  local app="$DEST/$name.app"
  rm -rf "$app"
  mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"

  cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$name</string>
  <key>CFBundleDisplayName</key><string>$name</string>
  <key>CFBundleIdentifier</key><string>$bid</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>NSMicrophoneUsageDescription</key><string>Imla records your voice to transcribe it.</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
EOF

  cat > "$app/Contents/MacOS/launch" <<EOF
#!/bin/bash
cd "$ROOT"
exec "$PY" -m imla.main $args
EOF
  chmod +x "$app/Contents/MacOS/launch"
  [ -f "$ROOT/assets/imla.icns" ] && cp "$ROOT/assets/imla.icns" "$app/Contents/Resources/AppIcon.icns" || true
  # Ad-hoc sign so macOS keeps the privacy permissions you grant to this app.
  codesign --force --deep --sign - "$app" >/dev/null 2>&1 || true
  echo "Created: $app"
}

mkdir -p "$DEST"
make_app "Imla" "com.imla.dictation" ""
make_app "Imla Settings" "com.imla.settings" "--settings"

if [ "$START_ON_LOGIN" = 1 ]; then
  mkdir -p "$(dirname "$AGENT")"
  cat > "$AGENT" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.imla.dictation</string>
  <key>ProgramArguments</key><array><string>$DEST/Imla.app/Contents/MacOS/launch</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
EOF
  echo "Created: $AGENT (active from your next login)"
fi

cat <<'EOF'

Done. Open Spotlight (Cmd+Space) and type "Imla".
Drag "Imla" from ~/Applications to the Dock for one-click access.

First launch: macOS will ask for permissions. Grant them in
System Settings > Privacy & Security:
  - Microphone        (to record)
  - Accessibility     (to paste text at your cursor)
  - Input Monitoring  (to detect the Right Option hotkey)
If the app is blocked as "unidentified developer", right-click it and choose Open once.
EOF
