"""Canonical batch paths and rebuildable, local Finder views."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, TYPE_CHECKING
from urllib.parse import quote

from .service import WorkflowError

if TYPE_CHECKING:
    from .service import Newsroom


BATCH_ACTIONS = (
    ("01-打开下一个Claude任务.command", "open-next-request"),
    ("02-导入Claude返回文件.command", "import-result"),
    ("03-打开整批审核材料.command", "open-review"),
    ("04-登记整批图片.command", "register-images"),
    ("05-完成整批审核.command", "review"),
    ("06-批准整个批次.command", "approve"),
    ("07-生成全部发布包.command", "build-packages"),
    ("08-刷新批次视图.command", "refresh"),
)


@dataclass(frozen=True)
class BatchWorkspacePaths:
    root: Path

    @property
    def requests(self) -> Path:
        return self.root / "claude" / "requests"

    @property
    def inbox(self) -> Path:
        return self.root / "claude" / "inbox"

    @property
    def archive(self) -> Path:
        return self.root / "claude" / "archive"

    @property
    def articles(self) -> Path:
        return self.root / "articles"

    @property
    def publish(self) -> Path:
        return self.root / "publish"

    @property
    def input(self) -> Path:
        return self.root / "input"

    @property
    def actions(self) -> Path:
        return self.root / "actions"


def _shell_literal(value: str) -> str:
    # Double quotes retain readable fixed IDs while disabling every expansion.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'


def render_action_script(batch_id: str, action: str) -> str:
    """Render only bootstrap code and safely quoted fixed dispatcher literals."""
    return '''#!/bin/zsh
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
search_dir="$script_dir"
while [[ "$search_dir" != "/" && ! -f "$search_dir/pyproject.toml" ]]; do
  search_dir="$(dirname "$search_dir")"
done
if [[ ! -f "$search_dir/pyproject.toml" ]]; then
  print -u2 -- "Cannot locate newsroom pyproject.toml above this action."
  exit 1
fi
repo_root="$search_dir"
exec uv run --project "$repo_root" newsroom --root "$repo_root" macos-action ''' + _shell_literal(batch_id) + " " + _shell_literal(action) + "\n"


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        raise WorkflowError(f"invalid workspace identifier: {value}")
    return value


def _atomic_write(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _cell(value: Any) -> str:
    return str(value if value is not None else "—").replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")


def _link(label: Any, path: str | None) -> str:
    return f"[{_cell(label)}](<{quote(path, safe='/.-_')}>)" if path else _cell(label)


class BatchWorkspace:
    def __init__(self, room: Newsroom):
        self.room = room
        self.root = room.root / "workspace"

    def batch_root(self, batch_id: str) -> Path:
        return self.root / "batches" / _identifier(batch_id)

    def article_root(self, batch_id: str, article_id: str) -> Path:
        return BatchWorkspacePaths(self.batch_root(batch_id)).articles / _identifier(article_id)

    def _batch(self, batch_id: str) -> dict[str, Any]:
        _identifier(batch_id)
        batch = self.room.db.one("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
        if not batch:
            raise WorkflowError(f"batch not found: {batch_id}")
        return batch

    def ensure_layout(self, batch_id: str) -> Path:
        self._batch(batch_id)
        paths = BatchWorkspacePaths(self.batch_root(batch_id))
        for directory in (paths.input, paths.requests, paths.inbox, paths.archive, paths.articles, paths.publish, paths.actions):
            directory.mkdir(parents=True, exist_ok=True)
        for article in self.room.db.all("SELECT article_id FROM articles WHERE owning_batch_id=? ORDER BY article_id", (batch_id,)):
            self.article_root(batch_id, article["article_id"]).mkdir(parents=True, exist_ok=True)
        _atomic_write(paths.inbox / "将Claude返回的JSON放在这里.txt", "将 Claude 返回的 JSON 文件放在这里，然后运行 02-导入Claude返回文件.command。\n")
        for filename, action in (("OPEN_BATCH.command", "open-batch"), *BATCH_ACTIONS):
            destination = paths.root / filename if filename == "OPEN_BATCH.command" else paths.actions / filename
            _atomic_write(destination, render_action_script(batch_id, action))
            destination.chmod(0o755)
        return paths.root

    def store_source_csv(self, batch_id: str, raw: bytes, source_sha256: str) -> Path:
        """Preserve the exact validated input only for its recorded batch hash."""
        batch = self._batch(batch_id)
        if source_sha256 != batch["source_sha256"] or hashlib.sha256(raw).hexdigest() != source_sha256:
            raise WorkflowError("CSV source_sha256 does not match the batch source_sha256")
        path = BatchWorkspacePaths(self.batch_root(batch_id)).input / "candidates.csv"
        if path.exists():
            if path.read_bytes() != raw:
                raise WorkflowError("stored source CSV differs from the batch input")
            return path
        _atomic_write_bytes(path, raw)
        return path

    def _relative(self, batch_root: Path, repository_path: str | None) -> str | None:
        if not repository_path:
            return None
        return Path(os.path.relpath(self.room.root / repository_path, batch_root)).as_posix()

    def refresh(self, batch_id: str) -> dict[str, Any]:
        """Rebuild views without changing canonical workflow or artifact records."""
        self._batch(batch_id)
        root = self.ensure_layout(batch_id)
        with self.room.db.connect() as connection:
            connection.execute("BEGIN")
            batch = dict(connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone())
            items = [dict(row) for row in connection.execute(
                "SELECT bi.*,c.candidate_title,a.title_cn,a.status AS article_status,a.owning_batch_id,"
                "a.revision_number,a.fact_check_outcome,a.image_id,a.draft_path,a.research_brief_path,a.fact_check_path "
                "FROM batch_items bi JOIN candidates c ON c.candidate_id=bi.candidate_id "
                "LEFT JOIN articles a ON a.article_id=bi.article_id WHERE bi.batch_id=? ORDER BY bi.candidate_id", (batch_id,))]
            requests = [dict(row) for row in connection.execute("SELECT * FROM batch_requests WHERE batch_id=? ORDER BY created_at,request_id", (batch_id,))]
            images = [dict(row) for row in connection.execute("SELECT ic.* FROM image_candidates ic JOIN batch_items bi ON bi.article_id=ic.article_id WHERE bi.batch_id=? ORDER BY ic.article_id,ic.image_candidate_id", (batch_id,))]
        for request in requests:
            for key in ("request_path", "result_path"):
                request[key] = self._relative(root, request[key])
        for item in items:
            article_id, owner = item["article_id"], item["owning_batch_id"]
            item["article_path"] = Path(os.path.relpath(self.article_root(owner, article_id), root)).as_posix() if article_id and owner else None
            for key in ("draft_path", "research_brief_path", "fact_check_path"):
                item[key] = self._relative(root, item[key])
            item["image_state"] = "REGISTERED" if item["image_id"] else ("PROPOSED" if any(image["article_id"] == article_id for image in images) else "MISSING")
            item["approval_state"] = item["article_status"] if item["article_status"] in {"REVIEWED", "APPROVED", "PUBLISHED"} else "PENDING"
            package = BatchWorkspacePaths(self.batch_root(owner)).publish / article_id if owner and article_id else None
            item["package_path"] = Path(os.path.relpath(package, root)).as_posix() if package and package.is_dir() else None
            if article_id and owner == batch_id:
                _atomic_write(self.article_root(owner, article_id) / "image-candidates.json", json.dumps(
                    self.room.list_image_candidates(article_id), ensure_ascii=False, indent=2
                ) + "\n")
        pending = [request for request in requests if request["status"] == "PENDING"]
        active = [item for item in items if item["item_status"] not in {"EXCLUDED", "REUSED_COMPLETE"}]
        if any(item["item_status"] == "WAITING_DUPLICATE" for item in active):
            next_action = "resolve-duplicates"
        elif pending or any(item["item_status"] == "NEEDS_REVISION" for item in active):
            next_action = "open-next-request"
        elif any(item["image_state"] != "REGISTERED" for item in active):
            next_action = "register-images"
        elif any(item["approval_state"] == "PENDING" for item in active):
            next_action = "review"
        elif any(item["approval_state"] == "REVIEWED" for item in active):
            next_action = "approve"
        elif any(not item["package_path"] for item in active):
            next_action = "build-packages"
        else:
            next_action = "open-review"
        state = {**batch, "counts": dict(sorted(Counter(item["item_status"] for item in items).items())), "next_action": next_action, "items": items, "requests": requests, "image_candidates": images}
        _atomic_write(root / "batch-state.json", json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        _atomic_write(root / "README.md", self._dashboard(state))
        self.install_root_actions()
        return state

    def _dashboard(self, state: dict[str, Any]) -> str:
        lines = [f"# {_cell(state['batch_id'])}", "", "此视图由 SQLite 生成，可删除后重新生成。", "", f"状态：{_cell(state['status'])}", f"来源 CSV：{_cell(state['source_filename'])}", f"创建：{_cell(state['created_at'])} · 更新：{_cell(state['updated_at'])}", f"下一步：{_cell(state['next_action'])}", "", "## 条目状态", "", " | ".join(f"{_cell(key)}: {value}" for key, value in state["counts"].items()), "", "## 文章", "", "| 标题 | 文章 ID | 条目状态 | 版本 | 核验 | 图片 | 审批 | 发布包 |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for item in state["items"]:
            lines.append("| " + " | ".join((_cell(item["title_cn"] or item["candidate_title"]), _link(item["article_id"], item["article_path"]), _cell(item["item_status"]), _cell(item["revision_number"]), _cell(item["fact_check_outcome"]), _cell(item["image_state"]), _cell(item["approval_state"]), _link("发布包", item["package_path"]) if item["package_path"] else "—")) + " |")
        lines += ["", "## Claude 交接", ""]
        for request in state["requests"]:
            lines.append(f"- {_link(request['request_type'], request['request_path'])} · {_cell(request['status'])}" + (f" · {_link('已归档结果', request['result_path'])}" if request["result_path"] else ""))
        lines += ["", "## 待处理事项", ""]
        for item in state["items"]:
            if item["item_status"] in {"WAITING_DUPLICATE", "NEEDS_REVISION"} or item["error_message"]:
                lines.append(f"- {_cell(item['candidate_id'])}: {_cell(item['item_status'])} · {_cell(item['error_message'])}")
            elif item["article_status"] == "FACT_CHECKED" and item["image_state"] != "REGISTERED":
                lines.append(f"- {_cell(item['article_id'])}: 人工确认图片权利并登记文件。")
        lines += ["", "## Finder 操作", "", "双击 OPEN_BATCH.command 打开此批次。操作仍保留人工核验、图片权利、审核与批准步骤。", ""]
        for filename, action in BATCH_ACTIONS:
            lines.append(f"- {_link(filename, 'actions/' + filename)}: {action}")
        return "\n".join(lines) + "\n"

    def install_root_actions(self) -> Path:
        """Set up the root dashboard/action location; launchers are added separately."""
        actions = self.root / "actions"
        actions.mkdir(parents=True, exist_ok=True)
        batches = self.room.db.all("SELECT b.*,COUNT(bi.candidate_id) AS total,SUM(CASE WHEN bi.item_status IN ('EXCLUDED','REUSED_COMPLETE','FACT_CHECKED') THEN 1 ELSE 0 END) AS completed FROM batches b LEFT JOIN batch_items bi ON b.batch_id=bi.batch_id GROUP BY b.batch_id ORDER BY b.created_at,b.batch_id")
        lines = ["# 新闻编辑室批次", "", "此视图由 SQLite 生成，可删除后重新生成。", "", "| 批次 | 状态 | 进度 | 最近更新 |", "| --- | --- | --- | --- |"]
        for batch in batches:
            lines.append(f"| {_link(batch['batch_id'], 'batches/' + _identifier(batch['batch_id']) + '/README.md')} | {_cell(batch['status'])} | {batch['completed'] or 0}/{batch['total']} | {_cell(batch['updated_at'])} |")
        _atomic_write(self.root / "README.md", "\n".join(lines) + "\n")
        return actions
