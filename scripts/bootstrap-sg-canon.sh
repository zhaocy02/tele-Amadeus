#!/usr/bin/env bash
set -euo pipefail

SOURCE_COMMIT="9d4726bd37dce9919af37904e442e49205f329b8"
SOURCE_BLOB_SHA="df73d9a52cdcd77c54320773fa3ff5225b938c09"
SOURCE_URL="https://raw.githubusercontent.com/FrancescoCaracciolo/Amadeus/${SOURCE_COMMIT}/Dialogues/SG_Dialogues_EN.md"

usage() {
  cat <<'EOF'
Usage: bash scripts/bootstrap-sg-canon.sh [OUTPUT_DIR]

Download the pinned FrancescoCaracciolo/Amadeus STEINS;GATE dialogue export,
verify its Git blob SHA, and build Kurisu raw Canon JSONL with the repository's
current ingestion code.

Default OUTPUT_DIR: data/v2/canon-build

The downloaded source is stored under the sibling directory:
  data/v2/canon-source/SG_Dialogues_EN.md

All generated/source files live under data/ and must not be committed.
The helper prefers the repository-local .venv/bin/python used by Server Dev
validation, falling back to python only when that virtualenv does not exist.
EOF
}

fail() {
  echo "$*" >&2
  exit 1
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

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

for command_name in curl git; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    fail "missing required command: ${command_name}"
  fi
done

output_dir="${1:-data/v2/canon-build}"
source_dir="$(dirname "${output_dir}")/canon-source"
source_file="${source_dir}/SG_Dialogues_EN.md"
output_file="${output_dir}/sg-kurisu.raw.jsonl"

mkdir -p "${source_dir}" "${output_dir}"

echo "Downloading pinned STEINS;GATE dialogue source..."
curl --location --fail --show-error --silent \
  "${SOURCE_URL}" \
  --output "${source_file}"

actual_blob_sha="$(git hash-object "${source_file}")"
if [[ "${actual_blob_sha}" != "${SOURCE_BLOB_SHA}" ]]; then
  echo "source verification failed" >&2
  echo "expected Git blob SHA: ${SOURCE_BLOB_SHA}" >&2
  echo "actual Git blob SHA:   ${actual_blob_sha}" >&2
  rm -f "${source_file}"
  exit 1
fi

echo "Verified source blob ${actual_blob_sha}."

"${python_bin}" scripts/build_canon_corpus.py \
  --input "${source_file}" \
  --output "${output_file}" \
  --format speaker \
  --source sg \
  --persona kurisu \
  --speaker Kurisu \
  --history-size 3

record_count="$(wc -l < "${output_file}" | tr -d '[:space:]')"

echo "SG Kurisu bootstrap complete."
echo "Python:        ${python_bin}"
echo "source commit: ${SOURCE_COMMIT}"
echo "source blob:   ${SOURCE_BLOB_SHA}"
echo "records:       ${record_count}"
echo "output:        ${output_file}"
