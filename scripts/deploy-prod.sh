#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_PROD_ROOT="/opt/amadeus-bot"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="$ROOT/.venv/bin/python"
SUMMARY_FILE="/tmp/amadeus-prod-deploy-last.txt"
LOG_DIR=""
KEEP_LOGS=0
SKIP_RESTART=0
DEPLOY_SUCCEEDED=0

usage() {
  cat <<'EOF'
Usage: bash scripts/deploy-prod.sh [--keep-logs] [--skip-restart]

Guarded deployment of merged main into the Amadeus v2 production checkout.

Options:
  --keep-logs     retain full logs even after a successful deploy
  --skip-restart  update/install/check only; do not restart the v2 service
  -h, --help      show this help

For the editable pip install, an explicit readable PIP_CERT is respected. Otherwise the helper
uses a readable host system CA bundle when one is available. TLS verification is never disabled.
The initial git fetch is retried at most three times with bounded backoff; later deployment steps
remain fail-fast and are never retried implicitly.
EOF
}

while (($#)); do
  case "$1" in
    --keep-logs)
      KEEP_LOGS=1
      ;;
    --skip-restart)
      SKIP_RESTART=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

SUMMARY_LINES=()
add_summary() {
  SUMMARY_LINES+=("$1")
  printf '%s\n' "$1"
}

write_summary() {
  printf '%s\n' "${SUMMARY_LINES[@]}" > "$SUMMARY_FILE"
}

fail() {
  local label="$1"
  local log_file="${2:-}"
  add_summary "[FAIL] $label"
  if [[ -n "$log_file" && -f "$log_file" ]]; then
    echo "---- last 30 log lines: $label ----" >&2
    tail -n 30 "$log_file" >&2 || true
    echo "---- end log tail ----" >&2
  fi
  add_summary "RESULT=FAIL"
  [[ -n "$LOG_DIR" ]] && add_summary "logs=$LOG_DIR"
  write_summary
  exit 1
}

run_logged() {
  local label="$1"
  shift
  local log_file="$LOG_DIR/${label//[^A-Za-z0-9_.-]/_}.log"
  if "$@" >"$log_file" 2>&1; then
    add_summary "[PASS] $label"
  else
    fail "$label" "$log_file"
  fi
}

run_logged_retry() {
  local label="$1"
  local max_attempts="$2"
  local delay_seconds="$3"
  shift 3
  local attempt=1
  local log_file="$LOG_DIR/${label//[^A-Za-z0-9_.-]/_}.log"

  while true; do
    if "$@" >"$log_file" 2>&1; then
      if ((attempt == 1)); then
        add_summary "[PASS] $label"
      else
        add_summary "[PASS] $label attempt=$attempt/$max_attempts"
      fi
      return 0
    fi

    if ((attempt >= max_attempts)); then
      fail "$label after $max_attempts attempts" "$log_file"
    fi

    add_summary "[WARN] $label attempt=$attempt/$max_attempts failed; retrying in ${delay_seconds}s"
    sleep "$delay_seconds"
    attempt=$((attempt + 1))
    delay_seconds=$((delay_seconds * 2))
  done
}

service_state() {
  systemctl --user is-active "$1" 2>/dev/null || true
}

require_service_state() {
  local phase="$1"
  local v1 v2 cpa
  v1="$(service_state telegram-codex-cpa-bot.service)"
  v2="$(service_state amadeus-telegram-bot.service)"
  cpa="$(service_state cli-proxy-api.service)"
  add_summary "$phase services: v1=$v1 v2=$v2 cpa=$cpa"
  [[ "$v1" != "active" ]] || fail "$phase: legacy v1 must be inactive"
  [[ "$v2" == "active" ]] || fail "$phase: v2 must be active"
  [[ "$cpa" == "active" ]] || fail "$phase: CPA must be active"
}

resolve_pip_cert() {
  local candidate

  if [[ -n "${PIP_CERT:-}" ]]; then
    [[ -r "$PIP_CERT" ]] || return 2
    printf '%s\n' "$PIP_CERT"
    return 0
  fi

  for candidate in \
    /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem \
    /etc/pki/tls/certs/ca-bundle.crt \
    /etc/ssl/certs/ca-certificates.crt
  do
    if [[ -r "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

cleanup() {
  if [[ -n "$LOG_DIR" && "$KEEP_LOGS" -eq 0 && "$DEPLOY_SUCCEEDED" -eq 1 ]]; then
    rm -rf "$LOG_DIR"
  fi
}

LOG_DIR="$(mktemp -d /tmp/amadeus-prod-deploy.XXXXXX)"
trap cleanup EXIT

add_summary "Amadeus production deploy"
add_summary "repo=$ROOT"

[[ "$ROOT" == "$EXPECTED_PROD_ROOT" ]] || fail "refusing non-production checkout (expected $EXPECTED_PROD_ROOT)"
[[ -x "$PYTHON" ]] || fail "missing production venv python: $PYTHON"
[[ -f "$ROOT/.env" ]] || fail "missing production .env"

cd "$ROOT"

branch="$(git branch --show-current)"
head_before="$(git rev-parse HEAD)"
add_summary "branch=$branch"
add_summary "head_before=$head_before"
[[ "$branch" == "main" ]] || fail "production checkout must be on main"

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  fail "production working tree is not clean"
fi
add_summary "[PASS] clean-worktree"

run_logged_retry "git-fetch" 3 2 git fetch origin
remote_main="$(git rev-parse origin/main)"
add_summary "origin_main=$remote_main"

if ! git merge-base --is-ancestor HEAD origin/main; then
  fail "local main cannot fast-forward cleanly to origin/main"
fi
add_summary "[PASS] fast-forward-eligible"

run_logged "git-fast-forward" git merge --ff-only origin/main
head_after="$(git rev-parse HEAD)"
[[ "$head_after" == "$remote_main" ]] || fail "deployed HEAD does not match fetched origin/main"
add_summary "head_after=$head_after"
add_summary "[PASS] exact-main"

pip_cert=""
pip_cert_status=0
pip_cert="$(resolve_pip_cert)" || pip_cert_status=$?
case "$pip_cert_status" in
  0)
    if [[ -n "${PIP_CERT:-}" ]]; then
      add_summary "pip_cert=environment:$pip_cert"
    else
      add_summary "pip_cert=system:$pip_cert"
    fi
    run_logged "install" env PIP_CERT="$pip_cert" "$PYTHON" -m pip install -q -e .
    ;;
  1)
    add_summary "pip_cert=pip-default"
    run_logged "install" "$PYTHON" -m pip install -q -e .
    ;;
  2)
    fail "PIP_CERT is not readable: ${PIP_CERT:-<empty>}"
    ;;
  *)
    fail "unexpected PIP_CERT resolution status: $pip_cert_status"
    ;;
esac

config_log="$LOG_DIR/check-config.log"
if (
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
  exec "$PYTHON" -m amadeus_bot check-config
) >"$config_log" 2>&1; then
  add_summary "[PASS] config"
else
  fail "config" "$config_log"
fi

require_service_state "pre-restart"

if [[ "$SKIP_RESTART" -eq 1 ]]; then
  add_summary "[SKIP] restart"
else
  restart_log="$LOG_DIR/restart.log"
  if systemctl --user restart amadeus-telegram-bot.service >"$restart_log" 2>&1; then
    add_summary "[PASS] restart"
  else
    fail "restart" "$restart_log"
  fi
  sleep 3
  require_service_state "post-restart"
fi

DEPLOY_SUCCEEDED=1
add_summary "RESULT=PASS"
add_summary "telegram_smoke=required"
add_summary "summary=$SUMMARY_FILE"
if [[ "$KEEP_LOGS" -eq 1 ]]; then
  add_summary "logs=$LOG_DIR"
fi
write_summary
