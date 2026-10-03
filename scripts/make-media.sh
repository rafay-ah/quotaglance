#!/usr/bin/env bash
# Regenerate every image in docs/screenshots from demo data.
#
#   scripts/make-media.sh
#
# Needs: weston, gnome-shell, gnome-backgrounds, imagemagick, Inter font,
# and the GTK/libadwaita Python bindings. Uses throwaway sessions only.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/docs/screenshots"
PY="${PYTHON:-python3}"
TMP="$(mktemp -d)"
chmod 755 "$TMP"
trap 'kill "${WESTON_PID:-}" 2>/dev/null || true; rm -rf "$TMP"' EXIT
mkdir -p "$OUT"
FONT="Inter 10.5"

echo ">> GTK surfaces (weston, headless)"
export XDG_RUNTIME_DIR="$TMP/run"
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"
weston --backend=headless --socket=wayland-media --width=1600 --height=1000 --idle-time=0 \
  > "$TMP/weston.log" 2>&1 &
WESTON_PID=$!
for _ in $(seq 1 50); do [ -S "$XDG_RUNTIME_DIR/wayland-media" ] && break; sleep 0.2; done
for scheme in light dark; do
  flag=""; [ "$scheme" = dark ] && flag="--dark"
  WAYLAND_DISPLAY=wayland-media GDK_BACKEND=wayland QUOTAGLANCE_HIDE_DEMO_BANNER=1 \
    dbus-run-session -- "$PY" "$ROOT/scripts/capture.py" --out "$TMP/gtk" --font "$FONT" $flag \
    2>/dev/null | grep wrote || true
done
kill "$WESTON_PID" 2>/dev/null || true
cp "$TMP/gtk/main-light.png" "$TMP/gtk/main-dark.png" "$TMP/gtk/preferences-light.png" \
  "$TMP/gtk/providers-dark.png" "$OUT/"

echo ">> Widget gallery"
bg="/usr/share/backgrounds/gnome/blobs-d.svg"
sheet="$TMP/sheet"
mkdir -p "$sheet"
for scheme in light dark; do
  convert "$TMP/gtk/widget-small-$scheme.png" "$TMP/gtk/widget-medium-$scheme.png" \
    -background none -gravity center -splice 24x0 +append "$sheet/row1-$scheme.png"
done
convert "$TMP/gtk/widget-large-light.png" "$TMP/gtk/widget-large-dark.png" \
  "$TMP/gtk/widget-medium-tinted-dark.png" -background none -gravity north -splice 24x0 +append \
  "$sheet/row3.png"
convert "$sheet/row1-light.png" "$sheet/row1-dark.png" -background none -gravity west \
  -splice 0x24 -append "$sheet/col.png"
convert "$sheet/col.png" "$sheet/row3.png" -background none -gravity north -splice 0x24 -append \
  -bordercolor none -border 36 "$sheet/widgets-raw.png"
# Soft drop shadow, then place on a wallpaper crop.
convert "$sheet/widgets-raw.png" \( +clone -background black -shadow 45x14+0+8 \) +swap \
  -background none -layers merge +repage "$sheet/widgets-shadow.png"
size="$(identify -format '%wx%h' "$sheet/widgets-shadow.png")"
convert -density 96 "$bg" -resize "${size}^" -gravity center -extent "$size" "$sheet/bg.png"
convert "$sheet/bg.png" "$sheet/widgets-shadow.png" -gravity center -composite \
  -resize '1400x>' "$OUT/widgets.png"

echo ">> GNOME Shell: hero, notification, tray, tour"
seed='{"first_run_complete": true, "autostart": false, "widget": {"visible": true, "size": "large", "position": [48, 72]}}'
QUOTAGLANCE_HIDE_DEMO_BANNER=1 QG_SEED_CONFIG="$seed" PYTHON="$PY" \
  "$ROOT/scripts/shell-capture/run-shell.sh" "$TMP/hero" dark extension \
  "wait:6000,overview-hide,wait:2000,move:QuotaGlance:478:86,clear-notifications,wait:1200,open-menu,shot:hero,quit" \
  > /dev/null 2>&1
cp "$TMP/hero/hero.png" "$OUT/hero.png"

QG_APP_DELAY=7 QUOTAGLANCE_HIDE_DEMO_BANNER=1 \
  QG_SEED_CONFIG='{"first_run_complete": true, "autostart": false}' PYTHON="$PY" \
  "$ROOT/scripts/shell-capture/run-shell.sh" "$TMP/notif" dark extension \
  "wait:5000,overview-hide,clear-notifications,wait:5500,shot:notification,quit" > /dev/null 2>&1
convert "$TMP/notif/notification.png" -crop 640x150+400+30 +repage "$OUT/notification.png"

QG_SEED_CONFIG='{"first_run_complete": true, "autostart": false}' PYTHON="$PY" \
  "$ROOT/scripts/shell-capture/run-shell.sh" "$TMP/tray" dark tray \
  "wait:7000,overview-hide,clear-notifications,wait:3000,open-menu:appindicator,shot:tray,quit" \
  > /dev/null 2>&1
convert "$TMP/tray/tray.png" -crop 640x560+800+0 +repage "$OUT/tray.png"

seed_small='{"first_run_complete": true, "autostart": false, "widget": {"visible": true, "size": "small", "position": [48, 72]}}'
QUOTAGLANCE_HIDE_DEMO_BANNER=1 QG_SEED_CONFIG="$seed_small" PYTHON="$PY" QG_APP_ARGS="--background" \
  "$ROOT/scripts/shell-capture/run-shell.sh" "$TMP/tour" dark extension \
  "wait:6000,overview-hide,clear-notifications,wait:1500,shot:t1,action:widget-size:medium,shot:t2,action:widget-size:large,shot:t3,action:widget-tinted,shot:t4,action:widget-tinted,open-menu,shot:t5,close-menu,action:show-window,wait:800,move:QuotaGlance:478:86,wait:600,shot:t6,quit" \
  > /dev/null 2>&1
convert -delay 170 -loop 0 "$TMP"/tour/t{1,2,3,4,5,6}.png -resize 960x -dither FloydSteinberg \
  -colors 192 -layers Optimize "$OUT/tour.gif"

ls -la "$OUT"
