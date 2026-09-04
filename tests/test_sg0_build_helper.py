import subprocess
from pathlib import Path

from amadeus_bot.character.canon import load_canon_examples


def test_sg0_build_helper_accepts_preextracted_txt(tmp_path: Path) -> None:
    txt_dir = tmp_path / "txt"
    output_dir = tmp_path / "out"
    txt_dir.mkdir()
    (txt_dir / "scene001.txt").write_text(
        "[name]Maho[line]Are you certain?[%p]\n"
        "[name]Amadeus Kurisu[line]Then check the evidence again.[%p]\n"
        "[name]Rintaro[line]Who are you?[%p]\n"
        "[name]Amadeus[line]Who is this?[%p]\n"
        "[name]Rintaro[line]Kurisu?[%p]\n"
        "[name]Kurisu[line]Don't jump to conclusions.[%p]\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "bash",
            "scripts/build-sg0-canon.sh",
            "--txt-dir",
            str(txt_dir),
            "--output-dir",
            str(output_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    amadeus = load_canon_examples(output_dir / "sg0-amadeus.raw.jsonl")
    kurisu = load_canon_examples(output_dir / "sg0-kurisu.raw.jsonl")
    assert len(amadeus) == 2
    assert [example.response for example in amadeus] == [
        "Then check the evidence again.",
        "Who is this?",
    ]
    assert all(example.persona == "amadeus" for example in amadeus)
    assert len(kurisu) == 1
    assert kurisu[0].persona == "kurisu"
    assert (output_dir / "sg0-raw-preflight.json").exists()
