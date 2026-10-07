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
