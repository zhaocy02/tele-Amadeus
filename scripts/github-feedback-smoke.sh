#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/github-feedback-smoke.sh [--credentials-env PATH]

Runs a non-writing GitHub App capability smoke against the configured public feedback repository.

Safety properties:
  - refuses to run from the production checkout;
  - requires this task checkout/worktree's own .venv because this feature changes dependencies;
  - reads GitHub App credentials only from a private env file/environment;
  - never starts Telegram polling or touches Amadeus runtime databases;
  - only mints a repository-scoped installation token, reads public HEAD, and searches Issues.

Private credential file default:
  $HOME/.config/amadeus/github-feedback-smoke.env

Required variables:
  AMADEUS_GITHUB_APP_CLIENT_ID
  AMADEUS_GITHUB_APP_INSTALLATION_ID
  AMADEUS_GITHUB_APP_PRIVATE_KEY_PATH

Optional variables:
  AMADEUS_GITHUB_FEEDBACK_REPOSITORY  default: zhaocy02/tele-Amadeus
  AMADEUS_GITHUB_FEEDBACK_BRANCH      default: main
EOF
}

CREDENTIALS_ENV="${AMADEUS_GITHUB_FEEDBACK_SMOKE_ENV:-$HOME/.config/amadeus/github-feedback-smoke.env}"
while (($#)); do
  case "$1" in
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

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$REPO_ROOT" ]]; then
  echo "ERROR: run this command inside an Amadeus-telegram-bot checkout/worktree." >&2
  exit 2
fi
REPO_ROOT="$(cd "$REPO_ROOT" && pwd -P)"
PROD_ROOT="/opt/amadeus-bot"
PYTHON="$REPO_ROOT/.venv/bin/python"

if [[ "$REPO_ROOT" == "$PROD_ROOT" ]]; then
  echo "ERROR: GitHub feedback smoke must run from a task worktree, not production." >&2
  exit 2
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: task-local virtualenv not found." >&2
  echo "Run: bash scripts/validate-dev.sh --install" >&2
  exit 2
fi
if [[ ! -r "$CREDENTIALS_ENV" ]]; then
  echo "ERROR: private GitHub feedback credentials env is not readable." >&2
  exit 2
fi

ENV_MODE="$(stat -c '%a' "$CREDENTIALS_ENV" 2>/dev/null || true)"
if [[ -z "$ENV_MODE" ]] || (( (8#$ENV_MODE & 077) != 0 )); then
  echo "ERROR: credentials env must be readable only by its owner (chmod 600)." >&2
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
    echo "ERROR: interpreter imported amadeus_bot outside this task worktree." >&2
    exit 2
    ;;
esac

(
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
  echo "credentials_mode=private_file"
  echo "python_mode=task_local_venv"

  exec "$PYTHON" -m amadeus_bot.github_feedback_smoke
)
