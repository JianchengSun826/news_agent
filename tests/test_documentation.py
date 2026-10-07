from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INIT_RUNBOOKS = (
    ROOT / "docs/WORKFLOW.md",
    ROOT / "docs/superpowers/plans/2026-10-05-batch-workspace-finder-workflow.md",
)


@pytest.mark.parametrize("path", INIT_RUNBOOKS, ids=lambda path: path.name)
def test_runbook_uses_guarded_cli_init_without_manual_lock_or_checkpoint(path: Path) -> None:
    blocks = re.findall(r"```bash\n(.*?)\n```", path.read_text(encoding="utf-8"), re.DOTALL)
    init_blocks = [block for block in blocks if 'newsroom --root "$NEWSROOM_ROOT" init' in block]

    assert len(init_blocks) == 1
    block = init_blocks[0]
    assert "Newsroom(root).init()" not in block
    assert "operation_lock(" not in block
    assert "wal_checkpoint(" not in block


def test_summary_docs_state_that_normal_init_owns_the_exclusive_guard() -> None:
    for path in (ROOT / "README.md", ROOT / "docs/WORKFLOW_ZH.md"):
        text = path.read_text(encoding="utf-8")
        assert "newsroom init" in text
        assert "独占" in text
        assert "operation lock" in text


def test_migration_runbook_uses_checked_rehearsal_copy_script() -> None:
    path = ROOT / "docs/superpowers/plans/2026-10-05-batch-workspace-finder-workflow.md"
    text = path.read_text(encoding="utf-8")

    assert 'scripts/create_rehearsal_copy.py "$production_root" "$rehearsal_root"' in text
    assert "assert_migration_access(source)" in (
        ROOT / "scripts/create_rehearsal_copy.py"
    ).read_text(encoding="utf-8")


def test_init_runbooks_require_complete_compatible_v4_before_idempotent_init() -> None:
    chinese_runbooks = (ROOT / "docs/WORKFLOW.md", ROOT / "docs/WORKFLOW_ZH.md")
    for path in chinese_runbooks:
        text = path.read_text(encoding="utf-8")
        assert "完整兼容的 v4 schema" in text
        assert "schema 保持不变" in text
    plan = INIT_RUNBOOKS[1].read_text(encoding="utf-8")
    assert "complete, compatible v4 schema" in plan
    assert "schema must remain unchanged" in plan


def test_chinese_migration_runbook_documents_current_schema_and_snapshot_guards() -> None:
    text = (ROOT / "docs/WORKFLOW_ZH.md").read_text(encoding="utf-8")

    assert "历史记录必须恰好为 `1..max(version)`" in text
    assert "含隐藏/生成列" in text
    assert "CHECK 约束" in text
    assert "带唯一后缀且绝不覆盖既有文件" in text
    assert "目标根路径本身也必须先用未解析路径检查" in text


def test_task_11_plan_documents_current_schema_and_rehearsal_guards() -> None:
    text = (ROOT / "docs/superpowers/plans/2026-10-05-batch-workspace-finder-workflow.md").read_text(
        encoding="utf-8"
    )
    task_11 = text.split("### Task 11:", 1)[1].split("### Task 12:", 1)[0]

    assert "history is exactly `1..max(version)`" in task_11
    assert "hidden/generated columns" in task_11
    assert "normalized CHECK constraint" in task_11
    assert "unique suffix and exclusive no-clobber creation" in task_11
    assert "unresolved target path before `resolve()`" in task_11
    assert "substr(name, 1, 7) != 'sqlite_'" in task_11
