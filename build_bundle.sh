#!/bin/zsh
# Declaration: Code generated using Anthropic Claude (Sonnet 5.5)
set -e
cd "$(dirname "$0")"
VENV="../.venv"
"$VENV/bin/python" -m pip install --quiet pyinstaller
rm -rf build dist
"$VENV/bin/python" -m PyInstaller --onefile --collect-all pypdfium2 --collect-all pypdfium2_raw --name tropy_ocr --distpath dist --workpath build --specpath build tropy_ocr_plugin.py
mkdir -p bin
cp dist/tropy_ocr bin/tropy_ocr
codesign --force --sign - bin/tropy_ocr
rm -rf build dist
ls -lh bin/tropy_ocr
