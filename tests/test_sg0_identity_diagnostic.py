import json
import subprocess
import sys
from pathlib import Path


def test_identity_diagnostic_counts_labels_and_context_without_dialogue(tmp_path: Path) -> None:
    txt_dir = tmp_path / "txt"
    output = tmp_path / "report.json"
    txt_dir.mkdir()
    (txt_dir / "digital.scx.txt").write_text(
        "[name]Maho[line]Open the Amadeus app on your phone.[%p]\n"
        "[name]Kurisu[line]Hello, Okabe.[%p]\n"
        "[name]Amadeus[line]Who is this?[%p]\n",
        encoding="utf-8",
    )
    (txt_dir / "memory.scx.txt").write_text(
        "I remembered the past world line.[%p]\n"
        "[name]Kurisu[line]You look exhausted.[%p]\n",
        encoding="utf-8",
    )
    (txt_dir / "empty.scx.txt").write_text("", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/inspect_sg0_identity.py",
            "--txt-dir",
            str(txt_dir),
            "--output",
            str(output),
            "--context-records",
            "1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["files_scanned"] == 3
    assert report["kurisu_candidates"] == 2
    assert report["amadeus_labeled_records"] == 1
    assert report["kurisu_near_amadeus_literal"] == 1
    assert report["kurisu_near_digital_cues"] == 1
    assert report["kurisu_near_flashback_cues"] == 1

    rows = {row["path"]: row for row in report["files"]}
    assert rows["digital.scx.txt"]["kurisu"] == 1
    assert rows["digital.scx.txt"]["amadeus"] == 1
    assert rows["memory.scx.txt"]["kurisu_near_flashback_cues"] == 1

    serialized = json.dumps(report, ensure_ascii=False)
    assert "Hello, Okabe" not in serialized
    assert "You look exhausted" not in serialized
