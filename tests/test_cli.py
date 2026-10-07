from contextlib import closing
import csv
import json
import shutil
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from newsroom.cli import app
from newsroom.db import Database


runner = CliRunner()


def candidate_add_args() -> list[str]:
    return [
        "candidate", "add",
        "--candidate-title", "Example launches Tool",
        "--core-fact", "Example launched Tool on 2026-09-29.",
        "--event-date", "2026-09-29",
        "--company", "Example",
        "--product", "Tool",
        "--topic", "AI",
        "--subtopic", "Developer Tools",
        "--event-type", "launch",
        "--primary-source", "https://example.com/official",
    ]

def test_cli_init_is_idempotent(tmp_path: Path, capsys):
    first = runner.invoke(app, ["--root", str(tmp_path), "init"])
    second = runner.invoke(app, ["--root", str(tmp_path), "init"])
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert (tmp_path / "data" / "newsroom.db").exists()
    assert "initialized" in second.output.lower()


def test_cli_init_rejects_future_schema_without_backup_or_database_changes(tmp_path: Path):
    db_path = tmp_path / "data" / "newsroom.db"
    db_path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_version VALUES (5, 'future')")
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('preserve-me')")
        connection.commit()
    before = db_path.read_bytes()

    result = runner.invoke(app, ["--root", str(tmp_path), "init"])

    assert result.exit_code != 0
    assert "schema version 5" in result.output
    assert "supports versions 1 through 4" in result.output
    assert db_path.read_bytes() == before
    assert not (db_path.parent / "backups").exists()
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "preserve-me"


def test_cli_init_rejects_unversioned_user_database_without_changes(tmp_path: Path):
    db_path = tmp_path / "data/newsroom.db"
    db_path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("CREATE TABLE legacy_content (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_content VALUES ('preserve-me')")
        connection.commit()
    before = db_path.read_bytes()

    result = runner.invoke(app, ["--root", str(tmp_path), "init"])

    assert result.exit_code != 0
    assert "user tables" in result.output
    assert "no schema_version" in result.output
    assert "new empty database" in result.output
    assert db_path.read_bytes() == before
    assert not (db_path.parent / "backups").exists()


def test_cli_init_checkpoints_v3_wal_and_creates_complete_upgrade_backup(tmp_path: Path):
    source = tmp_path / "source/data/newsroom.db"
    Database(source).initialize("2026-10-06T00:00:00+00:00")
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        writer.execute("DELETE FROM schema_version WHERE version=4")
        writer.execute(
            "INSERT INTO sources(source_id,source_name,domain,source_type,reliability_level,usage_policy,notes,active) "
            "VALUES ('wal-source','WAL Source','wal.example','PRIMARY','HIGH','LINK_ONLY','committed in WAL',1)"
        )
        writer.commit()
        root = tmp_path / "target"
        database = root / "data/newsroom.db"
        database.parent.mkdir(parents=True)
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(Path(f"{source}{suffix}"), Path(f"{database}{suffix}"))
    finally:
        writer.close()

    result = runner.invoke(app, ["--root", str(root), "init"])

    assert result.exit_code == 0, result.output
    backup_path = next((root / "data/backups").glob("newsroom-v3-*.db"))
    assert Database(backup_path).one("SELECT notes FROM sources WHERE source_id='wal-source'")["notes"] == "committed in WAL"
    assert Database(database).one("SELECT MAX(version) AS version FROM schema_version")["version"] == 4


def test_cli_candidate_add_accepts_primary_only(tmp_path: Path):
    assert runner.invoke(app, ["--root", str(tmp_path), "init"]).exit_code == 0

    result = runner.invoke(app, ["--root", str(tmp_path), *candidate_add_args()])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["secondary_sources"] == []


def test_cli_candidate_add_preserves_repeated_secondary_sources(tmp_path: Path):
    assert runner.invoke(app, ["--root", str(tmp_path), "init"]).exit_code == 0

    result = runner.invoke(app, [
        "--root", str(tmp_path), *candidate_add_args(),
        "--secondary-source", "https://news.example.org/report",
        "--secondary-source", "https://analysis.example.net/story",
    ])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["secondary_sources"] == [
        "https://news.example.org/report",
        "https://analysis.example.net/story",
    ]


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


def test_cli_imports_primary_only_candidate_csv(tmp_path: Path):
    assert runner.invoke(app, ["--root", str(tmp_path), "init"]).exit_code == 0
    csv_path = tmp_path / "primary-only.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["candidate_title","core_fact","event_date","company","product","topic","subtopic","event_type","keywords","primary_source","secondary_sources","discovered_from","priority"])
        writer.writeheader()
        writer.writerow({"candidate_title":"Example","core_fact":"Example Co released Tool on 2026-09-29.","event_date":"2026-09-29","company":"Example Co","product":"Tool","topic":"AI","subtopic":"Tools","event_type":"launch","keywords":"example|tool","primary_source":"https://example.com/official","secondary_sources":"","discovered_from":"gemini-spark","priority":"3"})

    imported = runner.invoke(app, ["--root", str(tmp_path), "candidate", "import", str(csv_path)])

    assert imported.exit_code == 0, imported.output
    candidate_id = json.loads(imported.output)["candidate_ids"][0]
    shown = runner.invoke(app, ["--root", str(tmp_path), "candidate", "show", candidate_id])
    assert json.loads(shown.output)["secondary_sources"] == []


def test_typer_exposes_required_nested_commands(tmp_path):
    for command in (["candidate", action, "--help"] for action in ("add", "import", "export", "list", "show", "select", "reject")):
        result = runner.invoke(app, ['--root', str(tmp_path), *command])
        assert result.exit_code == 0, result.output
    for command in (["duplicate-check", "--help"], ["published", "list", "--help"], ["stats", "--help"]):
        result = runner.invoke(app, ['--root', str(tmp_path), *command])
        assert result.exit_code == 0, result.output


def test_fresh_init_does_not_create_legacy_artifact_roots(tmp_path):
    result = CliRunner().invoke(app, ["--root", str(tmp_path), "init"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "data/newsroom.db").is_file()
    assert all(not (tmp_path / name).exists() for name in ("research", "articles", "images", "output"))
