from pathlib import Path
import csv

from typer.testing import CliRunner

from newsroom.cli import app


runner = CliRunner()

def test_cli_init_is_idempotent(tmp_path: Path, capsys):
    first = runner.invoke(app, ["--root", str(tmp_path), "init"])
    second = runner.invoke(app, ["--root", str(tmp_path), "init"])
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert (tmp_path / "data" / "newsroom.db").exists()
    assert "initialized" in second.output.lower()


def test_cli_imports_candidate_csv(tmp_path: Path, capsys):
    assert runner.invoke(app, ["--root", str(tmp_path), "init"]).exit_code == 0
    csv_path = tmp_path / "candidates.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["candidate_title","core_fact","event_date","company","product","topic","subtopic","event_type","keywords","primary_source","secondary_sources","discovered_from","priority"])
        writer.writeheader()
        writer.writerow({"candidate_title":"Example","core_fact":"Example Co released Tool on 2026-09-29.","event_date":"2026-09-29","company":"Example Co","product":"Tool","topic":"AI","subtopic":"Tools","event_type":"launch","keywords":"example|tool","primary_source":"https://example.com/official","secondary_sources":"https://news.example.org/report","discovered_from":"manual","priority":"3"})
    imported = runner.invoke(app, ["--root", str(tmp_path), "candidate", "import", str(csv_path)])
    assert imported.exit_code == 0
    assert '"imported": 1' in imported.output
    export_path = tmp_path / "export.csv"
    assert runner.invoke(app, ["--root", str(tmp_path), "candidate", "export", str(export_path)]).exit_code == 0
    exported = list(csv.DictReader(export_path.open(encoding="utf-8")))
    assert exported[0]["core_fact"] == "Example Co released Tool on 2026-09-29."
    assert exported[0]["keywords"] == "example|tool"


def test_typer_exposes_required_nested_commands():
    for command in (["candidate", "add", "--help"], ["brief", "create", "--help"], ["article", "register", "--help"], ["fact-check", "register", "--help"], ["image", "register", "--help"], ["published", "list", "--help"]):
        result = runner.invoke(app, list(command))
        assert result.exit_code == 0, result.output
