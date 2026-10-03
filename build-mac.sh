#!/bin/bash
# DropIt macOS build script.
# Apple requires Mac apps to be compiled ON a Mac, so run this script on the
# Mac itself (double-click won't work -- run it from Terminal):
#
#   cd /path/to/dropit && ./build-mac.sh
#
# It installs the needed Python packages and produces dist/DropIt.app,
# which you can drag into Applications. The app is built for the chip of the
# Mac it runs on (Apple Silicon or Intel).
set -e
cd "$(dirname "$0")"

echo "==> Checking Python 3..."
python3 --version || { echo "Install Python 3 from https://www.python.org/downloads/ first."; exit 1; }

echo "==> Installing dependencies..."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

echo "==> Building DropIt.app (takes a few minutes)..."
python3 -m PyInstaller --noconfirm --windowed --name DropIt \
  --add-data "icon.png:." \
  app.py

echo ""
echo "Done! Your app is at: dist/DropIt.app"
echo "Drag it into Applications, then launch it like any other Mac app."
