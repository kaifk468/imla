#!/usr/bin/env bash
# Creates Imla launchers on Linux (tested target: Ubuntu GNOME).
#   - "Imla" and "Imla Settings" entries in the app menu (press Super, type "Imla")
#   - the same two icons on the Desktop
#   - optional: start on login
# Usage: bash scripts/install_linux.sh [--start-on-login] [--no-desktop]
# Remove: bash scripts/install_linux.sh --uninstall
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"

START_ON_LOGIN=0
WITH_DESKTOP=1
UNINSTALL=0
for a in "$@"; do
  case "$a" in
    --start-on-login) START_ON_LOGIN=1 ;;
    --no-desktop) WITH_DESKTOP=0 ;;
    --uninstall) UNINSTALL=1 ;;
    *) echo "Unknown option: $a"; exit 2 ;;
  esac
done

if [ "$UNINSTALL" = 1 ]; then
  rm -f "$APPS/imla.desktop" "$APPS/imla-settings.desktop" "$APPS/imla-quit.desktop" \
        "$DESKTOP_DIR/Imla.desktop" "$DESKTOP_DIR/Imla Settings.desktop" \
        "$DESKTOP_DIR/Quit Imla.desktop" \
        "$AUTOSTART/imla.desktop"
  echo "Removed Imla launchers."
  exit 0
fi

if [ ! -x "$PY" ]; then
  echo "Could not find $PY"
  echo "Create the environment first, from the project folder:"
  echo "    uv sync --python 3.12"
  exit 1
fi

# Wrapper so the launcher works from any folder, with .env found, and logs
# go to ~/.config/imla/imla.log (the app writes them itself).
BIN="$ROOT/scripts/imla-launch.sh"
cat > "$BIN" <<EOF
#!/usr/bin/env bash
export DISPLAY="\${DISPLAY:-:0}"
cd "$ROOT"
exec "$PY" -m imla.main "\$@"
EOF
chmod +x "$BIN"

ICON="audio-input-microphone"
[ -f "$ROOT/assets/imla.png" ] && ICON="$ROOT/assets/imla.png"

write_entry() { # path name exec-args
  local path="$1" name="$2" args="$3"
  cat > "$path" <<EOF
[Desktop Entry]
Type=Application
Name=$name
Comment=Imla voice dictation
Exec="$BIN" $args
Path=$ROOT
Icon=$ICON
Terminal=false
Categories=Utility;Audio;
StartupNotify=false
EOF
  chmod +x "$path"
  echo "Created: $path"
}

mkdir -p "$APPS"
write_entry "$APPS/imla.desktop" "Imla" ""
write_entry "$APPS/imla-settings.desktop" "Imla Settings" "--settings"
write_entry "$APPS/imla-quit.desktop" "Quit Imla" "--quit"

if [ "$WITH_DESKTOP" = 1 ] && [ -d "$DESKTOP_DIR" ]; then
  write_entry "$DESKTOP_DIR/Imla.desktop" "Imla" ""
  write_entry "$DESKTOP_DIR/Imla Settings.desktop" "Imla Settings" "--settings"
  write_entry "$DESKTOP_DIR/Quit Imla.desktop" "Quit Imla" "--quit"
  # Ubuntu shows "Allow Launching" until the file is marked trusted.
  for f in "$DESKTOP_DIR/Imla.desktop" "$DESKTOP_DIR/Imla Settings.desktop" "$DESKTOP_DIR/Quit Imla.desktop"; do
    gio set "$f" metadata::trusted true 2>/dev/null || true
  done
fi

if [ "$START_ON_LOGIN" = 1 ]; then
  mkdir -p "$AUTOSTART"
  write_entry "$AUTOSTART/imla.desktop" "Imla" ""
fi

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true

echo
echo "Done. Press Super and type 'Imla' to start it, 'Quit Imla' to stop it."
echo "You can also quit from Imla Settings (the 'Quit Imla' button, bottom left)."
echo "Last resort without a tray menu: pkill -f imla.main"
echo "If a Desktop icon says 'Allow Launching', right-click it and choose that once."
if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
  echo
  echo "NOTE: you are on a Wayland session. Global hotkeys (Right Alt) and pasting"
  echo "into other apps are blocked by Wayland. Choose 'Ubuntu on Xorg' on the login"
  echo "screen (gear icon) for dictation to work."
fi
