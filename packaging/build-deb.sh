#!/bin/sh
set -eu

VERSION="${1:-0.1.0}"
ARCH=all
PKG="quickvideo_${VERSION}_${ARCH}"
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
BUILD="$ROOT/build/$PKG"
OUT="$ROOT/dist"

rm -rf "$BUILD"
mkdir -p \
  "$BUILD/DEBIAN" \
  "$BUILD/usr/bin" \
  "$BUILD/usr/lib/quickvideo" \
  "$BUILD/usr/share/applications" \
  "$BUILD/usr/share/icons/hicolor/256x256/apps" \
  "$BUILD/usr/share/doc/quickvideo"

cp -a "$ROOT/quickvideo" "$BUILD/usr/lib/quickvideo/"
cp -a "$ROOT/data" "$BUILD/usr/lib/quickvideo/"
cp "$ROOT/run.py" "$BUILD/usr/lib/quickvideo/run.py"
cp "$ROOT/packaging/quickvideo-launcher" "$BUILD/usr/bin/quickvideo"
cp "$ROOT/packaging/quickvideo.desktop" "$BUILD/usr/share/applications/quickvideo.desktop"
cp "$ROOT/data/icons/quickvideo.png" "$BUILD/usr/share/icons/hicolor/256x256/apps/io.github.quickvideo.QuickVideo.png"
cp "$ROOT/data/icons/quickvideo.png" "$BUILD/usr/share/icons/hicolor/256x256/apps/quickvideo.png"
cp "$ROOT/README.md" "$BUILD/usr/share/doc/quickvideo/README.md"

chmod 0755 "$BUILD/DEBIAN"

cat > "$BUILD/DEBIAN/control" <<CONTROL
Package: quickvideo
Version: $VERSION
Section: video
Priority: optional
Architecture: all
Maintainer: QuickVideo Project
Depends: python3, python3-gi, gir1.2-gtk-4.0, ffmpeg, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-libav
Recommends: yt-dlp
Description: Quick GTK frontend for common FFmpeg video edits
 QuickVideo provides a fast graphical interface for trimming, cropping,
 rotating, image and audio adjustments, FFmpeg export, and optional video
 downloads through yt-dlp.
CONTROL

cat > "$BUILD/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
POSTINST
chmod 0755 "$BUILD/DEBIAN/postinst"

cat > "$BUILD/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
POSTRM
chmod 0755 "$BUILD/DEBIAN/postrm"

chmod 0755 "$BUILD/usr/bin/quickvideo"
chmod 0644 "$BUILD/usr/share/applications/quickvideo.desktop"
find "$BUILD" -type d -exec chmod g-s {} +
chmod 0755 "$BUILD/DEBIAN"

mkdir -p "$OUT"
dpkg-deb --root-owner-group --build "$BUILD" "$OUT/${PKG}.deb"
echo "$OUT/${PKG}.deb"
