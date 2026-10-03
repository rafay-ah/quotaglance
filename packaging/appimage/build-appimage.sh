#!/usr/bin/env bash
# Build a self-contained x86_64 AppImage on Ubuntu 24.04.
#
#   packaging/appimage/build-appimage.sh [output-dir]
#
# Bundles Python, PyGObject, pycairo, GTK 4, libadwaita and libsecret, so
# the AppImage runs on distributions with glibc >= 2.39 even without those
# packages installed. Host-specific pieces (glibc, Mesa/GL, fonts, themes,
# D-Bus services) come from the system, following AppImage conventions.
#
# Build requirements (CI installs them):
#   python3 python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-4.0 gir1.2-adw-1
#   gir1.2-secret-1 libgtk-4-1 libadwaita-1-0 libsecret-1-0 librsvg2-common
#   libglib2.0-bin dconf-gsettings-backend adwaita-icon-theme file
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$(realpath -m "${1:-$ROOT/dist}")"
PY="${PYTHON:-python3}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/src/quotaglance/__init__.py")"
APP_ID="io.github.rafay_ah.QuotaGlance"
UUID="quotaglance@rafay-ah.github.io"
ARCH="$(uname -m)"
MULTIARCH="$(dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null || echo "$ARCH-linux-gnu")"
LIBDIR="/usr/lib/$MULTIARCH"
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
TOOLS="${QG_TOOLS_DIR:-$ROOT/build/tools}"
WORK="$(mktemp -d)"
APPDIR="$WORK/AppDir"
trap 'rm -rf "$WORK"' EXIT
export APPIMAGE_EXTRACT_AND_RUN=1  # no FUSE needed for the build tools

fetch() {
  local name="$1" url="$2"
  if [ ! -x "$TOOLS/$name" ]; then
    mkdir -p "$TOOLS"
    curl -fsSL --retry 4 -o "$TOOLS/$name" "$url"
    chmod +x "$TOOLS/$name"
  fi
}
fetch linuxdeploy "https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-$ARCH.AppImage"
fetch appimagetool "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"

echo ">> Python $PYVER runtime"
install -D "$(readlink -f "$(command -v "python$PYVER")")" "$APPDIR/usr/bin/python3"
mkdir -p "$APPDIR/usr/lib"
tar -C /usr/lib --exclude="python$PYVER/test" --exclude="python$PYVER/idlelib" \
  --exclude="python$PYVER/tkinter" --exclude="python$PYVER/turtledemo" \
  --exclude="python$PYVER/ensurepip" --exclude="python$PYVER/lib2to3" \
  --exclude="python$PYVER/pydoc_data" --exclude="python$PYVER/config-*" \
  --exclude="python$PYVER/site-packages" --exclude="python$PYVER/dist-packages" \
  -cf - "python$PYVER" | tar -C "$APPDIR/usr/lib" -xf -
mkdir -p "$APPDIR/usr/lib/python3/dist-packages"
for module in gi cairo; do
  cp -a "/usr/lib/python3/dist-packages/$module" "$APPDIR/usr/lib/python3/dist-packages/"
done

echo ">> QuotaGlance $VERSION"
mkdir -p "$APPDIR/usr/share/quotaglance/extension"
cp -r "$ROOT/src/quotaglance" "$APPDIR/usr/share/quotaglance/"
cp -r "$ROOT/extension/$UUID" "$APPDIR/usr/share/quotaglance/extension/"
find "$APPDIR/usr/share/quotaglance" -name '__pycache__' -type d -prune -exec rm -rf {} +
"$PY" -m compileall -q "$APPDIR/usr/share/quotaglance/quotaglance"

echo ">> GObject introspection data"
mkdir -p "$APPDIR/usr/lib/girepository-1.0"
for pkg in gir1.2-glib-2.0 gir1.2-freedesktop gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-secret-1 \
           gir1.2-pango-1.0 gir1.2-harfbuzz-0.0 gir1.2-graphene-1.0 gir1.2-gdkpixbuf-2.0; do
  dpkg -L "$pkg" 2>/dev/null | grep '\.typelib$' | while read -r typelib; do
    cp -a "$typelib" "$APPDIR/usr/lib/girepository-1.0/"
  done
done

echo ">> Image loaders, GIO modules, GSettings schemas, icons"
mkdir -p "$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders" "$APPDIR/usr/lib/gio/modules" \
  "$APPDIR/usr/share/glib-2.0/schemas" "$APPDIR/usr/lib/gtk-4.0"
cp -a "$LIBDIR"/gdk-pixbuf-2.0/2.10.0/loaders/*.so "$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders/"
install -m 755 "$LIBDIR/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders" "$APPDIR/usr/bin/"
cp -a "$LIBDIR/gio/modules/libdconfsettings.so" "$APPDIR/usr/lib/gio/modules/"
cp /usr/share/glib-2.0/schemas/org.gtk.gtk4.*.gschema.xml "$APPDIR/usr/share/glib-2.0/schemas/"
glib-compile-schemas "$APPDIR/usr/share/glib-2.0/schemas"
mkdir -p "$APPDIR/usr/share/icons"
cp -r "$ROOT/data/icons/hicolor" "$APPDIR/usr/share/icons/"
# Fallback copies of the few symbolic icons the UI uses, for non-GNOME hosts.
for icon in view-refresh open-menu view-pin dialog-warning dialog-information user-trash \
            preferences-system view-grid window-close; do
  src="$(find /usr/share/icons/Adwaita -name "$icon-symbolic.svg" | head -n1 || true)"
  if [ -n "$src" ]; then
    install -Dm644 "$src" "$APPDIR/usr/share/icons/hicolor/symbolic/apps/$icon-symbolic.svg"
  fi
done
install -Dm644 "$ROOT/data/$APP_ID.metainfo.xml" "$APPDIR/usr/share/metainfo/$APP_ID.appdata.xml"
sed -e 's/^DBusActivatable=true/DBusActivatable=false/' "$ROOT/data/$APP_ID.desktop" \
  > "$WORK/$APP_ID.desktop"

echo ">> Shared libraries"
libs=()
for name in libgtk-4.so.1 libadwaita-1.so.0 libsecret-1.so.0 libgirepository-1.0.so.1 \
            libgraphene-1.0.so.0 libpango-1.0.so.0 libpangocairo-1.0.so.0 libpangoft2-1.0.so.0 \
            libcairo-gobject.so.2 libgdk_pixbuf-2.0.so.0 librsvg-2.so.2 libgmodule-2.0.so.0 \
            libgio-2.0.so.0 libharfbuzz-gobject.so.0; do
  if [ -e "$LIBDIR/$name" ]; then libs+=(--library "$LIBDIR/$name"); fi
done
"$TOOLS/linuxdeploy" --appdir "$APPDIR" \
  --executable "$APPDIR/usr/bin/python3" \
  --executable "$APPDIR/usr/bin/gdk-pixbuf-query-loaders" \
  "${libs[@]}" \
  --deploy-deps-only "$APPDIR/usr/lib/python$PYVER/lib-dynload" \
  --deploy-deps-only "$APPDIR/usr/lib/python3/dist-packages" \
  --deploy-deps-only "$APPDIR/usr/lib/gdk-pixbuf-2.0/2.10.0/loaders" \
  --deploy-deps-only "$APPDIR/usr/lib/gio/modules" \
  --desktop-file "$WORK/$APP_ID.desktop" \
  --icon-file "$ROOT/data/icons/hicolor/scalable/apps/$APP_ID.svg" \
  --custom-apprun "$ROOT/packaging/appimage/AppRun" >"$WORK/linuxdeploy.log" 2>&1 || {
    tail -n 40 "$WORK/linuxdeploy.log"; exit 1; }

echo ">> Packing"
mkdir -p "$OUT"
TARGET="$OUT/QuotaGlance-$VERSION-$ARCH.AppImage"
ARCH="$ARCH" VERSION="$VERSION" "$TOOLS/appimagetool" --no-appstream "$APPDIR" "$TARGET" \
  >"$WORK/appimagetool.log" 2>&1 || { tail -n 40 "$WORK/appimagetool.log"; exit 1; }
chmod +x "$TARGET"
echo "$TARGET"
