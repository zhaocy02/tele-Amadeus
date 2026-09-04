#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_PROD_ROOT="/opt/amadeus-bot"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
ENV_FILE="$ROOT/.env"
DEPLOY_HELPER="$ROOT/scripts/deploy-prod.sh"
PYTHON="$ROOT/.venv/bin/python"
CANON_FILE="$ROOT/data/v2/canon.jsonl"

usage() {
  cat <<'EOF'
Usage: bash scripts/rollout-prod-feature.sh <feature> <on|off>

Safely change one allow-listed production process gate, then run the existing guarded deploy helper.
The script never prints .env contents or unrelated environment variables.

Features:
  autonomy       AMADEUS_ENABLE_AUTONOMY_PILOT
  spontaneity    AMADEUS_ENABLE_SPONTANEITY
  web-search     AMADEUS_ENABLE_WEB_SEARCH
  canon          AMADEUS_ENABLE_CANON_EXAMPLES

Examples:
  bash scripts/rollout-prod-feature.sh spontaneity on
  bash scripts/rollout-prod-feature.sh web-search on
  bash scripts/rollout-prod-feature.sh canon on

Canon activation fails closed unless data/v2/canon.jsonl exists, parses with the runtime Canon
loader, and contains at least one example. Its example count and SHA-256 are printed before the
gate is changed so the operator can verify the exact evaluated corpus provenance.

This helper changes only the process-level gate. Some features also have per-chat Telegram gates.
Run it only from an independent production SSH session.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ "$#" -ne 2 ]]; then
  usage >&2
  exit 2
fi

feature="$1"
state="$2"

case "$feature" in
  autonomy)
    key="AMADEUS_ENABLE_AUTONOMY_PILOT"
    ;;
  spontaneity)
    key="AMADEUS_ENABLE_SPONTANEITY"
    ;;
  web-search)
    key="AMADEUS_ENABLE_WEB_SEARCH"
    ;;
  canon)
    key="AMADEUS_ENABLE_CANON_EXAMPLES"
    ;;
  *)
    echo "ERROR: unsupported feature: $feature" >&2
    usage >&2
    exit 2
    ;;
esac

case "$state" in
  on) value="true" ;;
  off) value="false" ;;
  *)
    echo "ERROR: state must be on or off" >&2
    usage >&2
    exit 2
    ;;
esac

[[ "$ROOT" == "$EXPECTED_PROD_ROOT" ]] || {
  echo "ERROR: refusing non-production checkout (expected $EXPECTED_PROD_ROOT)" >&2
  exit 1
}
[[ -f "$ENV_FILE" ]] || {
  echo "ERROR: missing production .env" >&2
  exit 1
}
[[ -x "$PYTHON" ]] || {
  echo "ERROR: missing production venv python: $PYTHON" >&2
  exit 1
}
[[ -f "$DEPLOY_HELPER" ]] || {
  echo "ERROR: missing deploy helper: $DEPLOY_HELPER" >&2
  exit 1
}

cd "$ROOT"
[[ "$(git branch --show-current)" == "main" ]] || {
  echo "ERROR: production checkout must be on main" >&2
  exit 1
}
if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "ERROR: production working tree is not clean" >&2
  exit 1
fi

if [[ "$feature:$state" == "canon:on" ]]; then
  [[ -f "$CANON_FILE" ]] || {
    echo "ERROR: Canon activation requires $CANON_FILE" >&2
    exit 1
  }
  PYTHONPATH="$ROOT" "$PYTHON" - "$CANON_FILE" <<'PY'
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from amadeus_bot.character.canon import load_canon_examples

path = Path(sys.argv[1]).resolve()
try:
    examples = load_canon_examples(path)
except Exception as exc:
    raise SystemExit(f"ERROR: invalid Canon JSONL: {exc}") from exc
if not examples:
    raise SystemExit("ERROR: Canon activation requires at least one parsed example")

print(f"canon_file={path}")
print(f"canon_examples={len(examples)}")
print(f"canon_sha256={hashlib.sha256(path.read_bytes()).hexdigest()}")
PY
fi

"$PYTHON" - "$ENV_FILE" "$key" "$value" <<'PY'
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
text = path.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)
pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
matches = [index for index, line in enumerate(lines) if pattern.match(line)]
if len(matches) > 1:
    raise SystemExit(f"ERROR: duplicate {key} entries in .env")

previous = "unset"
replacement = f"{key}={value}\n"
if matches:
    index = matches[0]
    previous_value = lines[index].split("=", 1)[1].strip().split("#", 1)[0].strip().lower()
    previous = previous_value or "empty"
    lines[index] = replacement
else:
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += "\n"
    lines.append(replacement)

new_text = "".join(lines)
tmp = path.with_name(path.name + ".rollout-tmp")
tmp.write_text(new_text, encoding="utf-8")
os.chmod(tmp, path.stat().st_mode)
os.replace(tmp, path)

print(f"feature_gate={key}")
print(f"gate_previous={previous}")
print(f"gate_now={value}")
PY

echo "deploy=starting"
bash "$DEPLOY_HELPER"

echo "rollout=PASS"
case "$feature:$state" in
  spontaneity:on)
    echo "next_telegram=/autonomy spontaneity on"
    echo "observe=.venv/bin/amadeus-observe spontaneity --since 24h --recent 30"
    ;;
  spontaneity:off|autonomy:on|autonomy:off)
    echo "next_telegram=/autonomy"
    ;;
  web-search:on)
    echo "next_telegram=ask a current factual question, e.g. OpenAI 最近有什么新消息？"
    ;;
  web-search:off)
    echo "next_check=web search process gate is off"
    ;;
  canon:on)
    echo "next_check=run the focused Canon production smoke, then observe normal chats"
    echo "rollback=bash scripts/rollout-prod-feature.sh canon off"
    ;;
  canon:off)
    echo "next_check=Canon process gate is off"
    ;;
esac
