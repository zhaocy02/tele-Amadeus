#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/character-web-tool-loop-smoke.sh [--scenario all|cross|retry|failure] [--credentials-env PATH]

Runs focused non-polling Character Web Search tool-loop acceptance smokes.
Default scenario is all.

The helper:
  - refuses the production checkout;
  - never starts Telegram polling or initializes runtime databases;
  - may reuse AMADEUS_WORKTREE_PYTHON read-only while forcing imports from this worktree;
  - reads the production .env only as a CPA compatibility baseline when available;
  - overlays a private provider-smoke env for DeepSeek credentials;
  - never prints credential values.
EOF
}

SCENARIO="all"
CREDENTIALS_ENV="${AMADEUS_PROVIDER_SMOKE_ENV:-$HOME/.config/amadeus/provider-smoke.env}"
while (($#)); do
  case "$1" in
    --scenario)
      [[ $# -ge 2 ]] || { echo "ERROR: --scenario requires a value." >&2; exit 2; }
      SCENARIO="$2"
      shift 2
      ;;
    --credentials-env)
      [[ $# -ge 2 ]] || { echo "ERROR: --credentials-env requires a path." >&2; exit 2; }
      CREDENTIALS_ENV="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$SCENARIO" in
  all|cross|retry|failure) ;;
  *)
    echo "ERROR: --scenario must be all, cross, retry, or failure." >&2
    exit 2
    ;;
esac

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$REPO_ROOT" ]]; then
  echo "ERROR: run this command inside an Amadeus-telegram-bot checkout/worktree." >&2
  exit 2
fi
REPO_ROOT="$(cd "$REPO_ROOT" && pwd -P)"
PROD_ROOT="/opt/amadeus-bot"
PROD_ENV="$PROD_ROOT/.env"
PYTHON="${AMADEUS_WORKTREE_PYTHON:-$REPO_ROOT/.venv/bin/python}"

if [[ "$REPO_ROOT" == "$PROD_ROOT" ]]; then
  echo "ERROR: Character tool-loop smoke must not run from production." >&2
  exit 2
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: selected Python is not executable: $PYTHON" >&2
  exit 2
fi
if [[ ! -r "$CREDENTIALS_ENV" ]]; then
  echo "ERROR: private provider smoke env is not readable: $CREDENTIALS_ENV" >&2
  exit 2
fi

SOURCE_PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
IMPORT_PATH="$(
  PYTHONPATH="$SOURCE_PYTHONPATH" "$PYTHON" -c \
    'import pathlib, amadeus_bot; print(pathlib.Path(amadeus_bot.__file__).resolve())'
)"
case "$IMPORT_PATH" in
  "$REPO_ROOT"/*) ;;
  *)
    echo "ERROR: interpreter imported amadeus_bot outside this worktree: $IMPORT_PATH" >&2
    exit 2
    ;;
esac

(
  if [[ -r "$PROD_ENV" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$PROD_ENV"
    set +a
  fi

  set -a
  # shellcheck disable=SC1090
  source "$CREDENTIALS_ENV"
  set +a

  unset AMADEUS_TELEGRAM_BOT_TOKEN
  unset AMADEUS_ALLOWED_USER_IDS
  export AMADEUS_ENABLE_LONG_POLLING=false
  export AMADEUS_ENABLE_AUTONOMY_PILOT=false
  export AMADEUS_ENABLE_SPONTANEITY=false
  export PYTHONPATH="$SOURCE_PYTHONPATH"

  echo "source_import=$IMPORT_PATH"
  echo "scenario=$SCENARIO"
  if [[ -n "${AMADEUS_WORKTREE_PYTHON:-}" ]]; then
    echo "python_mode=explicit_read_only_reuse"
  else
    echo "python_mode=task_local_venv"
  fi

  exec "$PYTHON" -m amadeus_bot.character_web_tool_loop_smoke --scenario "$SCENARIO"
)
