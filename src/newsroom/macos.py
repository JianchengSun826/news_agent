"""Human-operated Finder actions; SQLite and typed services own workflow gates."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Protocol

from pydantic import ValidationError

from .batch import BatchWorkflow
from .batch_models import (
    BatchContentResult, BatchFactCheckResult, BatchImagePlan, BatchImageSelection,
    BatchRevisionResult, NoImageSelection,
)
from .batch_release import BatchReleaseWorkflow
from .coordination import operation_lock
from .service import Newsroom, WorkflowError
from .workspace import BatchWorkspace, BatchWorkspacePaths


ALLOWED_ACTIONS = {
    "open-batch", "open-next-request", "import-result", "open-review",
    "register-images", "review", "approve", "build-packages", "refresh", "export",
}


class DialogPort(Protocol):
    def alert(self, title: str, message: str, *, error: bool = False) -> None: ...
    def confirm(self, title: str, message: str) -> bool: ...
    def choose_file(self, title: str, prompt: str) -> Path | None: ...
    def choose_list(self, title: str, prompt: str, choices: list[str]) -> str | None: ...
    def reveal_file(self, path: Path) -> None: ...
    def open_file(self, path: Path) -> None: ...
    def open_url(self, url: str) -> None: ...


class OsaScriptDialogs:
    """Pass all dynamic text as argv, never as AppleScript or shell source."""

    @staticmethod
    def _execute(body: str, *args: str) -> str | None:
        if sys.platform != "darwin":
            raise OSError("Finder 操作仅支持 macOS；请在 macOS 上运行。")
        script = """on run argv
try
""" + body + """
on error messageText number errorNumber
    if errorNumber is -128 then return "NEWSROOM_CANCELLED"
    error messageText number errorNumber
end try
end run
"""
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script, *args],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise OSError(result.stderr.strip() or "macOS 操作失败")
        output = result.stdout.rstrip("\n")
        return None if output == "NEWSROOM_CANCELLED" else output

    def alert(self, title: str, message: str, *, error: bool = False) -> None:
        self._execute('''if item 3 of argv is "true" then
    display alert (item 1 of argv) message (item 2 of argv) as critical
else
    display alert (item 1 of argv) message (item 2 of argv)
end if
return ""''', title, message, "true" if error else "false")

    def confirm(self, title: str, message: str) -> bool:
        return self._execute('''display dialog (item 2 of argv) with title (item 1 of argv) buttons {"取消", "确认"} default button "确认" cancel button "取消"
return "true"''', title, message) == "true"

    def choose_file(self, title: str, prompt: str) -> Path | None:
        value = self._execute('''set selectedFile to choose file with prompt ((item 1 of argv) & return & (item 2 of argv))
return POSIX path of selectedFile''', title, prompt)
        return Path(value) if value is not None else None

    def choose_list(self, title: str, prompt: str, choices: list[str]) -> str | None:
        if not choices:
            return None
        return self._execute('''set selectedItems to choose from list (items 3 thru -1 of argv) with title (item 1 of argv) with prompt (item 2 of argv) OK button name "选择" cancel button name "取消" multiple selections allowed false empty selection allowed false
if selectedItems is false then return "NEWSROOM_CANCELLED"
return item 1 of selectedItems''', title, prompt, *choices)

    def reveal_file(self, path: Path) -> None:
        self._execute('''set targetFile to POSIX file (item 1 of argv) as alias
tell application "Finder"
    reveal targetFile
    activate
end tell
return ""''', str(path))

    def open_file(self, path: Path) -> None:
        self._execute('''set targetFile to POSIX file (item 1 of argv) as alias
tell application "Finder"
    open targetFile
    activate
end tell
return ""''', str(path))

    def open_url(self, url: str) -> None:
        self._execute('''open location (item 1 of argv)
return ""''', url)


class MacOSBatchController:
    def __init__(self, room: Newsroom, dialogs: DialogPort):
        self.room = room
        self.dialogs = dialogs
        self.workflow = BatchWorkflow(room)
        self.release = BatchReleaseWorkflow(room)
        self.workspace = BatchWorkspace(room)

    def run(self, batch_id: str, action: str) -> int:
        """Return zero on success/cancellation and two on expected user errors."""
        try:
            with operation_lock(self.room.root, exclusive=action == "export"):
                return self._run(batch_id, action)
        except (WorkflowError, ValidationError, OSError) as error:
            return self._report_error(error)

    def _run(self, batch_id: str, action: str) -> int:
        try:
            from .migration import assert_migration_access
            assert_migration_access(self.room.root)
            if action not in ALLOWED_ACTIONS:
                raise WorkflowError("未知的 Finder 操作。请从批次 actions 文件夹选择操作。")
            # Validate the identifier and existence even for read-only actions.
            self.workspace._batch(batch_id)
            if action == "open-batch":
                state = self.workspace.snapshot(batch_id)
                root = self.workspace.batch_root(batch_id)
                self.dialogs.open_file(root)
                self.dialogs.open_file(root / "README.md")
                self._show_status(state)
            elif action == "open-next-request":
                request = self._pending_request(batch_id)
                if request is None and self.workflow.status(batch_id)["action_required"]["needs_revision_article_ids"]:
                    raise WorkflowError(
                        "当前文章需要修订，但没有待处理的修订任务。此打开操作不会创建任务。"
                        f"请由编辑明确运行 newsroom batch revision-request {batch_id} 生成任务后再打开。"
                    )
                if request is None:
                    raise WorkflowError("当前没有待处理的 Claude 任务。请查看批次 README 并完成下一步。")
                self._open_request(request)
            elif action == "open-review":
                root = self.workspace.batch_root(batch_id)
                self.dialogs.open_file(root / "articles")
                self.dialogs.open_file(root / "README.md")
            elif action == "import-result":
                self._import_result(batch_id)
            elif action == "register-images":
                self._register_images(batch_id)
            elif action in {"review", "approve", "build-packages", "export"}:
                self._release_action(batch_id, action)
            else:
                self._show_status(self.workspace.refresh(batch_id))
            return 0
        except (WorkflowError, ValidationError, OSError) as error:
            return self._report_error(error)

    def _report_error(self, error: WorkflowError | ValidationError | OSError) -> int:
        message = self._error_message(error)
        print(f"操作未完成：{message}", file=sys.stderr)
        try:
            self.dialogs.alert("操作未完成", message, error=True)
        except OSError as dialog_error:
            print(f"操作未完成：{message}\n无法显示对话框：{dialog_error}", file=sys.stderr)
        return 2

    def _pending_request(self, batch_id: str) -> dict | None:
        rows = self.room.db.all(
            "SELECT * FROM batch_requests WHERE batch_id=? AND status='PENDING' ORDER BY created_at,request_id",
            (batch_id,),
        )
        if len(rows) > 1:
            raise WorkflowError("批次存在多个待处理任务。请通过 batch status 检查并修复任务状态。")
        return rows[0] if rows else None

    def _open_request(self, request: dict) -> None:
        path = self.room.root / request["request_path"]
        if not path.is_file():
            raise WorkflowError(f'任务文件缺失：{path}。请检查批次素材并从备份恢复。')
        fallback = ''
        try:
            self.dialogs.reveal_file(path)
            self.dialogs.open_file(path)
        except OSError as error:
            fallback = f'Finder 定位失败：{error}。请用 ⌘⇧G 打开下面的文件夹，手动上传任务文件。\n文件夹：{path.parent}\n'
        print(f'Claude 任务文件：{path}\n{fallback}')
        self.dialogs.alert(
            "Claude 任务",
            "请上传此 JSON 到 Claude；事实核验必须使用全新的对话。\n"
            "文件 instructions 已包含完整任务，可省略重复长指令；如需文字输入，发送“执行附件任务”。\n"
            f"任务文件：{path}\n{fallback}返回文件：{request['expected_result_filename']}",
        )

    def _show_status(self, state: dict) -> None:
        counts = "，".join(f"{key}: {value}" for key, value in state["counts"].items())
        message = f"{state['batch_id']}\n状态：{state['status']}\n{counts}\n下一步：{state['next_action']}"
        print(message)
        self.dialogs.alert("批次状态", message)

    @staticmethod
    def _identity_matches(payload: object, request: dict) -> bool:
        return isinstance(payload, dict) and (
            payload.get("batch_id") == request["batch_id"]
            and payload.get("request_id") == request["request_id"]
            and payload.get("output_file") == request["expected_result_filename"]
        )

    def _import_result(self, batch_id: str) -> None:
        request = self._pending_request(batch_id)
        if request is None:
            raise WorkflowError("没有待导入的 Claude 任务。请查看批次 README 的当前任务与下一步。")
        inbox = BatchWorkspacePaths(self.workspace.batch_root(batch_id)).inbox
        matching = []
        for path in sorted(inbox.glob("*.json")):
            try:
                payload = json.loads(path.read_bytes())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if self._identity_matches(payload, request):
                matching.append(path)
        if len(matching) == 1:
            path = matching[0]
        elif matching:
            choices = {str(path): path for path in matching}
            selected = self.dialogs.choose_list("导入 Claude 结果", "请选择属于当前任务的返回文件。", list(choices))
            if selected is None:
                print('已取消导入；没有导入文件。')
                return
            if selected not in choices:
                raise WorkflowError("所选文件不在当前任务列表中。请重新选择。")
            path = choices[selected]
        else:
            path = self.dialogs.choose_file("导入 Claude 结果", f"请选择 {request['expected_result_filename']}。")
            if path is None:
                print('已取消导入；没有导入文件。')
                return
        self._validate_result(Path(path), request)
        if not self.dialogs.confirm("导入 Claude 结果", f"确认导入当前批次的 {request['request_type']} 结果？\n{path}"):
            print('已取消导入；没有导入文件。')
            return
        importer = {
            "CONTENT": self.workflow.import_content,
            "FACT_CHECK": self.workflow.import_fact_checks,
            "REVISION": self.workflow.import_revisions,
        }[request["request_type"]]
        importer(batch_id, Path(path))
        imported_count = len(json.loads(Path(path).read_bytes())['items'])
        message = f"导入成功：{request['request_type']}，{imported_count} 篇。\n批次：{batch_id}"
        print(message)
        self.dialogs.alert('导入成功', message)
        if request["request_type"] == "FACT_CHECK":
            status = self.workflow.status(batch_id)
            revision = self.room.db.one(
                "SELECT request_id FROM batch_requests WHERE batch_id=? AND request_type='REVISION' AND status='PENDING'",
                (batch_id,),
            )
            if status["action_required"]["needs_revision_article_ids"] and revision is None:
                self.workflow.create_revision_request(batch_id)
        self._show_status(self.workspace.refresh(batch_id))

    def _validate_result(self, path: Path, request: dict) -> None:
        raw = path.read_bytes()
        try:
            identity = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise WorkflowError("返回文件不是有效的 JSON。请重新下载 Claude 返回文件。") from error
        if not self._identity_matches(identity, request):
            raise WorkflowError(
                "所选文件不属于当前批次或当前任务。请检查 batch_id、request_id、output_file，"
                f"并选择当前任务的 {request['expected_result_filename']}。"
            )
        models = {"CONTENT": BatchContentResult, "FACT_CHECK": BatchFactCheckResult, "REVISION": BatchRevisionResult}
        model = models.get(request["request_type"])
        if model is None:
            raise WorkflowError("未知的 Claude 任务类型。请检查 batch status。")
        payload = model.model_validate_json(raw)
        document = json.loads((self.room.root / request["request_path"]).read_bytes())
        expected = {item["article_id"] for item in document["items"]}
        if expected != {item.article_id for item in payload.items}:
            raise WorkflowError("返回文件的文章集合与当前任务不一致。请让 Claude 返回全部指定文章且移除额外文章。")

    def _register_images(self, batch_id: str) -> None:
        active = self.room.db.all(
            "SELECT a.*,bi.item_status FROM batch_items bi LEFT JOIN articles a ON a.article_id=bi.article_id "
            "WHERE bi.batch_id=? AND bi.item_status NOT IN ('EXCLUDED','REUSED_COMPLETE') ORDER BY bi.candidate_id",
            (batch_id,),
        )
        missing = [article for article in active if not article["image_id"] and not self.room.no_image_selected(article)]
        if missing and len(missing) != len(active):
            raise WorkflowError(
                "当前批次部分有效文章已登记图片，部分尚未登记，属于不一致的旧版或部分完成状态。"
                "请从备份恢复，或通过迁移／管理员工具修复后再登记；此操作不会修改图片选择草稿。"
            )
        if active and not missing:
            self.dialogs.alert("素材选择已完成", "此批次已完成图片或明确无图选择。请继续整批审核。")
            return
        if not self.dialogs.confirm("整批图片选择", "每篇可选择图片或明确选择无图。有图时需检查原始落地页、权利页与本地原图，并明确确认使用权利。"):
            return
        if not active or any(article["fact_check_outcome"] != "PASS" for article in active):
            raise WorkflowError("整批图片登记需要全部有效文章通过事实核验。请先完成核验或修订。")
        path = self.workspace.image_draft_path(batch_id)
        plan = self.release.load_image_draft(batch_id) if path.exists() else BatchImagePlan(batch_id=batch_id, items=[])
        missing_ids = {article["article_id"] for article in missing}
        if {item.article_id for item in plan.items} - missing_ids:
            raise WorkflowError("图片选择草稿包含当前无需登记的文章。请检查 image-selections.json 后重试。")
        selected_ids = {item.article_id for item in plan.items}
        for article in missing:
            if article["article_id"] in selected_ids:
                continue
            candidates = self.room.list_image_candidates(article["article_id"])
            choices = {
                f"{candidate['image_candidate_id']} · {candidate['relevance']}": candidate
                for candidate in candidates if candidate["review_status"] != "SELECTED"
            }
            no_image = '无图发布（不使用图片）'
            selected = self.dialogs.choose_list("选择图片或无图", f"{article['title_cn'] or article['article_id']}：请选择候选或无图。", [*choices, no_image])
            if selected is None:
                return
            if selected == no_image:
                if not self.dialogs.confirm('确认无图', f"确认文章 {article['article_id']} 无图发布？此选择不代表缺失图片已获授权。"):
                    return
                item = NoImageSelection(article_id=article['article_id'], no_image=True, reason='编辑明确选择无图发布')
                plan = BatchImagePlan(batch_id=batch_id, items=[*plan.items, item])
                self.release.save_image_draft(batch_id, plan)
                continue
            if selected not in choices:
                raise WorkflowError("所选图片候选不在此文章列表中。请重新选择。")
            candidate = choices[selected]
            self.dialogs.open_url(candidate["landing_page_url"])
            self.dialogs.open_url(candidate["rights_url"])
            source = self.dialogs.choose_file("选择本地原图", "请人工下载并检查原图后选择本地文件。")
            if source is None:
                return
            source = Path(source).expanduser().resolve()
            if not source.is_file():
                raise WorkflowError("所选图片不是可读取的本地文件。请重新选择已下载原图。")
            if not self.dialogs.confirm(
                "确认图片权利",
                f"请确认作者、许可、署名与权利页一致，并已检查图片画面和分辨率。\n"
                f"作者：{candidate['creator']}\n许可：{candidate['license_name'] or candidate['rights_basis']}\n"
                f"权利声明：{candidate['rights_statement']}\n署名：{candidate['attribution_text'] or '无需署名'}\n"
                f"权利页：{candidate['rights_url']}\n原图：{source}",
            ):
                return
            item = BatchImageSelection(
                article_id=article["article_id"], image_candidate_id=candidate["image_candidate_id"],
                image_file=source, rights_confirmed=True,
            )
            plan = BatchImagePlan(batch_id=batch_id, items=[*plan.items, item])
            self.release.save_image_draft(batch_id, plan)
        if {item.article_id for item in plan.items} == missing_ids:
            self.release.apply_image_plan(batch_id, path)
            self._show_status(self.workspace.refresh(batch_id))

    def _release_action(self, batch_id: str, action: str) -> None:
        messages = {
            "export": "确认已检查整批当前正文、研究简报、核验报告和图片使用权利，并批准导出？将一次完成审核、批准、导出和刷新。获得正文与已登记图片即完成本地任务；验证成功后删除本批次过程文件，保留 SQL 去重记录。",
            "review": "确认已检查整批当前文章、研究简报、事实核验报告、图片与权利页面？",
            "approve": "确认批准整个批次的当前版本与发布素材？",
            "build-packages": "确认生成整批发布包？请完成人工审核和批准；发布包仍需人工上传。",
        }
        if not self.dialogs.confirm("确认批次操作", messages[action]):
            return
        if action == "export":
            path = self.release.export(batch_id, "editor", "编辑已检查整批当前版本、核验报告和图片权利，确认审核并批准导出")
            self.dialogs.open_file(path)
            self.dialogs.alert("导出完成", f"导出完成：正文与已登记图片已保存，过程文件已清理，本地内容任务完成。\n目录：{path}\n上传平台和 URL 登记可选。")
            return
        if action == "review":
            self.release.review(batch_id, "editor", "编辑已确认整批文章、核验报告、图片与权利页面")
        elif action == "approve":
            self.release.approve(batch_id, "editor", "编辑已确认整批当前版本与发布素材并批准")
        else:
            self.dialogs.open_file(self.release.build_packages(batch_id))
        self._show_status(self.workspace.refresh(batch_id))

    @staticmethod
    def _error_message(error: WorkflowError | ValidationError | OSError) -> str:
        if isinstance(error, ValidationError):
            return f"返回文件字段或格式不符合要求。请按当前任务的 instructions 重新生成完整 JSON。\n{error}"
        if isinstance(error, OSError):
            return f"无法读取文件或完成 macOS 操作。请检查文件路径、权限及本地文件后重试。\n{error}"
        return f"{error}\n请查看批次 README 与 batch status，完成当前步骤后重试。"


class MacOSMigrationController:
    """A narrow human-operated migration dialog; all dynamic data stays in argv."""

    def __init__(self, room: Newsroom, dialogs: DialogPort):
        self.room = room
        self.dialogs = dialogs

    def run(self) -> int:
        from .migration import WorkspaceMigrator
        service = WorkspaceMigrator(self.room)
        try:
            plan = service.preflight()
            report = service.save_report(plan)
            self.dialogs.open_file(report)
            if plan.blocking_errors:
                raise WorkflowError("迁移预检存在阻塞问题，请查看已保存报告并修复后重试。\n" + "\n".join(plan.blocking_errors))
            if not self.dialogs.confirm("迁移新闻编辑室", f"请先阅读预检报告：{report}\n确认创建验证备份并迁移本地工作区？不会发布文章。"):
                return 0
            result = service.execute_from_current_state() if service.receipt_path.exists() else service.execute(plan, service.backup(plan))
            self.dialogs.alert("迁移完成", f"{result['status']}\n清单 SHA-256：{result['manifest_sha256']}")
            return 0
        except Exception as error:
            try:
                self.dialogs.alert("迁移未完成", f"{error}\n请查看预检报告和迁移 journal；恢复完成前不要重复迁移。", error=True)
            except OSError as dialog_error:
                print(f"迁移未完成：{error}\n无法显示对话框：{dialog_error}", file=sys.stderr)
            return 2
