from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from newsroom.service import Newsroom


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/create_rehearsal_copy.py"


def _run(source: Path, target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(source), str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _source_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_rehearsal_copy_refuses_persistent_migration_barrier_before_copy(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    Newsroom(source).init()
    (source / "data/workspace-migration-lock.json").write_text("{}", encoding="utf-8")
    before = _source_bytes(source)

    result = _run(source, target)

    assert result.returncode != 0
    assert "migration access barrier active" in result.stderr
    assert not target.exists()
    assert _source_bytes(source) == before


def test_rehearsal_copy_refuses_unresolved_journal_before_copy(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    Newsroom(source).init()
    journals = source / "data/workspace-migration-journals"
    journals.mkdir()
    (journals / "unfinished.json").write_text("{}", encoding="utf-8")
    before = _source_bytes(source)

    result = _run(source, target)

    assert result.returncode != 0
    assert "migration journal unresolved or invalid" in result.stderr
    assert not target.exists()
    assert _source_bytes(source) == before


def test_rehearsal_copy_copies_normal_root_coherently_and_preserves_symlinks(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    Newsroom(source).init()
    (source / "research").mkdir()
    (source / "research/source.md").write_text("source", encoding="utf-8")
    (source / "research/reference").symlink_to("source.md")
    before = _source_bytes(source)

    result = _run(source, target)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["database_integrity"] == "ok"
    assert (target / "data/newsroom.db").is_file()
    assert (target / "research/reference").is_symlink()
    assert (target / "research/reference").readlink() == Path("source.md")
    assert _source_bytes(source) == before
