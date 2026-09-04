#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/web-search-provider-smoke.sh

Runs one CPA-native hosted web_search capability smoke from a non-production checkout/worktree.

Safety properties:
  - refuses to run from the production checkout;
  - prefers this checkout/worktree's own .venv Python;
  - when AMADEUS_WORKTREE_PYTHON is explicitly set, may reuse that compatible interpreter
    read-only while forcing source imports from this exact checkout/worktree;
  - verifies the imported amadeus_bot path before the capability request;
  - reads existing provider settings from the production .env in a subshell;
  - removes Telegram credentials before invoking the probe;
  - never starts Telegram polling or touches Amadeus runtime data;
  - needs no Brave/Tavily key and no Amadeus-side outbound search connection.

Optional environment:
  AMADEUS_WORKTREE_PYTHON=/absolute/path/to/python
    Use an already-provisioned compatible Python/venv read-only when creating a task-local
    virtualenv is blocked by the server environment. No package install or modification is made.
EOF
}

if (($#)); then
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: this helper takes no arguments." >&2
      usage >&2
      exit 2
      ;;
  esac
fi

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
  echo "ERROR: web-search smoke must run from Server Dev/worktree, not production." >&2
  exit 2
fi
if [[ ! -x "$PYTHON" ]]; then
  if [[ -n "${AMADEUS_WORKTREE_PYTHON:-}" ]]; then
    echo "ERROR: AMADEUS_WORKTREE_PYTHON is not executable: $PYTHON" >&2
  else
    echo "ERROR: repo-local virtualenv not found: $REPO_ROOT/.venv" >&2
    echo "Create it normally, or explicitly reuse a compatible read-only interpreter with:" >&2
    echo "  AMADEUS_WORKTREE_PYTHON=/absolute/path/to/python bash scripts/web-search-provider-smoke.sh" >&2
  fi
  exit 2
fi
if [[ ! -r "$PROD_ENV" ]]; then
  echo "ERROR: provider settings source is not readable: $PROD_ENV" >&2
  exit 2
fi

SOURCE_PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
IMPORT_PATH="$(
  PYTHONPATH="$SOURCE_PYTHONPATH" "$PYTHON" -c \
    'import pathlib, amadeus_bot; print(pathlib.Path(amadeus_bot.__file__).resolve())'
)"
case "$IMPORT_PATH" in
  "$REPO_ROOT"/*)
    ;;
  *)
    echo "ERROR: interpreter imported amadeus_bot outside this task worktree: $IMPORT_PATH" >&2
    exit 2
    ;;
esac

(
  set -a
  # shellcheck disable=SC1090
  source "$PROD_ENV"
  set +a

  unset AMADEUS_TELEGRAM_BOT_TOKEN
  unset AMADEUS_ALLOWED_USER_IDS
  export AMADEUS_ENABLE_LONG_POLLING=false
  export AMADEUS_ENABLE_AUTONOMY_PILOT=false
  export AMADEUS_ENABLE_SPONTANEITY=false
  export PYTHONPATH="$SOURCE_PYTHONPATH"

  echo "source_import=$IMPORT_PATH"
  if [[ -n "${AMADEUS_WORKTREE_PYTHON:-}" ]]; then
    echo "python_mode=explicit_read_only_reuse"
  else
    echo "python_mode=task_local_venv"
  fi

  exec "$PYTHON" -m amadeus_bot.web_search_smoke
)
