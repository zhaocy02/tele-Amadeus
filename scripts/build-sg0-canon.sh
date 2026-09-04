#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash scripts/build-sg0-canon.sh --txt-dir TXT_DIR [--output-dir OUTPUT_DIR]
  bash scripts/build-sg0-canon.sh --scx-dir SCX_DIR --sc3tools SC3TOOLS_BIN [--output-dir OUTPUT_DIR]

Build separate STEINS;GATE 0 Amadeus and Kurisu raw Canon JSONL files and run
preflight.

Preferred mode:
  --txt-dir TXT_DIR
    Use text already extracted by sc3tools. This is convenient when sg-unpack
    and the official Windows sc3tools.exe are run on the game-owning PC first.

Optional extraction mode:
  --scx-dir SCX_DIR --sc3tools SC3TOOLS_BIN
    Run a supplied sc3tools executable against an already-unpacked .scx
    directory, then consume SCX_DIR/txt.

Options:
  --output-dir OUTPUT_DIR   Default: data/v2/canon-build
  -h, --help               Show this help.

The helper never unpacks script.mpk and never commits source dialogue.
It prefers the repository-local .venv/bin/python used by Server Dev validation,
falling back to python only when that virtualenv does not exist.
EOF
}

fail() {
  echo "$*" >&2
  exit 1
}

has_direct_files() {
  local directory="$1"
  local pattern="$2"
  find "${directory}" -maxdepth 1 -type f -iname "${pattern}" -print -quit | grep -q .
}

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "${repo_root}" ]] || fail "run this helper inside the Amadeus-telegram-bot Git checkout"
cd "${repo_root}"

if [[ -x "${repo_root}/.venv/bin/python" ]]; then
  python_bin="${repo_root}/.venv/bin/python"
elif command -v python >/dev/null 2>&1; then
  python_bin="$(command -v python)"
else
  fail "missing Python: create ${repo_root}/.venv or provide python on PATH"
fi

txt_dir=""
scx_dir=""
sc3tools_bin=""
output_dir="data/v2/canon-build"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --txt-dir)
      [[ $# -ge 2 ]] || fail "--txt-dir requires a path"
      txt_dir="$2"
      shift 2
      ;;
    --scx-dir)
      [[ $# -ge 2 ]] || fail "--scx-dir requires a path"
      scx_dir="$2"
      shift 2
      ;;
    --sc3tools)
      [[ $# -ge 2 ]] || fail "--sc3tools requires an executable path"
      sc3tools_bin="$2"
      shift 2
      ;;
    --output-dir)
      [[ $# -ge 2 ]] || fail "--output-dir requires a path"
      output_dir="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -n "${txt_dir}" && -n "${scx_dir}" ]]; then
  fail "choose exactly one input mode: --txt-dir or --scx-dir"
fi

if [[ -n "${txt_dir}" ]]; then
  [[ -z "${sc3tools_bin}" ]] || fail "--sc3tools is only valid with --scx-dir"
  [[ -d "${txt_dir}" ]] || fail "TXT_DIR is not a directory: ${txt_dir}"
  has_direct_files "${txt_dir}" '*.txt' || fail "no .txt files found directly under: ${txt_dir}"
elif [[ -n "${scx_dir}" ]]; then
  [[ -n "${sc3tools_bin}" ]] || fail "--scx-dir requires --sc3tools"
  [[ -d "${scx_dir}" ]] || fail "SCX_DIR is not a directory: ${scx_dir}"
  [[ -x "${sc3tools_bin}" ]] || fail "sc3tools is not executable: ${sc3tools_bin}"
  has_direct_files "${scx_dir}" '*.scx' || fail "no .scx files found directly under: ${scx_dir}"

  txt_dir="${scx_dir%/}/txt"
  if [[ -d "${txt_dir}" ]] && has_direct_files "${txt_dir}" '*.txt'; then
    fail "existing sc3tools txt output detected: ${txt_dir}; move/remove it before a clean extraction"
  fi

  echo "Checking sc3tools SG0 support..."
  sc3tools_help="$("${sc3tools_bin}" 2>&1 || true)"
  if ! grep -qi 'sg0' <<<"${sc3tools_help}"; then
    fail "sc3tools output did not advertise the sg0 game alias"
  fi

  echo "Extracting STEINS;GATE 0 text from ${scx_dir}..."
  "${sc3tools_bin}" extract-text "${scx_dir%/}/*.scx" sg0

  [[ -d "${txt_dir}" ]] || fail "sc3tools did not create expected txt directory: ${txt_dir}"
  has_direct_files "${txt_dir}" '*.txt' || fail "sc3tools produced no .txt files under: ${txt_dir}"
else
  usage >&2
  exit 2
fi

mkdir -p "${output_dir}"
amadeus_output="${output_dir%/}/sg0-amadeus.raw.jsonl"
kurisu_output="${output_dir%/}/sg0-kurisu.raw.jsonl"
preflight_output="${output_dir%/}/sg0-raw-preflight.json"

echo "Building SG0 Amadeus canon records..."
"${python_bin}" scripts/build_canon_corpus.py \
  --input "${txt_dir}" \
  --output "${amadeus_output}" \
  --format sc3 \
  --source sg0 \
  --persona amadeus \
  --speaker "Amadeus Kurisu" \
  --speaker Amadeus \
  --history-size 3

echo "Building SG0 Kurisu canon records..."
"${python_bin}" scripts/build_canon_corpus.py \
  --input "${txt_dir}" \
  --output "${kurisu_output}" \
  --format sc3 \
  --source sg0 \
  --persona kurisu \
  --speaker Kurisu \
  --history-size 3

"${python_bin}" scripts/inspect_canon_corpus.py \
  --input "${amadeus_output}" "${kurisu_output}" \
  --output "${preflight_output}"

amadeus_count="$(wc -l < "${amadeus_output}" | tr -d '[:space:]')"
kurisu_count="$(wc -l < "${kurisu_output}" | tr -d '[:space:]')"

if [[ "${amadeus_count}" == "0" || "${kurisu_count}" == "0" ]]; then
  fail "SG0 input produced a zero-count persona; inspect extraction/speaker naming before enrichment"
fi

echo "SG0 canon build complete."
echo "Python:          ${python_bin}"
echo "Amadeus records: ${amadeus_count}"
echo "Kurisu records:  ${kurisu_count}"
echo "preflight:       ${preflight_output}"
