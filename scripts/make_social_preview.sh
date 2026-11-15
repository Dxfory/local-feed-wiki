#!/usr/bin/env bash
# Render docs/assets/social-preview.html to docs/assets/social-preview.png.
# Uses a local Chrome or Chromium binary. The page has no network assets.

set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
HTML="${ROOT}/docs/assets/social-preview.html"
OUT="${ROOT}/docs/assets/social-preview.png"

if [[ ! -f "$HTML" ]]; then
  printf 'error: missing social preview source: %s\n' "$HTML" >&2
  exit 1
fi

browser=""
for name in chromium chromium-browser google-chrome; do
  if command -v "$name" >/dev/null 2>&1; then
    browser="$(command -v "$name")"
    break
  fi
done

if [[ -z "$browser" ]]; then
  printf 'error: headless Chrome/Chromium not found. Tried chromium, chromium-browser, and google-chrome.\n' >&2
  exit 1
fi

workdir="$(mktemp -d)"
cleanup() {
  rm -rf "$workdir"
}
trap cleanup EXIT

render() {
  (
    cd "$workdir"
    "$browser" \
      --headless \
      --screenshot \
      --window-size=1280,640 \
      --hide-scrollbars \
      --force-device-scale-factor=1 \
      --disable-gpu \
      "$@" \
      "$HTML"
  )
}

if ! render >"$workdir/log.txt" 2>&1 || [[ ! -s "$workdir/screenshot.png" ]]; then
  if ! render --no-sandbox >"$workdir/log-nosandbox.txt" 2>&1 || [[ ! -s "$workdir/screenshot.png" ]]; then
    printf 'error: %s did not write a screenshot\n' "$browser" >&2
    if [[ -f "$workdir/log.txt" ]]; then
      cat "$workdir/log.txt" >&2
    fi
    if [[ -f "$workdir/log-nosandbox.txt" ]]; then
      cat "$workdir/log-nosandbox.txt" >&2
    fi
    exit 1
  fi
fi

mkdir -p "$(dirname "$OUT")"
cp "$workdir/screenshot.png" "$OUT"
