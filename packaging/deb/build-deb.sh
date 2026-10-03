#!/usr/bin/env bash
# Build an architecture-independent .deb for Ubuntu 24.04+ / Debian 13+.
#
#   packaging/deb/build-deb.sh [output-dir]
#
# Installs the app into /usr/share/quotaglance (private module dir), the
# GNOME Shell extension system-wide, plus desktop entry, AppStream metadata,
# icons and the D-Bus activation file.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$(realpath -m "${1:-$ROOT/dist}")"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/src/quotaglance/__init__.py")"
APP_ID="io.github.rafay_ah.QuotaGlance"
UUID="quotaglance@rafay-ah.github.io"
STAGE="$(mktemp -d)"
PKG="$STAGE/quotaglance_${VERSION}_all"
trap 'rm -rf "$STAGE"' EXIT

install -d "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/usr/share/quotaglance" \
  "$PKG/usr/share/applications" "$PKG/usr/share/metainfo" \
  "$PKG/usr/share/dbus-1/services" "$PKG/usr/share/gnome-shell/extensions" \
  "$PKG/usr/share/doc/quotaglance"

# Application code (byte-compiled at install time by postinst).
cp -r "$ROOT/src/quotaglance" "$PKG/usr/share/quotaglance/"
find "$PKG/usr/share/quotaglance" -name '__pycache__' -type d -prune -exec rm -rf {} +

cat > "$PKG/usr/bin/quotaglance" <<'LAUNCHER'
#!/usr/bin/python3
import sys

sys.path.insert(0, "/usr/share/quotaglance")
from quotaglance.main import main  # noqa: E402

sys.exit(main())
LAUNCHER
chmod 755 "$PKG/usr/bin/quotaglance"

install -m 644 "$ROOT/data/$APP_ID.desktop" "$PKG/usr/share/applications/"
install -m 644 "$ROOT/data/$APP_ID.metainfo.xml" "$PKG/usr/share/metainfo/"
install -m 644 "$ROOT/data/$APP_ID.service" "$PKG/usr/share/dbus-1/services/"
cp -r "$ROOT/data/icons" "$PKG/usr/share/"
cp -r "$ROOT/extension/$UUID" "$PKG/usr/share/gnome-shell/extensions/"
find "$PKG/usr/share" -type f -exec chmod 644 {} +
find "$PKG/usr/share" -type d -exec chmod 755 {} +

cat > "$PKG/usr/share/doc/quotaglance/copyright" <<COPYRIGHT
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: QuotaGlance
Source: https://github.com/rafay-ah/quotaglance

Files: *
Copyright: 2026 rafay-ah
License: MIT
$(sed 's/^/ /; s/^ $/ ./' "$ROOT/LICENSE")
COPYRIGHT
printf 'quotaglance (%s) unstable; urgency=medium\n\n  * Release %s.\n\n -- rafay-ah <54492363+rafay-ah@users.noreply.github.com>  %s\n' \
  "$VERSION" "$VERSION" "$(date -R)" | gzip -9n > "$PKG/usr/share/doc/quotaglance/changelog.Debian.gz"

SIZE="$(du -sk "$PKG/usr" | cut -f1)"
cat > "$PKG/DEBIAN/control" <<CONTROL
Package: quotaglance
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Installed-Size: $SIZE
Depends: python3 (>= 3.10), python3-gi (>= 3.42), python3-gi-cairo, gir1.2-glib-2.0,
 gir1.2-gtk-4.0 (>= 4.12), gir1.2-adw-1 (>= 1.5), gir1.2-secret-1
Recommends: gnome-keyring
Suggests: gnome-shell-extension-appindicator
Maintainer: rafay-ah <54492363+rafay-ah@users.noreply.github.com>
Homepage: https://github.com/rafay-ah/quotaglance
Description: AI coding quotas at a glance for GNOME
 QuotaGlance shows how much of your Claude Code, Codex, Cursor, GitHub
 Copilot, Gemini CLI, Kiro, ElevenLabs, OpenCode and other AI tool limits
 you have used, and when each limit resets: in the top bar, in a desktop
 widget and in a native GTK 4 / libadwaita window, with notifications at
 80% and 95%.
 .
 It reads the sign-ins your tools already keep on this computer and
 stores any API keys in GNOME Keyring.
CONTROL

cat > "$PKG/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
  python3 -m compileall -q /usr/share/quotaglance >/dev/null 2>&1 || true
fi
exit 0
POSTINST
cat > "$PKG/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
set -e
find /usr/share/quotaglance -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
exit 0
PRERM
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/prerm"

mkdir -p "$OUT"
dpkg-deb --root-owner-group --build "$PKG" "$OUT/quotaglance_${VERSION}_all.deb" >/dev/null
echo "$OUT/quotaglance_${VERSION}_all.deb"
