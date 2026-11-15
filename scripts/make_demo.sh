#!/usr/bin/env bash
# Rebuild the English offline demo, then screenshot the digest and assemble a short GIF.
# Writes docs/assets/digest.png and docs/assets/demo.gif. Uses a temporary root, never
# the checkout's data/, raw/, or wiki/.

set -euo pipefail

REPO=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$REPO"

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

CHROME=""
for candidate in chromium chromium-browser google-chrome; do
  if command -v "$candidate" >/dev/null 2>&1; then
    CHROME=$(command -v "$candidate")
    break
  fi
done
if [[ -z "$CHROME" ]]; then
  fail "headless Chrome is required to render docs/assets/digest.png (tried chromium, chromium-browser, google-chrome)"
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  fail "ffmpeg is required to assemble docs/assets/demo.gif"
fi
if ! command -v ffprobe >/dev/null 2>&1; then
  fail "ffprobe is required to check that docs/assets/demo.gif stays within 30 seconds"
fi
if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 is required to run the offline demo"
fi
if ! PYTHONPATH="$REPO/src" python3 -c "import feedparser, yaml" >/dev/null 2>&1; then
  fail "python3 is missing feedparser or PyYAML; install the pinned packages before running the demo"
fi
if [[ -L "$REPO/tests/fakes/grok" || ! -f "$REPO/tests/fakes/grok" ]]; then
  fail "tests/fakes/grok must be the regular offline binary"
fi

WORKDIR=$(mktemp -d)
cleanup() {
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

mkdir -p "$WORKDIR/tests/fakes" "$WORKDIR/tests/fixtures" "$WORKDIR/examples" "$WORKDIR/frames"
if ! ln "$REPO/tests/fakes/grok" "$WORKDIR/tests/fakes/grok" 2>/dev/null; then
  cp -a "$REPO/tests/fakes/grok" "$WORKDIR/tests/fakes/grok"
fi
if [[ -L "$WORKDIR/tests/fakes/grok" || ! -f "$WORKDIR/tests/fakes/grok" ]]; then
  fail "could not place a regular offline binary in the temporary root"
fi
cp -a "$REPO/examples/demo" "$WORKDIR/examples/demo"
cp -a "$REPO/tests/fixtures/demo" "$WORKDIR/tests/fixtures/demo"

export PYTHONPATH="$REPO/src"
export SECOND_BRAIN_GROK="tests/fakes/grok"
unset SECOND_BRAIN_GROK_SCENARIO || true
unset SECOND_BRAIN_GROK_ARGV_LOG || true
unset SECOND_BRAIN_ROOT || true

set +e
python3 -m second_brain.cli run --offline \
  --config-dir examples/demo \
  --now 2030-01-15T00:00:00Z \
  --db data/second_brain.sqlite \
  --root "$WORKDIR" \
  >"$WORKDIR/stdout.txt" 2>"$WORKDIR/stderr.txt"
status=$?
set -e
if [[ "$status" -ne 0 ]]; then
  cat "$WORKDIR/stdout.txt" >&2 || true
  cat "$WORKDIR/stderr.txt" >&2 || true
  fail "offline demo failed with exit ${status}"
fi

DEMO_STDOUT="$WORKDIR/stdout.txt" \
DEMO_STDERR="$WORKDIR/stderr.txt" \
DEMO_WIKI="$WORKDIR/wiki" \
DEMO_FRAMES="$WORKDIR/frames" \
python3 - <<'PY'
import html
import os
import sys
from pathlib import Path

stdout = Path(os.environ["DEMO_STDOUT"]).read_text(encoding="utf-8").strip()
stderr = Path(os.environ["DEMO_STDERR"]).read_text(encoding="utf-8").strip()
wiki = Path(os.environ["DEMO_WIKI"])
frames = Path(os.environ["DEMO_FRAMES"])
fields = dict(part.split("=", 1) for part in stdout.split(" "))
if fields.get("status") != "success":
    sys.exit("offline demo status was not success")
relative = fields.get("html", "")
digest = wiki / relative
if not digest.is_file() or relative.startswith("/") or ".." in Path(relative).parts:
    sys.exit("offline demo did not publish a digest")
page = digest.read_text(encoding="utf-8")
marker = "<h2>非共识</h2>"
start = page.find(marker)
if start < 0:
    sys.exit("digest has no minority section")
end = page.find("<h2>", start + len(marker))
if end < 0:
    sys.exit("digest minority section is incomplete")
section = page[start:end]
if "<p>无</p>" in section or "少数候选" not in section:
    sys.exit("digest minority section is empty")
opportunities = sorted(wiki.glob("snapshots/*/opportunities/*.md"))
if len(opportunities) != 1:
    sys.exit("expected one minority comparison page")
comparison = opportunities[0].read_text(encoding="utf-8")
command = (
    "PYTHONPATH=src SECOND_BRAIN_GROK=tests/fakes/grok \\\n"
    "  python3 -m second_brain.cli run --offline \\\n"
    "  --config-dir examples/demo \\\n"
    "  --now 2030-01-15T00:00:00Z \\\n"
    "  --db data/second_brain.sqlite"
)
shown = command + "\n\n" + stdout
if stderr:
    shown += "\n\n" + stderr
style = (
    "body{margin:0;background:#f7f4ee;color:#1c1915;}"
    "main{padding:28px 36px;}"
    "h1{font-family:Georgia,serif;font-size:32px;font-weight:600;margin:0 0 18px;}"
    "pre{font-family:ui-monospace,monospace;font-size:18px;line-height:1.45;white-space:pre-wrap;}"
    "article{font-family:Georgia,serif;font-size:20px;line-height:1.45;}"
    "h2{font-size:28px;margin:0 0 12px;}"
    "li{margin:6px 0;}"
)
frames.joinpath("command.html").write_text(
    "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
    f"<title>Demo command</title>\n<style>{style}</style>\n</head>\n<body>\n<main>\n"
    "<h1>Run the offline demo</h1>\n"
    f"<pre>{html.escape(shown)}</pre>\n"
    "</main>\n</body>\n</html>\n",
    encoding="utf-8",
)
frames.joinpath("minority.html").write_text(
    "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
    f"<title>Minority comparison</title>\n<style>{style}</style>\n</head>\n<body>\n<main>\n"
    "<article>\n"
    f"{section}\n"
    "</article>\n"
    "<h1>Comparison page</h1>\n"
    f"<pre>{html.escape(comparison)}</pre>\n"
    "</main>\n</body>\n</html>\n",
    encoding="utf-8",
)
frames.joinpath("digest.path").write_text(str(digest.resolve()), encoding="utf-8")
PY

shot() {
  local width="$1"
  local height="$2"
  local src="$3"
  local dest="$4"
  local err="$WORKDIR/chrome.err"
  if ! "$CHROME" \
    --headless \
    --disable-gpu \
    --no-sandbox \
    --disable-dev-shm-usage \
    --hide-scrollbars \
    --force-device-scale-factor=1 \
    --default-background-color=FFFFFF \
    --virtual-time-budget=3000 \
    --window-size="${width},${height}" \
    --screenshot="$dest" \
    "file://${src}" \
    >"$WORKDIR/chrome.out" 2>"$err"; then
    cat "$err" >&2 || true
    fail "headless Chrome could not screenshot ${src}"
  fi
  if [[ ! -s "$dest" ]]; then
    cat "$err" >&2 || true
    fail "headless Chrome did not write ${dest}"
  fi
}

DIGEST=$(cat "$WORKDIR/frames/digest.path")
shot 1100 6400 "$DIGEST" "$WORKDIR/frames/digest.png"
python3 - "$WORKDIR/frames/digest.png" <<'PY'
import os
import subprocess
import sys

path = sys.argv[1]
probe = subprocess.check_output(
    [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        path,
    ],
    text=True,
).strip()
width, height = (int(part) for part in probe.split("x"))
raw = subprocess.check_output(
    ["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
)
stride = width * 3
if len(raw) < stride * height:
    sys.exit("could not read the digest screenshot")
last = 0
for y in range(height):
    row = raw[y * stride : (y + 1) * stride]
    if any(pixel < 250 for pixel in row):
        last = y
crop_h = min(height, last + 48)
if crop_h < 400:
    sys.exit("digest screenshot looks blank")
if crop_h < height:
    trimmed = path + ".crop.png"
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            path,
            "-vf",
            f"crop={width}:{crop_h}:0:0",
            trimmed,
        ]
    )
    os.replace(trimmed, path)
PY
shot 1100 980 "$WORKDIR/frames/command.html" "$WORKDIR/frames/command.png"
shot 1100 980 "$DIGEST" "$WORKDIR/frames/digest-top.png"
shot 1100 980 "$WORKDIR/frames/minority.html" "$WORKDIR/frames/minority.png"

cat >"$WORKDIR/frames/list.txt" <<EOF
file '$WORKDIR/frames/command.png'
duration 4
file '$WORKDIR/frames/digest-top.png'
duration 4
file '$WORKDIR/frames/minority.png'
duration 4
file '$WORKDIR/frames/minority.png'
EOF

ffmpeg -y -f concat -safe 0 -i "$WORKDIR/frames/list.txt" \
  -vf "fps=4,scale=960:760:force_original_aspect_ratio=decrease,pad=960:760:(ow-iw)/2:(oh-ih)/2:color=0xF7F4EE,split[s0][s1];[s0]palettegen=stats_mode=diff:max_colors=128[p];[s1][p]paletteuse" \
  -loop 0 \
  "$WORKDIR/frames/demo.gif" \
  >"$WORKDIR/ffmpeg.out" 2>"$WORKDIR/ffmpeg.err"

python3 - "$WORKDIR/frames/demo.gif" <<'PY'
import os
import struct
import subprocess
import sys

path = sys.argv[1]
size = os.path.getsize(path)
if size > 5 * 1024 * 1024:
    sys.exit(f"demo.gif is {size} bytes, over the 5 MB limit")
probe = subprocess.run(
    [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        path,
    ],
    check=True,
    capture_output=True,
    text=True,
)
duration = float(probe.stdout.strip())
if duration > 30:
    sys.exit(f"demo.gif lasts {duration:.2f}s, over the 30s limit")
png = os.path.join(os.path.dirname(path), "digest.png")
with open(png, "rb") as handle:
    header = handle.read(24)
if header[:8] != b"\x89PNG\r\n\x1a\n":
    sys.exit("digest screenshot is not a PNG")
width, height = struct.unpack(">II", header[16:24])
if width < 100 or height < 100:
    sys.exit("digest screenshot is unexpectedly small")
print(f"demo.gif {size} bytes, {duration:.2f}s; digest.png {width}x{height}")
PY

mkdir -p "$REPO/docs/assets"
cp -f "$WORKDIR/frames/digest.png" "$REPO/docs/assets/digest.png"
cp -f "$WORKDIR/frames/demo.gif" "$REPO/docs/assets/demo.gif"
