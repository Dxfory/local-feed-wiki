#!/usr/bin/env bash
# Assemble the English offline demo into a static sample site.
# The run uses a temporary root, as scripts/make_demo.sh does, and does not
# launch a browser or ffmpeg. The destination directory is replaced only
# after that run publishes a digest.

set -euo pipefail

REPO=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

if [[ $# -ne 1 || -z "$1" ]]; then
  fail "usage: scripts/build_sample_site.sh DEST"
fi

DEST=$1
if [[ "$DEST" != /* ]]; then
  DEST=$(pwd)/$DEST
fi
case "$DEST" in
  *'//'*|*'..'*) fail "destination is not a local directory" ;;
esac
if [[ "$DEST" == "/" || "$DEST" == "$REPO" || "$DEST" == "$REPO/" ]]; then
  fail "destination is not a local directory"
fi
case "$REPO/" in
  "$DEST"/*) fail "destination contains the checkout" ;;
esac
if [[ -L "$DEST" ]]; then
  fail "destination is a symlink"
fi
if [[ -e "$DEST" && ! -d "$DEST" ]]; then
  fail "destination is not a directory"
fi
if [[ -e "$DEST/.git" ]]; then
  fail "destination contains a git directory"
fi
parent=$(dirname -- "$DEST")
if [[ "$parent" == "/" || -L "$parent" || ! -d "$parent" ]]; then
  fail "destination parent is not a directory"
fi

cd "$REPO"

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 is required to run the offline demo"
fi
if [[ -n "${PYTHONPATH:-}" ]]; then
  export PYTHONPATH="$REPO/src:${PYTHONPATH}"
else
  export PYTHONPATH="$REPO/src"
fi
export PYTHONDONTWRITEBYTECODE=1
if ! python3 -c "import feedparser, yaml" >/dev/null 2>&1; then
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

mkdir -p "$WORKDIR/tests/fakes" "$WORKDIR/tests/fixtures" "$WORKDIR/examples"
if ! ln "$REPO/tests/fakes/grok" "$WORKDIR/tests/fakes/grok" 2>/dev/null; then
  cp -a "$REPO/tests/fakes/grok" "$WORKDIR/tests/fakes/grok"
fi
if [[ -L "$WORKDIR/tests/fakes/grok" || ! -f "$WORKDIR/tests/fakes/grok" ]]; then
  fail "could not place a regular offline binary in the temporary root"
fi
cp -a "$REPO/examples/demo" "$WORKDIR/examples/demo"
cp -a "$REPO/tests/fixtures/demo" "$WORKDIR/tests/fixtures/demo"

export SECOND_BRAIN_GROK="tests/fakes/grok"
unset SECOND_BRAIN_GROK_SCENARIO || true
unset SECOND_BRAIN_GROK_ARGV_LOG || true
unset SECOND_BRAIN_GROK_HELP || true
unset SECOND_BRAIN_GROK_HELP_STATUS || true
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

mkdir -p "$WORKDIR/site"
DEMO_STDOUT="$WORKDIR/stdout.txt" \
DEMO_WIKI="$WORKDIR/wiki" \
SAMPLE_DEST="$WORKDIR/site" \
python3 - <<'PY'
import os
import sys
from pathlib import Path

stdout = Path(os.environ["DEMO_STDOUT"]).read_text(encoding="utf-8").strip()
wiki = Path(os.environ["DEMO_WIKI"])
dest = Path(os.environ["SAMPLE_DEST"])
if "\n" in stdout or stdout == "":
    sys.exit("offline demo status was not success")
fields = {}
for part in stdout.split(" "):
    key, value = part.split("=", 1)
    fields[key] = value
if fields.get("status") != "success":
    sys.exit("offline demo status was not success")
relative = fields.get("html", "")
digest = wiki / relative
parts = Path(relative).parts
if (
    not digest.is_file()
    or digest.is_symlink()
    or relative.startswith(("/", "\\"))
    or ".." in parts
    or not parts
):
    sys.exit("offline demo did not publish a digest")
page = digest.read_text(encoding="utf-8")
markers = (
    ("<h2>非共识</h2>", "<p>无</p>", "少数候选"),
    ("<h2>Minority view</h2>", "<p>None</p>", "Minority candidate"),
)
found = []
for heading, empty, label in markers:
    at = page.find(heading)
    if at >= 0:
        found.append((at, heading, empty, label))
if not found:
    sys.exit("digest has no minority section")
start, marker, empty, label = min(found)
end = page.find("<h2>", start + len(marker))
if end < 0:
    sys.exit("digest minority section is incomplete")
section = page[start:end]
if empty in section or label not in section:
    sys.exit("digest minority section is empty")
opportunities = sorted(
    path
    for path in wiki.glob("snapshots/*/opportunities/*.md")
    if path.is_file() and not path.is_symlink()
)
if len(opportunities) != 1:
    sys.exit("expected one minority comparison page")
topics = [
    path
    for path in wiki.glob("snapshots/*/topics/*.md")
    if path.is_file() and not path.is_symlink()
]
if not topics:
    sys.exit("offline demo did not publish a topic page")
index_md = wiki / "index.md"
if not index_md.is_file() or index_md.is_symlink():
    sys.exit("offline demo did not publish an index")

for path in sorted(wiki.rglob("*")):
    relative_page = path.relative_to(wiki)
    if ".staging" in relative_page.parts:
        continue
    if path.is_symlink():
        sys.exit("wiki page is a symlink")
    if path.is_dir():
        continue
    if not path.is_file():
        sys.exit("wiki page is not a regular file")
    if path.suffix not in {".html", ".md"}:
        continue
    if any(part in {"", ".", ".."} for part in relative_page.parts):
        sys.exit("wiki page escaped")
    target = dest.joinpath(*relative_page.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(path.read_bytes())

index = dest / "index.html"
index.write_bytes(digest.read_bytes())
PY

if [[ -e "$DEST" ]]; then
  rm -rf -- "$DEST"
fi
mkdir -p -- "$DEST"
cp -a "$WORKDIR/site/." "$DEST/"

python3 - "$DEST/index.html" <<'PY'
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

index = Path(sys.argv[1])
if index.is_symlink() or not index.is_file():
    sys.exit("missing sample index")
text = index.read_text(encoding="utf-8")
for raw in re.findall(r"https?://[^\s\"'<>)\]]+", text):
    host = urlsplit(raw.rstrip(".,")).hostname
    if host != "example.com":
        sys.exit("sample index host is not example.com")
PY

printf 'sample_site=%s\n' "$DEST"
