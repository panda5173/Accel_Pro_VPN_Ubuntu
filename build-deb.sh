#!/bin/bash
# Build accelpro-vpn_<version>_<arch>.deb
# Usage: ./build-deb.sh /path/to/accelpro_client
set -euo pipefail
VERSION="1.2"
MAINTAINER="${MAINTAINER:-AccelPro VPN Launcher <noreply@example.com>}"
HERE="$(cd "$(dirname "$0")" && pwd)"

need() { command -v "$1" >/dev/null || { echo "Missing tool: $1 (sudo apt install $2)"; exit 1; }; }
need dpkg-deb dpkg; need python3 python3; need gzip gzip; need md5sum coreutils
ARCH="$(dpkg --print-architecture)"

CLIENT="${1:-}"
[ -z "$CLIENT" ] && CLIENT="$(ls -t "$HERE"/accelpro_client "$HOME"/Downloads/*accelpro*client* 2>/dev/null | grep -v '\.zip$' | head -1 || true)"
[ -n "$CLIENT" ] && [ -f "$CLIENT" ] || { echo "AccelPro Linux client not found. Usage: $0 /path/to/accelpro_client"; exit 1; }
[ "$(head -c4 "$CLIENT" | od -An -tx1 | tr -d ' \n')" = "7f454c46" ] || { echo "$CLIENT is not a Linux executable"; exit 1; }
echo "==> AccelPro client : $CLIENT"
echo "==> Architecture    : $ARCH"

BUILD="$HERE/build"; ROOT="$BUILD/root"; APPD="$ROOT/opt/accelpro-vpn"
rm -rf "$BUILD"; mkdir -p "$ROOT/DEBIAN"
install -d "$APPD/lib" "$ROOT/usr/bin" "$ROOT/usr/share/applications" "$ROOT/usr/share/icons/hicolor/256x256/apps" \
  "$ROOT/usr/share/polkit-1/actions" "$ROOT/etc/accelpro-vpn" "$ROOT/usr/share/doc/accelpro-vpn"

install -m 0644 "$HERE/src/accelpro_vpn.py"   "$APPD/accelpro_vpn.py"
install -m 0644 "$HERE/src/accelpro-vpn.png"  "$APPD/accelpro-vpn.png"
install -m 0755 "$HERE/src/accelpro-helper"   "$APPD/accelpro-helper"
install -m 0755 "$CLIENT"                     "$APPD/accelpro_client"
install -m 0755 "$HERE/src/accelpro-vpn"      "$ROOT/usr/bin/accelpro-vpn"
install -m 0644 "$HERE/src/accelpro-vpn.desktop" "$ROOT/usr/share/applications/accelpro-vpn.desktop"
install -m 0644 "$HERE/src/accelpro-vpn.png"  "$ROOT/usr/share/icons/hicolor/256x256/apps/accelpro-vpn.png"
install -m 0644 "$HERE/src/io.github.accelprovpn.policy" "$ROOT/usr/share/polkit-1/actions/io.github.accelprovpn.policy"
install -m 0644 "$HERE/config/defaults.json"  "$ROOT/etc/accelpro-vpn/defaults.json"
install -m 0644 "$HERE/packaging/debian/copyright" "$ROOT/usr/share/doc/accelpro-vpn/copyright"
gzip -9n -c "$HERE/packaging/debian/changelog" > "$ROOT/usr/share/doc/accelpro-vpn/changelog.gz"

# Vendored pystray (pure Python, LGPL) - not packaged in every Debian/Ubuntu release
python3 - "$HERE"/vendor/pystray-*.whl "$APPD/lib" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    for n in z.namelist():
        if n.startswith('pystray/'):
            z.extract(n, sys.argv[2])
PY
find "$APPD/lib" -type d -exec chmod 0755 {} +; find "$APPD/lib" -type f -exec chmod 0644 {} +
python3 -m py_compile "$APPD/accelpro_vpn.py" && rm -rf "$APPD/__pycache__"
bash -n "$APPD/accelpro-helper"

# Shared libraries used by the AccelPro client -> Depends:
CLIENTDEPS=""
for lib in $(ldd "$CLIENT" 2>/dev/null | awk '/=>/ {print $3}' | grep '^/' || true); do
  pkg="$(dpkg -S "$(readlink -f "$lib")" 2>/dev/null | head -1 | cut -d: -f1 || true)"
  [ -z "$pkg" ] && pkg="$(dpkg -S "$lib" 2>/dev/null | head -1 | cut -d: -f1 || true)"
  case "$pkg" in ""|libc6|libgcc-s1|libstdc++6) ;; *) CLIENTDEPS="$CLIENTDEPS $pkg";; esac
done
CLIENTDEPS=", libc6$(echo $CLIENTDEPS | tr ' ' '\n' | sort -u | grep -v '^$' | sed 's/^/, /' | tr -d '\n' || true)"
echo "==> Client libraries:${CLIENTDEPS#,}"

SIZE="$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)"
sed -e "s|@VERSION@|$VERSION|" -e "s|@ARCH@|$ARCH|" -e "s|@MAINTAINER@|$MAINTAINER|" \
    -e "s|@SIZE@|$SIZE|" -e "s|@CLIENTDEPS@|$CLIENTDEPS|" "$HERE/packaging/debian/control.in" > "$ROOT/DEBIAN/control"
echo "/etc/accelpro-vpn/defaults.json" > "$ROOT/DEBIAN/conffiles"
for s in postinst prerm postrm; do install -m 0755 "$HERE/packaging/debian/$s" "$ROOT/DEBIAN/$s"; done
(cd "$ROOT" && find . -type f ! -path './DEBIAN/*' -printf '%P\0' | sort -z | xargs -0 md5sum > DEBIAN/md5sums)
chmod 0644 "$ROOT/DEBIAN/md5sums" "$ROOT/DEBIAN/control" "$ROOT/DEBIAN/conffiles"

OUT="$HERE/accelpro-vpn_${VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$OUT" >/dev/null
[ -n "${KEEP:-}" ] || rm -rf "$BUILD"
echo
echo "==> Built: $OUT"
echo "Install:  sudo apt install ./$(basename "$OUT")"
