#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/vision-provider-smoke.sh

Runs the Phase 5.5 provider/CPA input_image capability smoke from Server Dev.

Safety properties:
  - refuses to run from the production checkout;
  - uses this checkout's own .venv Python;
  - reads provider settings from the existing production .env in a subshell;
  - removes Telegram credentials before invoking the probe;
  - never starts Telegram polling or touches Amadeus runtime data.
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
  echo "ERROR: run this command inside the Amadeus-telegram-bot checkout." >&2
  exit 2
fi
REPO_ROOT="$(cd "$REPO_ROOT" && pwd -P)"
PROD_ROOT="/opt/amadeus-bot"
PROD_ENV="$PROD_ROOT/.env"
PYTHON="$REPO_ROOT/.venv/bin/python"

if [[ "$REPO_ROOT" == "$PROD_ROOT" ]]; then
  echo "ERROR: provider vision smoke must run from Server Dev, not production." >&2
  exit 2
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: repo-local virtualenv not found: $REPO_ROOT/.venv" >&2
  echo "Create/install it with: bash scripts/validate-dev.sh --install" >&2
  exit 2
fi
if [[ ! -r "$PROD_ENV" ]]; then
  echo "ERROR: provider settings source is not readable: $PROD_ENV" >&2
  exit 2
fi

(
  set -a
  # shellcheck disable=SC1090
  source "$PROD_ENV"
  set +a

  # The probe needs only provider settings. Remove Telegram authority before execution so this
  # helper cannot accidentally become a second production long-poller even if the CLI changes.
  unset AMADEUS_TELEGRAM_BOT_TOKEN
  unset AMADEUS_ALLOWED_USER_IDS
  export AMADEUS_ENABLE_LONG_POLLING=false

  exec "$PYTHON" -m amadeus_bot.vision_smoke
)
