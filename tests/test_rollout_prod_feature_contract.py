from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rollout-prod-feature.sh"


def test_canon_rollout_is_allow_listed_and_fail_closed() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "canon          AMADEUS_ENABLE_CANON_EXAMPLES" in text
    assert 'key="AMADEUS_ENABLE_CANON_EXAMPLES"' in text
    assert 'if [[ "$feature:$state" == "canon:on" ]]' in text
    assert 'CANON_FILE="$ROOT/data/v2/canon.jsonl"' in text
    assert "load_canon_examples" in text
    assert "Canon activation requires at least one parsed example" in text
    assert "canon_sha256=" in text


def test_canon_rollout_has_same_path_rollback() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'rollback=bash scripts/rollout-prod-feature.sh canon off' in text
    assert 'canon:off)' in text
