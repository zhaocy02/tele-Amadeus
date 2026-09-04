#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/provider-capability-smoke.sh --provider cpa|deepseek [--capability text|tool|vision|web|all] [--credentials-env PATH]

Runs a non-Telegram provider capability smoke from Server Dev/worktree.
Default capability is all.

Safety properties:
  - refuses to run from the production checkout;
  - prefers this checkout/worktree's own .venv Python;
  - may reuse AMADEUS_WORKTREE_PYTHON read-only while forcing imports from this exact checkout;
  - may read existing CPA settings from production .env as a compatibility baseline;
  - may overlay a separate private credentials env for DeepSeek validation before production activation;
  - removes Telegram credentials and disables polling/autonomy/spontaneity before the probe;
  - never starts Telegram polling or touches Amadeus runtime databases.

Capabilities:
  text
    Basic text generation.
  tool
    Native function_call -> function_call_output -> continued response round trip.
  vision
    Existing vision capability smoke.
  web
    Hosted Web Search capability with source evidence.
  all
    Run text + tool + vision + web.

Private credential override:
  --credentials-env PATH
    Source a private env file after the production compatibility baseline. This is the preferred
    way to validate DeepSeek before adding any DeepSeek credential to production configuration.
    The file must remain outside Git and should be readable only by the current user.

  AMADEUS_PROVIDER_SMOKE_ENV=/absolute/private/path
    Equivalent environment-variable form when --credentials-env is omitted.

Examples:
  AMADEUS_WORKTREE_PYTHON=/opt/amadeus-bot-dev/.venv/bin/python \
    bash scripts/provider-capability-smoke.sh --provider cpa --capability tool
  AMADEUS_WORKTREE_PYTHON=/opt/amadeus-bot-dev/.venv/bin/python \
    bash scripts/provider-capability-smoke.sh --provider deepseek --capability tool
  bash scripts/provider-capability-smoke.sh --provider deepseek --capability web \
    --credentials-env /private/path/deepseek-smoke.env
EOF
}

PROVIDER=""
CAPABILITY="all"
CREDENTIALS_ENV="${AMADEUS_PROVIDER_SMOKE_ENV:-}"
while (($#)); do
  case "$1" in
    --provider)
      [[ $# -ge 2 ]] || { echo "ERROR: --provider requires a value." >&2; exit 2; }
      PROVIDER="$2"
      shift 2
      ;;
    --capability)
      [[ $# -ge 2 ]] || { echo "ERROR: --capability requires a value." >&2; exit 2; }
      CAPABILITY="$2"
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

case "$PROVIDER" in
  cpa|deepseek) ;;
  *)
    echo "ERROR: --provider must be cpa or deepseek." >&2
    exit 2
    ;;
esac
case "$CAPABILITY" in
  text|tool|vision|web|all) ;;
  *)
    echo "ERROR: --capability must be text, tool, vision, web, or all." >&2
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
  echo "ERROR: provider capability smoke must run from Server Dev/worktree, not production." >&2
  exit 2
fi
if [[ ! -x "$PYTHON" ]]; then
  if [[ -n "${AMADEUS_WORKTREE_PYTHON:-}" ]]; then
    echo "ERROR: AMADEUS_WORKTREE_PYTHON is not executable: $PYTHON" >&2
  else
    echo "ERROR: repo-local virtualenv not found: $REPO_ROOT/.venv" >&2
    echo "Create/install it with: bash scripts/validate-dev.sh --install" >&2
  fi
  exit 2
fi
if [[ -n "$CREDENTIALS_ENV" && ! -r "$CREDENTIALS_ENV" ]]; then
  echo "ERROR: private credentials env is not readable." >&2
  exit 2
fi
if [[ "$PROVIDER" == "cpa" && ! -r "$PROD_ENV" && -z "$CREDENTIALS_ENV" ]]; then
  echo "ERROR: CPA smoke requires production provider settings or --credentials-env." >&2
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
    echo "ERROR: interpreter imported amadeus_bot outside this task worktree: $IMPORT_PATH" >&2
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
  if [[ -n "$CREDENTIALS_ENV" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$CREDENTIALS_ENV"
    set +a
  fi

  unset AMADEUS_TELEGRAM_BOT_TOKEN
  unset AMADEUS_ALLOWED_USER_IDS
  export AMADEUS_ENABLE_LONG_POLLING=false
  export AMADEUS_ENABLE_AUTONOMY_PILOT=false
  export AMADEUS_ENABLE_SPONTANEITY=false
  export PYTHONPATH="$SOURCE_PYTHONPATH"

  echo "source_import=$IMPORT_PATH"
  echo "provider=$PROVIDER capability=$CAPABILITY"
  if [[ -n "$CREDENTIALS_ENV" ]]; then
    echo "credentials_mode=private_override"
  elif [[ -r "$PROD_ENV" ]]; then
    echo "credentials_mode=production_compatibility_baseline"
  else
    echo "credentials_mode=environment_only"
  fi
  if [[ -n "${AMADEUS_WORKTREE_PYTHON:-}" ]]; then
    echo "python_mode=explicit_read_only_reuse"
  else
    echo "python_mode=task_local_venv"
  fi

  exec "$PYTHON" -m amadeus_bot.provider_capability_smoke \
    --provider "$PROVIDER" \
    --capability "$CAPABILITY"
)
