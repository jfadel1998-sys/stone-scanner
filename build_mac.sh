#!/usr/bin/env bash
# Build the standalone Stone Scanner app (onedir) on macOS and bundle Chromium.
# MUST be run ON a Mac — PyInstaller cannot cross-compile from Windows/Linux.
#
# Usage:  ./build_mac.sh              (full build)
#         ./build_mac.sh --skip-build (only re-copy Chromium into an existing dist)
#
# Prereqs (once, in the repo root on the Mac):
#   python3 -m venv .venv && source .venv/bin/activate
#   pip install -r requirements.txt pywebview pyinstaller
#   python -m playwright install chromium      # matching Chromium into ~/Library/Caches/ms-playwright
#   # (stonescan.db must exist in the repo root — copy a snapshot or crawl first.)
set -euo pipefail
cd "$(dirname "$0")"

PYI="./.venv/bin/pyinstaller"
if [ ! -x "$PYI" ]; then
  echo "error: $PYI not found. Create the venv and 'pip install pyinstaller' first." >&2
  exit 1
fi

if [ "${1:-}" != "--skip-build" ]; then
  echo "== Building with PyInstaller (this takes a few minutes) =="
  "$PYI" stonescan.spec --noconfirm
fi

DIST="$PWD/dist/StoneScanner"
BROWSERS="$DIST/browsers"
mkdir -p "$BROWSERS"

SRC="$HOME/Library/Caches/ms-playwright"
if [ ! -d "$SRC" ]; then
  echo "error: $SRC not found. Run 'python -m playwright install chromium' first." >&2
  exit 1
fi

echo "== Copying Chromium from $SRC into the app =="
# Copy each revision dir verbatim: chromium-<rev>, chromium_headless_shell-<rev>
# (headless crawl needs it), ffmpeg-<rev>. No 'winldd' on macOS (Windows-only).
# cp -R (capital) preserves the Chromium.app internal symlinks; -r would flatten them.
shopt -s nullglob
for d in "$SRC"/chromium* "$SRC"/ffmpeg*; do
  name="$(basename "$d")"
  dest="$BROWSERS/$name"
  if [ ! -e "$dest" ]; then cp -R "$d" "$dest"; fi
  echo "   + $name"
done

echo "== Double-click launcher (a raw onedir binary opens Terminal in Finder) =="
cat > "$DIST/StoneScanner.command" <<'LAUNCH'
#!/usr/bin/env bash
# Double-click me. cd into the app folder so data/ and browsers/ resolve correctly.
cd "$(dirname "$0")"
exec ./StoneScanner
LAUNCH
chmod +x "$DIST/StoneScanner.command"

echo "== Gatekeeper: strip quarantine + restore exec bits =="
# So a Finder-copied folder launches cleanly, and the unsigned bundled Chromium.app
# doesn't throw "Chromium is damaged and can't be opened".
xattr -dr com.apple.quarantine "$DIST" 2>/dev/null || true
chmod +x "$DIST/StoneScanner" 2>/dev/null || true
find "$BROWSERS" -type d -name "*.app" -print0 | while IFS= read -r -d '' app; do
  find "$app/Contents/MacOS" -type f -exec chmod +x {} \; 2>/dev/null || true
done
# Ad-hoc re-sign the bundled Chromium.app (covers zipped/redownloaded copies). PyInstaller
# already ad-hoc signs its own binaries. '--deep' can error on nested helpers; non-fatal.
find "$BROWSERS" -type d -name "Chromium.app" -exec \
  codesign --force --deep --sign - {} \; 2>/dev/null || true

cat > "$DIST/READ ME FIRST (macOS).txt" <<'READ'
Stone Scanner — macOS

Run it:
  • Double-click "StoneScanner.command"  (or run ./StoneScanner in Terminal).
  • A "data" folder is created next to the app for the database and supplier list;
    the bundled snapshot loads immediately.

If macOS blocks it ("unidentified developer" / "damaged and can't be opened"):
  This build is ad-hoc signed, not Apple-notarized. On first run either:
    • Right-click the app → Open → Open (once), OR
    • In Terminal, from this folder:   xattr -dr com.apple.quarantine .

Architecture:
  This folder is built for ONE CPU type (Apple Silicon arm64 OR Intel x86_64).
  An arm64 build runs natively on M-series Macs; an x86_64 build runs on Intel
  (and on Apple Silicon only via Rosetta 2).

USB / external drives (important):
  The app contains symlinks and needs POSIX exec bits, which exFAT/FAT32 USB
  sticks DROP. Keep it on a Mac-formatted (APFS/HFS+) drive, or copy the folder
  to the internal disk before running. A single exFAT stick cannot hold both a
  working Windows .exe folder and a working Mac app folder.
READ

SIZE="$(du -sh "$DIST" | cut -f1)"
echo ""
echo "Done. App folder: $DIST  ($SIZE)"
echo "Run it:  open \"$DIST/StoneScanner.command\"   (or ./dist/StoneScanner/StoneScanner)"
