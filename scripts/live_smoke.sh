#!/bin/sh
# 本地最小冒烟。默认离线，只使用仓库里的 tests/fakes/grok。
# 真实 grok 必须同时传入 --live、--budget 和 --max-items，且 N 只能是 1、2 或 3。
# 数据写在临时目录，调用 second_brain.cli.main。不读取真实订阅，不对外发布，不读凭据。
# 在线二进制只来自 SECOND_BRAIN_GROK。未设置时 CLI 拒绝，不探测本机路径。
# 脚本不改 live_authorized、billing_path_verified、tool_policy_verified。
# 在线授权默认只读仓库里的 config/runtime.yaml。
# 设置 SECOND_BRAIN_SMOKE_RUNTIME 时改为只读该路径，判断不变。
# 说明见 README「真实 grok 冒烟」。不证明账单或工具策略。

set -eu

REPO=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
FAKE=$REPO/tests/fakes/grok

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

trim() {
  printf '%s' "$1" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

grok_is_fake() {
  [ -n "${SECOND_BRAIN_GROK:-}" ] || return 1
  candidate=$SECOND_BRAIN_GROK
  case "$candidate" in
    /*) ;;
    *) candidate=$REPO/$candidate ;;
  esac
  [ -L "$candidate" ] && return 1
  [ -L "$FAKE" ] && return 1
  [ -f "$candidate" ] || return 1
  [ "$candidate" -ef "$FAKE" ]
}

in_cap() {
  case "$1" in
    1|2|3) return 0 ;;
    *) return 1 ;;
  esac
}

make_offline_root() {
  candidate=$1
  mkdir -p "$candidate/tests/fakes" || return 1
  if ln "$FAKE" "$candidate/tests/fakes/grok" 2>/dev/null; then
    printf '%s\n' "$candidate"
    return 0
  fi
  rm -rf "$candidate"
  return 1
}

new_dir() {
  parent=$1
  [ -d "$parent" ] || return 1
  mktemp -d "$parent/second-brain-smoke.XXXXXX"
}

offline=0
live=0
budget=
max_items=

while [ $# -gt 0 ]; do
  case "$1" in
    --offline) offline=1 ;;
    --live) live=1 ;;
    --budget)
      shift
      [ $# -gt 0 ] || fail missing_value
      budget=$1
      ;;
    --max-items)
      shift
      [ $# -gt 0 ] || fail missing_value
      max_items=$1
      ;;
    *) fail unknown_argument ;;
  esac
  shift
done

if [ "$offline" -eq 1 ] && [ "$live" -eq 1 ]; then
  fail conflicting_mode
fi

if [ "$live" -eq 1 ]; then
  mode=live
else
  mode=offline
fi

if [ "$mode" = live ]; then
  [ -n "$budget" ] || fail missing_budget
  [ -n "$max_items" ] || fail missing_item_cap
else
  [ -n "$budget" ] || budget=1
  [ -n "$max_items" ] || max_items=3
fi

in_cap "$budget" || fail budget_cap
in_cap "$max_items" || fail item_cap

if [ "$mode" = offline ] || grok_is_fake; then
  if [ "$mode" = live ]; then
    fail offline_grok_rejected
  fi
  if [ -n "${SECOND_BRAIN_GROK:-}" ] && ! grok_is_fake; then
    fail offline_grok_rejected
  fi
  [ -L "$FAKE" ] && fail offline_grok_rejected
  [ -f "$FAKE" ] || fail offline_grok_rejected
fi

if [ "$mode" = live ]; then
  if [ -n "${SECOND_BRAIN_SMOKE_RUNTIME:-}" ]; then
    runtime=$SECOND_BRAIN_SMOKE_RUNTIME
  else
    runtime=$REPO/config/runtime.yaml
  fi
  if [ -L "$runtime" ] || [ ! -f "$runtime" ]; then
    fail live_not_authorized
  fi
  live_authorized=
  billing_path=
  billing_path_verified=
  tool_policy_verified=
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ""|\#*) continue ;;
      billing_path_verified:*) billing_path_verified=${line#billing_path_verified:} ;;
      tool_policy_verified:*) tool_policy_verified=${line#tool_policy_verified:} ;;
      live_authorized:*) live_authorized=${line#live_authorized:} ;;
      billing_path:*) billing_path=${line#billing_path:} ;;
    esac
  done < "$runtime"
  live_authorized=$(trim "$live_authorized")
  billing_path=$(trim "$billing_path")
  billing_path_verified=$(trim "$billing_path_verified")
  tool_policy_verified=$(trim "$tool_policy_verified")
  if [ "$live_authorized" != true ] || [ "$billing_path_verified" != true ] || [ "$tool_policy_verified" != true ]; then
    fail live_not_authorized
  fi
  if [ "$billing_path" != session ] && [ "$billing_path" != api ]; then
    fail live_not_authorized
  fi
  if [ -n "${SECOND_BRAIN_GROK:-}" ]; then
    case "$SECOND_BRAIN_GROK" in
      *..*) fail invalid_grok_binary ;;
      /*) ;;
      *) SECOND_BRAIN_GROK=$REPO/$SECOND_BRAIN_GROK ;;
    esac
    export SECOND_BRAIN_GROK
  fi
fi

command -v python3 >/dev/null 2>&1 || fail io

root=
if [ "$mode" = offline ]; then
  if [ -n "${TMPDIR:-}" ]; then
    try=$(new_dir "$TMPDIR") || try=
    if [ -n "$try" ]; then
      root=$(make_offline_root "$try") || root=
    fi
  fi
  if [ -z "$root" ]; then
    try=$(new_dir "$(dirname "$REPO")") || fail io
    root=$(make_offline_root "$try") || fail io
  fi
  export SECOND_BRAIN_GROK=$root/tests/fakes/grok
else
  if [ -n "${TMPDIR:-}" ]; then
    root=$(new_dir "$TMPDIR") || root=
  fi
  if [ -z "$root" ]; then
    root=$(new_dir "$(dirname "$REPO")") || fail io
  fi
fi

mkdir -p "$root/config" "$root/tests/fixtures/rss"
cp "$REPO/config/preferences.example.md" "$root/config/preferences.md"
cp "$REPO/config/topic_aliases.example.yaml" "$root/config/topic_aliases.yaml"
cat > "$root/config/sources.yaml" <<'EOF'
sources:
  - id: smoke-rss
    kind: file
    file_format: rss
    url: file:tests/fixtures/rss/feed.xml
    title: 冒烟订阅
    backfill_days: 14
EOF

if [ "$mode" = offline ]; then
  cat > "$root/config/runtime.yaml" <<EOF
offline: true
live_authorized: false
billing_path: test
billing_path_verified: false
tool_policy_verified: false
review_llm: true
max_classify_batches: $budget
max_review_batches: $budget
max_transcript_fetches: $budget
EOF
else
  seen_classify=0
  seen_review=0
  seen_transcript=0
  : > "$root/config/runtime.yaml"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      max_classify_batches:*)
        printf 'max_classify_batches: %s\n' "$budget" >> "$root/config/runtime.yaml"
        seen_classify=1
        ;;
      max_review_batches:*)
        printf 'max_review_batches: %s\n' "$budget" >> "$root/config/runtime.yaml"
        seen_review=1
        ;;
      max_transcript_fetches:*)
        printf 'max_transcript_fetches: %s\n' "$budget" >> "$root/config/runtime.yaml"
        seen_transcript=1
        ;;
      *)
        printf '%s\n' "$line" >> "$root/config/runtime.yaml"
        ;;
    esac
  done < "$runtime"
  if [ "$seen_classify" -eq 0 ]; then
    printf 'max_classify_batches: %s\n' "$budget" >> "$root/config/runtime.yaml"
  fi
  if [ "$seen_review" -eq 0 ]; then
    printf 'max_review_batches: %s\n' "$budget" >> "$root/config/runtime.yaml"
  fi
  if [ "$seen_transcript" -eq 0 ]; then
    printf 'max_transcript_fetches: %s\n' "$budget" >> "$root/config/runtime.yaml"
  fi
fi

trimmer=$root/.trim.py
cat > "$trimmer" <<'PY'
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

source, dest, limit_text, mode = sys.argv[1:]
limit = int(limit_text)
text = Path(source).read_text(encoding="utf-8")
marker = "<item>"
if marker not in text:
    sys.stderr.write("invalid_feed\n")
    raise SystemExit(1)
head, *chunks = text.split(marker)
closing = ""
items = []
for index, chunk in enumerate(chunks):
    if index == len(chunks) - 1 and "</channel>" in chunk:
        item, sep, tail = chunk.partition("</channel>")
        items.append(item)
        closing = sep + tail
    else:
        items.append(chunk)
if not closing or not items:
    sys.stderr.write("invalid_feed\n")
    raise SystemExit(1)
kept = items[:limit]
if not kept:
    sys.stderr.write("invalid_feed\n")
    raise SystemExit(1)
body = head + "".join(marker + item for item in kept) + closing
if mode == "live":
    now = datetime.now(timezone.utc)
    cursor = 0
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

    def replacer(_match: re.Match[str]) -> str:
        global cursor
        moment = now - timedelta(days=cursor + 1)
        cursor += 1
        stamp = (
            f"{weekdays[moment.weekday()]}, {moment.day:02d} "
            f"{months[moment.month - 1]} {moment.year:04d} 00:00:00 +0000"
        )
        return f"<pubDate>{stamp}</pubDate>"

    body = re.sub(r"<pubDate>.*?</pubDate>", replacer, body, flags=re.DOTALL)
Path(dest).write_text(body, encoding="utf-8")
PY

if [ -n "${PYTHONPATH:-}" ]; then
  PYTHONPATH=$REPO/src:$PYTHONPATH
else
  PYTHONPATH=$REPO/src
fi
export PYTHONPATH
unset SECOND_BRAIN_GROK_SCENARIO || true

python3 "$trimmer" "$REPO/tests/fixtures/rss/feed.xml" "$root/tests/fixtures/rss/feed.xml" "$max_items" "$mode"
rm -f "$trimmer"

launcher=$root/.launch.py
cat > "$launcher" <<'PY'
import os
import sys
from pathlib import Path

import second_brain.config as config

root = Path(os.environ["SECOND_BRAIN_SMOKE_ROOT"])
if not root.is_absolute() or root.is_symlink() or not root.is_dir():
    sys.stderr.write("invalid_root\n")
    raise SystemExit(1)
config.PROJECT_ROOT = root
from second_brain.cli import main

raise SystemExit(main(sys.argv[1:]))
PY

export SECOND_BRAIN_SMOKE_ROOT=$root
printf 'smoke_root=%s\n' "$root"
cd "$root"

set +e
if [ "$mode" = offline ]; then
  python3 "$launcher" run --offline --config-dir config --db data/smoke.sqlite --now 2030-01-15T00:00:00Z
else
  python3 "$launcher" run --live --config-dir config --db data/smoke.sqlite
fi
status=$?
set -e
rm -f "$launcher"
exit "$status"
