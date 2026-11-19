#!/bin/bash
# Збірка "YT Grab.app" і "YT-Grab.dmg" на macOS. Запуск: bash build_mac.sh
set -euo pipefail
cd "$(dirname "$0")"

APP="YT Grab"
ARCH="$(uname -m)"                      # arm64 або x86_64
case "$ARCH" in
  arm64)  DENO_T="aarch64-apple-darwin" ;;
  x86_64) DENO_T="x86_64-apple-darwin" ;;
  *) echo "Невідома архітектура $ARCH"; exit 1 ;;
esac

PY="$(command -v python3 || true)"
[ -z "$PY" ] && { echo "Потрібен python3 (встанови: xcode-select --install)"; exit 1; }

echo "==> venv + залежності"
rm -rf .venv build dist dmg_root
"$PY" -m venv .venv
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo "==> ffmpeg + deno у bin/"
mkdir -p bin
FF="$(python -c 'import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())')"
cp "$FF" bin/ffmpeg
chmod +x bin/ffmpeg
curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_T}.zip" -o /tmp/deno.zip
unzip -oq /tmp/deno.zip -d bin
chmod +x bin/deno

echo "==> PyInstaller"
pyinstaller --noconfirm --windowed --name "$APP" \
  --collect-submodules yt_dlp --collect-data yt_dlp_ejs --collect-all yt_dlp_ejs \
  --add-binary "bin/ffmpeg:bin" --add-binary "bin/deno:bin" \
  --exclude-module tkinter \
  app.py

echo "==> підпис (ad-hoc) і DMG"
codesign --force --deep -s - "dist/$APP.app" || true
mkdir dmg_root
cp -R "dist/$APP.app" dmg_root/
ln -s /Applications dmg_root/Applications
OUT="YT-Grab-${ARCH}.dmg"
rm -f "$OUT"
hdiutil create -volname "$APP" -srcfolder dmg_root -ov -format UDZO "$OUT"
echo "Готово: $(pwd)/$OUT"
