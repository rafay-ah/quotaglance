#!/usr/bin/env bash
# Run QuotaGlance inside a throwaway headless GNOME Shell and take screenshots.
#
#   scripts/shell-capture/run-shell.sh OUTDIR [dark|light] [extension|tray|both] [STEPS]
#
# Development tool for README screenshots and for exercising the Shell
# extension / tray fallback end to end. Needs gnome-shell, dbus and the
# GNOME wallpapers (gnome-backgrounds) installed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$(realpath -m "${1:-/tmp/qg-shell}")"

# GNOME Shell warns loudly when the session runs as root; use a throwaway user.
if [ "$(id -u)" = "0" ]; then
  id qgdemo >/dev/null 2>&1 || useradd --create-home --shell /bin/bash qgdemo
  mkdir -p "$OUT" && chown qgdemo "$OUT"
  exec runuser -u qgdemo -- env PYTHON="${PYTHON:-python3}" QG_SEED_CONFIG="${QG_SEED_CONFIG:-}" \
    QG_NO_BANNERS="${QG_NO_BANNERS:-}" QUOTAGLANCE_HIDE_DEMO_BANNER="${QUOTAGLANCE_HIDE_DEMO_BANNER:-}" \
    QG_APP_ARGS="${QG_APP_ARGS:-}" QG_APP_DELAY="${QG_APP_DELAY:-2}" "$0" "$@"
fi
SCHEME="${2:-dark}"
MODE="${3:-extension}"
STEPS="${4:-wait:9000,log,shot:desktop,open-menu,shot:menu,close-menu,quit}"
PY="${PYTHON:-python3}"

SANDBOX="$(mktemp -d /tmp/qg-shell-XXXX)"
export HOME="$SANDBOX/home" XDG_RUNTIME_DIR="$SANDBOX/run"
export XDG_CONFIG_HOME="$HOME/.config" XDG_DATA_HOME="$HOME/.local/share"
export XDG_CACHE_HOME="$HOME/.cache" XDG_STATE_HOME="$HOME/.local/state"
mkdir -p "$HOME" "$XDG_RUNTIME_DIR" "$OUT" "$XDG_DATA_HOME/gnome-shell/extensions"
chmod 700 "$XDG_RUNTIME_DIR"
rm -f "$OUT/capture.log"

EXTS="'qg-capture@quotaglance.dev'"
cp -r "$ROOT/scripts/shell-capture/qg-capture@quotaglance.dev" "$XDG_DATA_HOME/gnome-shell/extensions/"
if [ "$MODE" = "extension" ] || [ "$MODE" = "both" ]; then
  cp -r "$ROOT/extension/quotaglance@rafay-ah.github.io" "$XDG_DATA_HOME/gnome-shell/extensions/"
  EXTS="$EXTS, 'quotaglance@rafay-ah.github.io'"
fi
if [ "$MODE" = "tray" ] || [ "$MODE" = "both" ]; then
  EXTS="$EXTS, 'ubuntu-appindicators@ubuntu.com'"
fi

if [ "$SCHEME" = "dark" ]; then BG=adwaita-d.jpg; else BG=adwaita-l.jpg; fi

# Desktop integration, as an installed package would provide it.
mkdir -p "$XDG_DATA_HOME/applications" "$XDG_DATA_HOME/icons"
cp -r "$ROOT/data/icons/hicolor" "$XDG_DATA_HOME/icons/"
sed "s|^Exec=quotaglance|Exec=env PYTHONPATH=$ROOT/src $PY -m quotaglance|; s|^DBusActivatable=true|DBusActivatable=false|" \
  "$ROOT/data/io.github.rafay_ah.QuotaGlance.desktop" \
  > "$XDG_DATA_HOME/applications/io.github.rafay_ah.QuotaGlance.desktop"
if [ -n "${QG_SEED_CONFIG:-}" ]; then
  mkdir -p "$XDG_CONFIG_HOME/quotaglance"
  printf '%s' "$QG_SEED_CONFIG" > "$XDG_CONFIG_HOME/quotaglance/config.json"
fi

export QG_CAPTURE_DIR="$OUT" QG_CAPTURE_STEPS="$STEPS" QUOTAGLANCE_DEMO=1

# Containers usually lack a system bus; GNOME Shell needs one to start.
if [ ! -S /run/dbus/system_bus_socket ] && [ -z "${DBUS_SYSTEM_BUS_ADDRESS:-}" ]; then
  eval "$(dbus-daemon --config-file="$ROOT/scripts/shell-capture/system-bus.conf" \
    --fork --print-address=1 --print-pid=2 2>"$SANDBOX/sysbus.pid" | sed 's/^/DBUS_SYSTEM_BUS_ADDRESS=/')"
  export DBUS_SYSTEM_BUS_ADDRESS
  SYSBUS_PID="$(cat "$SANDBOX/sysbus.pid")"
  trap 'kill "$SYSBUS_PID" 2>/dev/null || true' EXIT
fi
dbus-run-session -- bash -c "
  gsettings set org.gnome.shell enabled-extensions \"[$EXTS]\"
  gsettings set org.gnome.shell welcome-dialog-last-shown-version '999'
  gsettings set org.gnome.desktop.interface color-scheme 'prefer-$SCHEME'
  gsettings set org.gnome.desktop.interface enable-animations false
  gsettings set org.gnome.desktop.interface font-name 'Inter 11' 
  if [ -n \"\${QG_NO_BANNERS:-}\" ]; then gsettings set org.gnome.desktop.notifications show-banners false; fi
  gsettings set org.gnome.desktop.background picture-uri 'file:///usr/share/backgrounds/gnome/$BG'
  gsettings set org.gnome.desktop.background picture-uri-dark 'file:///usr/share/backgrounds/gnome/$BG'
  gnome-shell --headless --wayland --no-x11 --virtual-monitor 1440x900 > '$OUT/shell.log' 2>&1 &
  SHELL_PID=\$!
  for i in \$(seq 1 50); do [ -S \"\$XDG_RUNTIME_DIR/wayland-0\" ] && break; sleep 0.2; done
  sleep \${QG_APP_DELAY:-2}
  WAYLAND_DISPLAY=wayland-0 GDK_BACKEND=wayland PYTHONPATH='$ROOT/src' \
    ADW_DEBUG_COLOR_SCHEME=prefer-$SCHEME \
    $PY -m quotaglance --demo \${QG_APP_ARGS:-} > '$OUT/app.log' 2>&1 &
  for i in \$(seq 1 120); do grep -q '^done' '$OUT/capture.log' 2>/dev/null && break; sleep 0.5; done
  kill \$SHELL_PID 2>/dev/null || true
"
rm -rf "$SANDBOX"
ls -la "$OUT"
