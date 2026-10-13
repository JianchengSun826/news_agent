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
    ("02-导入Claude返回文件.command", "import-result"),
    ("03-打开整批审核材料.command", "open-review"),
    ("05-确认并导出.command", "export"),
)

LEGACY_RELEASE_ACTIONS = (
    "04-登记整批图片.command",
    "01-打开下一个Claude任务.command",
    "05-完成整批审核.command", "06-批准整个批次.command",
    "07-生成全部发布包.command", "08-刷新批次视图.command",
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


def render_migration_script() -> str:
    """Reuse the relocatable bootstrap with a fixed migration dispatcher."""
    return render_action_script("unused", "unused").split(" macos-action ")[0] + " macos-migrate\n"


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

    def completed_root(self, batch_id: str) -> Path:
        return self.root / "completed" / _identifier(batch_id)

    def image_draft_path(self, batch_id: str) -> Path:
        return self.batch_root(batch_id) / "image-selections.json"

    def source_csv_path(self, batch_id: str) -> Path:
        return BatchWorkspacePaths(self.batch_root(batch_id)).input / "candidates.csv"

    def request_artifact_path(
        self, batch_id: str, request_type: str, revision_number: int, *,
        result: bool = False, historical_request_id: str | None = None,
    ) -> Path:
        """Derive migration destinations using the same canonical handoff names."""
        if request_type not in {"CONTENT", "FACT_CHECK", "REVISION"} or revision_number < 1:
            raise WorkflowError("invalid workspace request type or revision")
        paths = BatchWorkspacePaths(self.batch_root(batch_id))
        directory = paths.archive if result else paths.requests
        if historical_request_id:
            directory = directory / _identifier(historical_request_id)
        kind = request_type.lower().replace("_", "-")
        version = "" if request_type == "CONTENT" else f"-v{revision_number}"
        return directory / f"{kind}-{'result' if result else 'request'}{version}.json"

    def _batch(self, batch_id: str) -> dict[str, Any]:
        _identifier(batch_id)
        batch = self.room.db.one("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
        if not batch:
            raise WorkflowError(f"batch not found: {batch_id}")
        return batch

    def ensure_layout(self, batch_id: str) -> Path:
        self._batch(batch_id)
        from .retention import BatchRetention
        BatchRetention(self.room).require_process(batch_id)
        paths = BatchWorkspacePaths(self.batch_root(batch_id))
        for directory in (paths.input, paths.requests, paths.inbox, paths.archive, paths.articles, paths.publish, paths.actions):
            directory.mkdir(parents=True, exist_ok=True)
        for article in self.room.db.all("SELECT article_id FROM articles WHERE owning_batch_id=? ORDER BY article_id", (batch_id,)):
            self.article_root(batch_id, article["article_id"]).mkdir(parents=True, exist_ok=True)
        _atomic_write(paths.inbox / "将Claude返回的JSON放在这里.txt", "将 Claude 返回的 JSON 文件放在这里，然后运行 02-导入Claude返回文件.command。\n")
        for filename in LEGACY_RELEASE_ACTIONS:
            (paths.actions / filename).unlink(missing_ok=True)
        for filename, action in (("OPEN_BATCH.command", "open-batch"), *BATCH_ACTIONS):
            destination = paths.root / filename if filename == "OPEN_BATCH.command" else paths.actions / filename
            _atomic_write(destination, render_action_script(batch_id, action))
            destination.chmod(0o755)
        return paths.root

    def store_source_csv(self, batch_id: str, raw: bytes, source_sha256: str) -> Path:
        """Preserve the exact validated input only for its recorded batch hash."""
        batch = self._batch(batch_id)
        from .retention import BatchRetention
        BatchRetention(self.room).require_process(batch_id)
        if source_sha256 != batch["source_sha256"] or hashlib.sha256(raw).hexdigest() != source_sha256:
            raise WorkflowError("CSV source_sha256 does not match the batch source_sha256")
        path = BatchWorkspacePaths(self.batch_root(batch_id)).input / "candidates.csv"
        if path.exists():
            if path.read_bytes() != raw:
                raise WorkflowError("stored source CSV differs from the batch input")
        else:
            _atomic_write_bytes(path, raw)
        metadata = {"source_filename": Path(batch["source_filename"]).name,
                    "source_sha256": source_sha256, "available": True}
        _atomic_write(path.parent / "source-metadata.json", json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        return path

    def _relative(self, batch_root: Path, repository_path: str | None) -> str | None:
        if not repository_path:
            return None
        return Path(os.path.relpath(self.room.root / repository_path, batch_root)).as_posix()

    def snapshot(self, batch_id: str) -> dict[str, Any]:
        """Read canonical state and existing package paths without writing views."""
        self._batch(batch_id)
        root = self.batch_root(batch_id)
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
        from .retention import BatchRetention
        retention = BatchRetention(self.room).record(batch_id)
        for request in requests:
            for key in ("request_path", "result_path"):
                request[key] = self._relative(root, request[key])
        for item in items:
            article_id, owner = item["article_id"], item["owning_batch_id"]
            item["article_path"] = Path(os.path.relpath(self.article_root(owner, article_id), root)).as_posix() if article_id and owner else None
            if owner and BatchRetention(self.room).record(owner) and item['draft_path']:
                item['article_path'] = self._relative(root, str(Path(item['draft_path']).parent))
            for key in ("draft_path", "research_brief_path", "fact_check_path"):
                item[key] = self._relative(root, item[key])
            item["image_state"] = "REGISTERED" if item["image_id"] else ('NO_IMAGE' if article_id and self.room.no_image_selected(item) else ('EXTERNAL' if article_id and self.room.external_images(item) else ("PROPOSED" if any(image["article_id"] == article_id for image in images) else "MISSING")))
            item["approval_state"] = item["article_status"] if item["article_status"] in {"REVIEWED", "APPROVED", "PUBLISHED"} else "PENDING"
            package = BatchWorkspacePaths(self.batch_root(owner)).publish / article_id if owner and article_id else None
            current_package = BatchWorkspacePaths(root).publish / article_id if article_id else None
            if item["item_status"] not in {"EXCLUDED", "REUSED_COMPLETE"} and current_package and current_package.is_dir():
                package = current_package
            item["package_path"] = Path(os.path.relpath(package, root)).as_posix() if package and package.is_dir() else None
        pending = [request for request in requests if request["status"] == "PENDING"]
        active = [item for item in items if item["item_status"] not in {"EXCLUDED", "REUSED_COMPLETE"}]
        if retention:
            next_action = 'complete' if retention['state'] == 'FINAL_ONLY' else 'export'
        elif any(item["item_status"] == "WAITING_DUPLICATE" for item in active):
            next_action = "resolve-duplicates"
        elif pending or any(item["item_status"] == "NEEDS_REVISION" for item in active):
            next_action = "upload-request"
        elif any(item["approval_state"] == "PENDING" for item in active):
            next_action = "export"
        elif any(item["approval_state"] == "REVIEWED" for item in active):
            next_action = "export"
        elif any(not item["package_path"] for item in active):
            next_action = "export"
        elif batch["status"] == "PACKAGED":
            next_action = "complete"
        else:
            next_action = "open-review"
        return {**batch, "retention_state": retention["state"] if retention else None, "counts": dict(sorted(Counter(item["item_status"] for item in items).items())), "next_action": next_action, "items": items, "requests": requests, "image_candidates": images}

    def refresh(self, batch_id: str) -> dict[str, Any]:
        """Rebuild views without changing canonical workflow or artifact records."""
        state = self.snapshot(batch_id)
        if state['retention_state']:
            from .retention import BatchRetention
            BatchRetention(self.room).validate(batch_id, strict=False)
            self.install_root_actions()
            return state
        root = self.ensure_layout(batch_id)
        for item in state["items"]:
            if item["article_id"] and item["owning_batch_id"] == batch_id:
                _atomic_write(self.article_root(batch_id, item["article_id"]) / "image-candidates.json", json.dumps(
                    self.room.list_image_candidates(item["article_id"]), ensure_ascii=False, indent=2
                ) + "\n")
        self._refresh_image_recommendations(batch_id)
        _atomic_write(root / "batch-state.json", json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        _atomic_write(root / "README.md", self._dashboard(state))
        self.install_root_actions()
        return state

    def _refresh_image_recommendations(self, batch_id: str) -> None:
        """Rebuild search leads from the canonical archived content result."""
        from .image_guide import render_image_guide
        for article in self.room.db.all('SELECT * FROM articles WHERE owning_batch_id=?', (batch_id,)):
            _atomic_write(self.article_root(batch_id, article['article_id']) / 'image-guide.md', render_image_guide(self.room, article))
        requests = self.room.db.all(
            "SELECT DISTINCT br.result_path FROM batch_requests br "
            "JOIN batch_items bi ON bi.batch_id=br.batch_id "
            "JOIN articles a ON a.article_id=bi.article_id "
            "WHERE a.owning_batch_id=? AND br.request_type='CONTENT' "
            "AND br.status='COMPLETED' AND br.result_path IS NOT NULL", (batch_id,)
        )
        from .batch_models import BatchContentResult

        owned_ids = {row['article_id'] for row in self.room.db.all(
            "SELECT article_id FROM articles WHERE owning_batch_id=?", (batch_id,)
        )}
        results = [BatchContentResult.model_validate_json(
            (self.room.root / request['result_path']).read_bytes()
        ) for request in requests]
        labels = {'PAGE_INACCESSIBLE': '页面无法访问', 'INSUFFICIENT_EVIDENCE': '授权证据不足', 'RESTRICTED_USE': '存在使用限制'}
        for item in (item for result in results for item in result.items if item.article_id in owned_ids):
            root = self.article_root(batch_id, item.article_id)
            recommendations = [r.model_dump(mode='json') for r in item.image_recommendations]
            _atomic_write(root / 'image-recommendations.json', json.dumps(recommendations, ensure_ascii=False, indent=2) + '\n')
            lines = ['# 图片推荐：待人工判断', '', '以下推荐没有完成授权核验。完整分类和使用条件见 image-guide.md；导出后自行下载并上传 Google Drive。',
                '如决定加入，请核对适用授权、使用范围与署名；复制 manual-image-candidates.json 到另一个文件，删除不采用项，填写完整权利字段后导入。',
                '导入命令：', '', '```bash', f'uv run newsroom --root . batch image-candidates-import {batch_id} /完整路径/补充图片.json --actor editor --reason "人工核对适用授权及署名"', '```', '']
            for index, r in enumerate(recommendations, 1):
                lines += [f'## 推荐 {index} · {labels[r["verification_status"]]}', '',
                    f'- 来源：{r["landing_page_url"]}', f'- 权利线索：{r["rights_url"] or "未找到"}',
                    f'- 作者：{_cell(r["creator"])}', f'- 已知权利信息：{_cell(r["rights_information"])}',
                    f'- 未通过原因：{_cell(r["verification_issue"])}', f'- 推荐理由：{_cell(r["relevance"])}', '']
            if not recommendations:
                lines.append('没有待判断的推荐图片。')
            _atomic_write(root / 'image-recommendations.md', '\n'.join(lines) + '\n')
            template_path = root / 'manual-image-candidates.json'
            # This editable draft is not a generated view: preserve human work on refresh.
            if recommendations and not template_path.exists():
                candidates = [{key: r[key] for key in ('landing_page_url', 'direct_image_url', 'creator', 'rights_url', 'relevance')} | {
                    'rights_basis': None, 'license_name': None, 'rights_statement': None,
                    'attribution_required': None, 'attribution_text': None,
                } for r in recommendations]
                template = {'schema_version': 1, 'batch_id': batch_id, 'items': [{'article_id': item.article_id, 'image_candidates': candidates}]}
                _atomic_write(template_path, json.dumps(template, ensure_ascii=False, indent=2) + '\n')

    def _dashboard(self, state: dict[str, Any]) -> str:
        lines = [f"# {_cell(state['batch_id'])}", "", "此视图由 SQLite 生成，可删除后重新生成。", "", f"状态：{_cell(state['status'])}", f"来源 CSV：{_cell(state['source_filename'])}", f"创建：{_cell(state['created_at'])} · 更新：{_cell(state['updated_at'])}", f"下一步：{_cell(state['next_action'])}", "", "## 条目状态", "", " | ".join(f"{_cell(key)}: {value}" for key, value in state["counts"].items()), "", "## 文章", "", "| 标题 | 文章 ID | 条目状态 | 版本 | 核验 | 图片 | 审批 | 发布包 |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        if state["status"] == "PACKAGED":
            lines += ["", "## 导出完成", "", "正文与图片指南已导出，本地内容任务完成。" + _link("打开导出目录", "publish/") + "。图片由编辑自行下载并上传 Google Drive。", ""]
            if self.completed_root(state["batch_id"]).is_dir():
                lines += ["完成文章：" + _link("正文与图片指南", "../../completed/" + state["batch_id"] + "/") + "。最终成果验证后清理本批次过程材料。", ""]
        available = self.source_csv_path(state["batch_id"]).is_file()
        lines.insert(7, "来源 CSV 副本：" + ("可用" if available else "未提供；原始文件名与 SHA-256 保存在 input/source-metadata.json。"))
        for item in state["items"]:
            lines.append("| " + " | ".join((_cell(item["title_cn"] or item["candidate_title"]), _link(item["article_id"], item["article_path"]), _cell(item["item_status"]), _cell(item["revision_number"]), _cell(item["fact_check_outcome"]), _cell(item["image_state"]), _cell(item["approval_state"]), _link("发布包", item["package_path"]) if item["package_path"] else "—")) + " |")
        lines += ["", "## 当前 Claude 任务", ""]
        pending = [request for request in state["requests"] if request["status"] == "PENDING"]
        if pending:
            lines += ["手动新建 Claude 对话并上传下面的任务文件。文件的 instructions 已包含完整任务，可省略重复的长指令。若应用要求文字输入或未开始执行，发送“执行附件任务”。", ""]
            for request in pending:
                conversation = "必须使用全新对话进行独立事实核验。" if request["request_type"] == "FACT_CHECK" else "使用研究/写作对话；首次写稿请新建对话。"
                lines += [f"- 上传文件：{_link(request['request_type'], request['request_path'])}",
                          f"- 任务 ID：{_cell(request['request_id'])}", f"- {conversation}",
                          f"- 预期返回文件：{_cell(request['expected_result_filename'])}", ""]
            lines.append("下载返回的 JSON 到 claude/inbox/，然后双击 02-导入Claude返回文件.command。")
        elif any(item["item_status"] == "NEEDS_REVISION" for item in state["items"]):
            lines.append(f"需要修订但尚无任务；先运行 newsroom batch revision-request {_cell(state['batch_id'])} 创建任务，再查看此处的上传文件。")
        else:
            lines.append("当前没有待上传的 Claude 任务。按下一步提示继续。")
        lines += ["", "## 图片指南", "", "区分已核实可用图片与授权待确认的推荐。05 会随正文导出指南；导出后自行下载图片并上传 Google Drive。", ""]
        for item in state['items']:
            if item['article_id'] and item['owning_batch_id']:
                path = self.article_root(item['owning_batch_id'], item['article_id']) / 'image-guide.md'
                if path.is_file():
                    relative = os.path.relpath(path, self.batch_root(state['batch_id']))
                    lines.append(f"- {_link(item['article_id'], relative)}")
        lines += ["", "## Claude 交接", ""]
        for request in state["requests"]:
            lines.append(f"- {_link(request['request_type'], request['request_path'])} · {_cell(request['status'])}" + (f" · {_link('已归档结果', request['result_path'])}" if request["result_path"] else ""))
        lines += ["", "## 待处理事项", ""]
        for item in state["items"]:
            if item["item_status"] in {"WAITING_DUPLICATE", "NEEDS_REVISION"} or item["error_message"]:
                lines.append(f"- {_cell(item['candidate_id'])}: {_cell(item['item_status'])} · {_cell(item['error_message'])}")
            elif item["article_status"] == "FACT_CHECKED":
                lines.append(f"- {_cell(item['article_id'])}: 检查正文和图片指南后，使用 05 确认并导出。")
        lines += ["", "## Finder 操作", "", "双击 OPEN_BATCH.command 打开此批次。操作仍保留人工核验、图片权利、审核与批准步骤。", ""]
        for filename, action in BATCH_ACTIONS:
            lines.append(f"- {_link(filename, 'actions/' + filename)}: {action}")
        return "\n".join(lines) + "\n"

    def install_root_actions(self) -> Path:
        """Install a relocatable, confirmation-gated root migration launcher."""
        actions = self.root / "actions"
        actions.mkdir(parents=True, exist_ok=True)
        batches = self.room.db.all("SELECT b.*,COUNT(bi.candidate_id) AS total,SUM(CASE WHEN bi.item_status IN ('EXCLUDED','REUSED_COMPLETE','FACT_CHECKED') THEN 1 ELSE 0 END) AS completed FROM batches b LEFT JOIN batch_items bi ON b.batch_id=bi.batch_id GROUP BY b.batch_id ORDER BY b.created_at,b.batch_id")
        lines = ["# 新闻编辑室批次", "", "此视图由 SQLite 生成，可删除后重新生成。", "", "| 批次 | 状态 | 进度 | 最近更新 |", "| --- | --- | --- | --- |"]
        for batch in batches:
            if self.room.batch_archived(batch['batch_id']) or batch['status'] in {'PACKAGED', 'PUBLISHED'}:
                continue
            lines.append(f"| {_link(batch['batch_id'], 'batches/' + _identifier(batch['batch_id']) + '/README.md')} | {_cell(batch['status'])} | {batch['completed'] or 0}/{batch['total']} | {_cell(batch['updated_at'])} |")
        completed = [batch for batch in batches if batch['status'] in {'PACKAGED', 'PUBLISHED'} and not self.room.batch_archived(batch['batch_id'])]
        if completed:
            lines += ['', '## 已完成', '', '最终成果集中在 completed/；新版包含正文和图片指南，旧版已登记图片仍保留。已清理批次的去重及审计记录保留在 SQLite。', '']
            for batch in completed:
                batch_id = batch['batch_id']
                output = 'completed/' + batch_id + '/' if self.completed_root(batch_id).is_dir() else 'batches/' + batch_id + '/publish/'
                history = ' · ' + _link('历史记录', 'batches/' + batch_id + '/README.md') if self.batch_root(batch_id).is_dir() else ''
                lines.append('- ' + _link(batch_id, output) + history)
        archived = [batch for batch in batches if self.room.batch_archived(batch['batch_id'])]
        if archived:
            lines += ['', '## 已归档批次', '', '素材和数据库记录仍保留在原路径；不要手动删除批次目录。', '']
            for batch in archived:
                batch_id = batch['batch_id']
                link = 'completed/' + batch_id + '/' if self.completed_root(batch_id).is_dir() else 'batches/' + batch_id + '/README.md'
                lines.append('- ' + _link(batch_id, link))
        _atomic_write(self.root / "README.md", "\n".join(lines) + "\n")
        launcher = self.root / "Migrate Newsroom.command"
        _atomic_write(launcher, render_migration_script())
        launcher.chmod(0o755)
        return actions
