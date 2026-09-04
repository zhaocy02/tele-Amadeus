#!/usr/bin/env bash
set -u

usage() {
  cat <<'EOF'
Usage: bash scripts/validate-dev.sh [--install] [--venv PATH] [--keep-logs]

Runs the repository validation suite with concise terminal output.

Options:
  --install    Quietly install/update the repo and dev dependencies into this checkout's .venv first.
  --venv PATH  Reuse an existing compatible virtualenv read-only while validating source from this checkout.
  --keep-logs  Keep full logs even when every check passes.
  -h, --help   Show this help.

Default mode uses <repo>/.venv/bin/python.

External-venv mode is intended for isolated worktrees whose dependency contract did not change.
It refuses --install, requires origin/main for the dependency-diff guard, rejects a conflicting
worktree-local .venv, forces PYTHONPATH to this checkout, puts the selected venv first on PATH,
and verifies the import origin. Package build runs with --no-isolation only when the selected
venv already provides the configured build backend; otherwise build is explicitly skipped and
must remain covered by CI. External-venv validation never installs into the shared venv.
EOF
}

INSTALL=0
KEEP_LOGS=0
EXTERNAL_VENV=""
while (($#)); do
  case "$1" in
    --install) INSTALL=1 ;;
    --venv)
      if (($# < 2)); then
        echo "ERROR: --venv requires a path" >&2
        usage >&2
        exit 2
      fi
      EXTERNAL_VENV="$2"
      shift
      ;;
    --keep-logs) KEEP_LOGS=1 ;;
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
  shift
done

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$REPO_ROOT" ]]; then
  echo "ERROR: run this command inside the Amadeus-telegram-bot Git checkout." >&2
  exit 2
fi
cd "$REPO_ROOT"

LOCAL_VENV="$REPO_ROOT/.venv"
EXTERNAL_MODE=0
if [[ -n "$EXTERNAL_VENV" ]]; then
  EXTERNAL_MODE=1
  if ((INSTALL)); then
    echo "ERROR: --install cannot be combined with --venv; external virtualenvs are read-only." >&2
    exit 2
  fi

  VENV="$(cd "$EXTERNAL_VENV" 2>/dev/null && pwd -P || true)"
  if [[ -z "$VENV" || ! -x "$VENV/bin/python" ]]; then
    echo "ERROR: compatible external virtualenv not found: $EXTERNAL_VENV" >&2
    exit 2
  fi

  if [[ -e "$LOCAL_VENV" || -L "$LOCAL_VENV" ]]; then
    LOCAL_REAL="$(cd "$LOCAL_VENV" 2>/dev/null && pwd -P || true)"
    if [[ "$LOCAL_REAL" != "$VENV" ]]; then
      echo "ERROR: worktree-local .venv exists and would shadow the selected external virtualenv." >&2
      echo "Move/remove only this task-owned .venv before using --venv: $LOCAL_VENV" >&2
      exit 2
    fi
  fi

  if ! git rev-parse --verify refs/remotes/origin/main >/dev/null 2>&1; then
    echo "ERROR: origin/main is unavailable; fetch it before external-venv validation." >&2
    exit 2
  fi
  BASE_SHA="$(git merge-base HEAD refs/remotes/origin/main 2>/dev/null || true)"
  if [[ -z "$BASE_SHA" ]]; then
    echo "ERROR: could not determine merge-base with origin/main." >&2
    exit 2
  fi
  if ! git diff --quiet "$BASE_SHA" HEAD -- pyproject.toml; then
    echo "ERROR: pyproject.toml changed in this work item; shared-venv validation is insufficient." >&2
    echo "Use a task-local .venv and perform a real dependency install instead." >&2
    exit 2
  fi

  export PATH="$VENV/bin:$PATH"
  export PYTHONPATH="$REPO_ROOT"
  export VIRTUAL_ENV="$VENV"
else
  VENV="$LOCAL_VENV"
fi

PYTHON="$VENV/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: repo-local virtualenv not found: $VENV" >&2
  echo "Create it once with: python3 -m venv .venv" >&2
  exit 2
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_DIR="${TMPDIR:-/tmp}/amadeus-dev-validation-${STAMP}-$$"
mkdir -p "$LOG_DIR"
SUMMARY="$LOG_DIR/summary.txt"

BRANCH="$(git branch --show-current 2>/dev/null || true)"
HEAD_SHA="$(git rev-parse HEAD)"
PYTHON_VERSION="$($PYTHON -c 'import platform; print(platform.python_version())')"
ACTIVE_IMPORT=""
FAILURES=0

record() {
  printf '%s\n' "$1" | tee -a "$SUMMARY"
}

fail_with_log() {
  local name="$1"
  local logfile="$2"
  FAILURES=$((FAILURES + 1))
  record "[FAIL] $name"
  echo "----- $name: last 30 log lines -----" >&2
  tail -n 30 "$logfile" >&2 || true
  echo "----- full log: $logfile -----" >&2
}

run_check() {
  local name="$1"
  shift
  local logfile="$LOG_DIR/${name}.log"
  if "$@" >"$logfile" 2>&1; then
    record "[PASS] $name"
    return 0
  fi
  fail_with_log "$name" "$logfile"
  return 1
}

record "Amadeus Server Dev validation"
record "repo=$REPO_ROOT"
record "branch=${BRANCH:-DETACHED}"
record "head=$HEAD_SHA"
record "python=$PYTHON_VERSION"
record "venv=$VENV"
if ((EXTERNAL_MODE)); then
  record "venv_mode=external-readonly"
else
  record "venv_mode=repo-local"
fi
if [[ -n "${VIRTUAL_ENV:-}" && "$(cd "$VIRTUAL_ENV" 2>/dev/null && pwd -P || true)" != "$(cd "$VENV" && pwd -P)" ]]; then
  record "note=ignoring active foreign VIRTUAL_ENV=$VIRTUAL_ENV"
fi

if ((INSTALL)); then
  if ! run_check install "$PYTHON" -m pip install -q -e '.[dev]'; then
    record "RESULT=FAIL"
    record "logs=$LOG_DIR"
    exit 1
  fi
fi

IMPORT_LOG="$LOG_DIR/import-path.log"
if ACTIVE_IMPORT="$($PYTHON -c 'import pathlib, amadeus_bot; print(pathlib.Path(amadeus_bot.__file__).resolve())' 2>"$IMPORT_LOG")"; then
  case "$ACTIVE_IMPORT" in
    "$REPO_ROOT"/*)
      record "[PASS] import-path=$ACTIVE_IMPORT"
      ;;
    *)
      echo "$ACTIVE_IMPORT" >"$IMPORT_LOG"
      fail_with_log import-path "$IMPORT_LOG"
      ;;
  esac
else
  fail_with_log import-path "$IMPORT_LOG"
fi

run_check shell-helpers bash -c '
  for helper in scripts/*.sh; do
    bash -n "$helper" || exit 1
  done
  bash scripts/deploy-prod.sh --help >/dev/null
  bash scripts/rollout-prod-feature.sh --help >/dev/null
  bash scripts/vision-provider-smoke.sh --help >/dev/null
  bash scripts/web-search-provider-smoke.sh --help >/dev/null
' || true
run_check observation-cli "$PYTHON" -m amadeus_bot.telemetry_cli --help || true
run_check vision-smoke-cli "$PYTHON" -m amadeus_bot.vision_smoke --help || true
run_check web-search-smoke-cli "$PYTHON" -m amadeus_bot.web_search_smoke --help || true
run_check ruff "$PYTHON" -m ruff check . || true
run_check mypy "$PYTHON" -m mypy amadeus_bot || true
run_check pytest "$PYTHON" -m pytest || true
if ((EXTERNAL_MODE)); then
  if "$PYTHON" -c 'import setuptools.build_meta' >/dev/null 2>&1; then
    run_check build "$PYTHON" -m build --no-isolation || true
  else
    record "[SKIP] build (external venv lacks setuptools.build_meta; CI build remains required)"
  fi
else
  run_check build "$PYTHON" -m build || true
fi

if ((FAILURES)); then
  record "RESULT=FAIL failures=$FAILURES"
  record "logs=$LOG_DIR"
  exit 1
fi

record "RESULT=PASS"
if ((KEEP_LOGS)); then
  record "logs=$LOG_DIR"
else
  cp "$SUMMARY" "/tmp/amadeus-dev-validation-last.txt"
  rm -rf "$LOG_DIR"
  echo "summary=/tmp/amadeus-dev-validation-last.txt"
fi
