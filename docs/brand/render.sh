#!/bin/sh
# render.sh <html> <width> <height> <out.png>  (transparent background)
BIN="$HOME/Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell"
"$BIN" --headless --disable-gpu --hide-scrollbars --default-background-color=00000000 \
  --force-device-scale-factor=1 --window-size="$2,$3" --screenshot="$4" "file://$1" >/dev/null 2>&1
