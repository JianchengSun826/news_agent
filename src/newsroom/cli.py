from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Callable, TypeVar

import typer
from pydantic import ValidationError

from .batch import BatchWorkflow
from .batch_release import BatchReleaseWorkflow
from .macos import MacOSBatchController, MacOSMigrationController, OsaScriptDialogs
from .migration import MigrationPlan, WorkspaceMigrator, assert_migration_access
from .service import Newsroom, WorkflowError
from .workspace import BatchWorkspace

T = TypeVar("T")

app = typer.Typer(help="Human-in-the-loop technology newsroom", no_args_is_help=True)
candidate_app = typer.Typer(help="Manage news candidates", no_args_is_help=True)
published_app = typer.Typer(help="Query published articles", no_args_is_help=True)
batch_app = typer.Typer(help="Process a reviewed CSV with manual Claude batch handoffs", no_args_is_help=True)
workspace_app = typer.Typer(help="Inspect local workspace migration", no_args_is_help=True)

app.add_typer(candidate_app, name="candidate")
app.add_typer(published_app, name="published")
app.add_typer(batch_app, name="batch")
app.add_typer(workspace_app, name="workspace")

CLAUDE_INSTRUCTION = "严格执行文件中的 instructions，完成全部条目，并生成其中指定的 output_file。"


@app.callback()
def root_callback(
    ctx: typer.Context,
    root: Annotated[Path, typer.Option(help="Newsroom workspace root")] = Path.cwd(),
) -> None:
    ctx.meta["migration_access_bypass"] = ctx.invoked_subcommand in {"workspace", "macos-migrate"}
    if not ctx.meta["migration_access_bypass"]:
        _run(lambda: assert_migration_access(root))
    ctx.obj = Newsroom(root)


def _room(ctx: typer.Context) -> Newsroom:
    room = ctx.ensure_object(Newsroom)
    if not ctx.meta.get("migration_access_bypass"):
        _run(lambda: assert_migration_access(room.root))
    return room


def _json(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _run(action: Callable[[], T]) -> T:
    try:
        return action()
    except (WorkflowError, ValidationError, ValueError, OSError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from error


def _with_claude_instruction(payload: dict) -> dict:
    return {**payload, "claude_instruction": CLAUDE_INSTRUCTION}


@app.command("init")
def initialize(ctx: typer.Context) -> None:
    """Initialize local directories and the SQLite database."""
    room = _room(ctx)
    _run(room.init)
    typer.echo(f"Initialized newsroom at {room.root}")


@workspace_app.command("migrate")
def workspace_migrate(
    ctx: typer.Context,
    preflight: Annotated[bool, typer.Option("--preflight", help="Inventory migration without changing workflow")] = False,
    source_csv: Annotated[list[Path] | None, typer.Option("--source-csv", help="Original CSV selected for hash verification")] = None,
    execute: Annotated[bool, typer.Option("--execute", help="Execute the exact saved plan and verified backup")] = False,
    plan_file: Annotated[Path | None, typer.Option("--plan", help="Saved preflight JSON report")] = None,
    backup_dir: Annotated[Path | None, typer.Option("--backup", help="Completed verified migration backup directory")] = None,
    backup_plan: Annotated[bool, typer.Option("--backup-plan", help="Back up an unchanged saved clean preflight plan")] = False,
) -> None:
    """Explicit local preflight, backup, and migration operations."""
    room = _room(ctx)
    service = WorkspaceMigrator(room)
    if sum((preflight, execute, backup_plan)) != 1 or (preflight and (plan_file or backup_dir)) or (not preflight and source_csv):
        typer.echo("error: choose --preflight, --backup-plan --plan PATH, or --execute --plan PATH --backup DIR", err=True)
        raise typer.Exit(code=2)
    if preflight:
        plan = _run(lambda: service.preflight(source_csv))
        report = _run(lambda: service.save_report(plan))
        _json({**plan.to_dict(), "report_path": report.relative_to(room.root).as_posix()})
        if plan.blocking_errors:
            raise typer.Exit(code=2)
        return
    if plan_file is None or (execute and backup_dir is None) or (backup_plan and backup_dir is not None):
        typer.echo("error: saved --plan and verified --backup are required for execution", err=True)
        raise typer.Exit(code=2)

    def load_plan() -> MigrationPlan:
        value = json.loads(plan_file.read_bytes())
        if isinstance(value, dict):
            value.pop("report_path", None)
        return MigrationPlan.from_dict(value)

    plan = _run(load_plan)
    try:
        if backup_plan:
            _json({"status": "BACKED_UP", "backup_path": str(service.backup(plan))})
        else:
            _json(service.execute(plan, backup_dir))
    except Exception as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from error


@candidate_app.command("add")
def candidate_add(
    ctx: typer.Context,
    candidate_title: Annotated[str, typer.Option("--candidate-title")],
    core_fact: Annotated[str, typer.Option("--core-fact")],
    event_date: Annotated[str, typer.Option("--event-date")],
    company: Annotated[str, typer.Option()],
    product: Annotated[str, typer.Option()],
    topic: Annotated[str, typer.Option()],
    subtopic: Annotated[str, typer.Option()],
    event_type: Annotated[str, typer.Option("--event-type")],
    primary_source: Annotated[str, typer.Option("--primary-source")],
    secondary_source: Annotated[list[str] | None, typer.Option("--secondary-source")] = None,
    keyword: Annotated[list[str] | None, typer.Option("--keyword")] = None,
    discovered_from: Annotated[str, typer.Option("--discovered-from")] = "manual",
    priority: Annotated[int, typer.Option(min=1, max=5)] = 3,
) -> None:
    """Add a manually discovered candidate."""
    result = _run(lambda: _room(ctx).add_candidate(candidate_title=candidate_title, core_fact=core_fact, event_date=event_date, company=company, product=product, topic=topic, subtopic=subtopic, event_type=event_type, keywords=keyword or [company, product], primary_source=primary_source, secondary_sources=secondary_source or [], discovered_from=discovered_from, priority=priority))
    _json(result)


@batch_app.command("prepare")
def batch_prepare(
    ctx: typer.Context,
    csv_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Validate a reviewed CSV and create one Claude content request."""
    result = _run(lambda: BatchWorkflow(_room(ctx)).prepare(csv_file))
    _json(_with_claude_instruction(result))


@batch_app.command("status")
def batch_status(
    ctx: typer.Context, batch_id: Annotated[str, typer.Argument(metavar="BATCH_ID")]
) -> None:
    """Show batch state and items requiring human action."""
    _json(_run(lambda: BatchWorkflow(_room(ctx)).status(batch_id)))


@batch_app.command("resolve")
def batch_resolve(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(metavar="BATCH_ID")],
    candidate_id: Annotated[str, typer.Argument(metavar="CANDIDATE_ID")],
    decision: Annotated[str, typer.Option()],
    actor: Annotated[str, typer.Option()],
    reason: Annotated[str, typer.Option()],
) -> None:
    """Include or exclude one candidate awaiting a duplicate decision."""
    result = _run(
        lambda: BatchWorkflow(_room(ctx)).resolve(
            batch_id, candidate_id, decision, actor, reason
        )
    )
    _json(result)


@batch_app.command("content-import")
def batch_content_import(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(metavar="BATCH_ID")],
    result_json: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Import the complete result from the first Claude conversation."""
    result = _run(lambda: BatchWorkflow(_room(ctx)).import_content(batch_id, result_json))
    _json(_with_claude_instruction(result))


@batch_app.command("fact-check-import")
def batch_fact_check_import(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(metavar="BATCH_ID")],
    result_json: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Import the result from the independent Claude fact-check conversation."""
    _json(_run(lambda: BatchWorkflow(_room(ctx)).import_fact_checks(batch_id, result_json)))


@batch_app.command("revision-request")
def batch_revision_request(
    ctx: typer.Context, batch_id: Annotated[str, typer.Argument(metavar="BATCH_ID")]
) -> None:
    """Create one Claude request for all articles that failed fact-checking."""
    path = _run(lambda: BatchWorkflow(_room(ctx)).create_revision_request(batch_id))
    _json(_with_claude_instruction({"batch_id": batch_id, "revision_request_path": str(path)}))


@batch_app.command("revision-import")
def batch_revision_import(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(metavar="BATCH_ID")],
    result_json: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Import all revisions and create the next independent fact-check request."""
    result = _run(lambda: BatchWorkflow(_room(ctx)).import_revisions(batch_id, result_json))
    _json(_with_claude_instruction(result))


@batch_app.command("image-plan")
def batch_image_plan(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(metavar="BATCH_ID")],
    plan_json: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Validate all confirmed image selections and register local files together."""
    _json(_run(lambda: BatchReleaseWorkflow(_room(ctx)).apply_image_plan(batch_id, plan_json)))


@batch_app.command("review")
def batch_review(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(metavar="BATCH_ID")],
    actor: Annotated[str, typer.Option()],
    reason: Annotated[str, typer.Option()],
) -> None:
    """Record human review for every active article in the batch."""
    _json(_run(lambda: BatchReleaseWorkflow(_room(ctx)).review(batch_id, actor, reason)))


@batch_app.command("approve")
def batch_approve(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(metavar="BATCH_ID")],
    actor: Annotated[str, typer.Option()],
    reason: Annotated[str, typer.Option()],
) -> None:
    """Record final human approval for every active article in the batch."""
    _json(_run(lambda: BatchReleaseWorkflow(_room(ctx)).approve(batch_id, actor, reason)))


@batch_app.command("publish-packages")
def batch_publish_packages(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(metavar="BATCH_ID")],
) -> None:
    """Build validated publication packages for the whole approved batch."""
    path = _run(lambda: BatchReleaseWorkflow(_room(ctx)).build_packages(batch_id))
    _json({"batch_id": batch_id, "path": str(path)})


@batch_app.command("publish-register")
def batch_publish_register(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(metavar="BATCH_ID")],
    result_json: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    actor: Annotated[str, typer.Option()],
) -> None:
    """Register the complete set of URLs after human CMS publication."""
    _json(_run(lambda: BatchReleaseWorkflow(_room(ctx)).register_publications(batch_id, result_json, actor)))


@batch_app.command("refresh-workspace")
def batch_refresh_workspace(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(metavar="BATCH_ID")],
) -> None:
    """Rebuild local workspace views from canonical SQLite state."""
    _json(_run(lambda: BatchWorkspace(_room(ctx)).refresh(batch_id)))


@candidate_app.command("list")
def candidate_list(ctx: typer.Context) -> None:
    """List candidates in editorial priority order."""
    _json(_run(_room(ctx).list_candidates))


@candidate_app.command("show")
def candidate_show(ctx: typer.Context, candidate_id: Annotated[str, typer.Argument(metavar="ID")]) -> None:
    """Show one candidate."""
    _json(_run(lambda: _room(ctx).get_candidate(candidate_id)))


@candidate_app.command("select")
def candidate_select(ctx: typer.Context, candidate_id: Annotated[str, typer.Argument(metavar="ID")], actor: Annotated[str, typer.Option()], reason: Annotated[str, typer.Option()]) -> None:
    """Pass the human editorial selection gate."""
    room = _room(ctx)
    _run(lambda: room.select_candidate(candidate_id, actor, reason))
    _json(room.get_candidate(candidate_id))


@candidate_app.command("reject")
def candidate_reject(ctx: typer.Context, candidate_id: Annotated[str, typer.Argument(metavar="ID")], actor: Annotated[str, typer.Option()], reason: Annotated[str, typer.Option()]) -> None:
    room = _room(ctx)
    _run(lambda: room.reject_candidate(candidate_id, actor, reason))
    _json(room.get_candidate(candidate_id))


@candidate_app.command("import")
def candidate_import(ctx: typer.Context, file: Path) -> None:
    ids = _run(lambda: _room(ctx).import_candidates(file))
    _json({"imported": len(ids), "candidate_ids": ids})


@candidate_app.command("export")
def candidate_export(ctx: typer.Context, file: Path) -> None:
    count = _run(lambda: _room(ctx).export_candidates(file))
    _json({"exported": count, "path": str(file)})


@app.command("duplicate-check")
def duplicate_check(ctx: typer.Context, candidate_id: Annotated[str, typer.Argument(metavar="ID")], override: Annotated[str | None, typer.Option()] = None, reason: Annotated[str | None, typer.Option()] = None, actor: Annotated[str, typer.Option()] = "editor") -> None:
    """Run deterministic event-level duplicate checking."""
    room = _room(ctx)
    result = _run(lambda: room.check_duplicate(candidate_id))
    if override:
        if not reason:
            raise typer.BadParameter("--reason is required with --override")
        _run(lambda: room.override_duplicate(candidate_id, override, reason, actor))
        result["override"] = override
    _json(result)


@published_app.command("list")
def published_list(ctx: typer.Context) -> None:
    _json(_run(lambda: _room(ctx).db.all("SELECT article_id,event_id,title_cn,published_at,article_url FROM articles WHERE status='PUBLISHED' ORDER BY published_at DESC")))


@app.command("stats")
def stats(ctx: typer.Context) -> None:
    _json(_run(_room(ctx).stats))


@app.command("macos-action", hidden=True)
def macos_action(
    ctx: typer.Context,
    batch_id: Annotated[str, typer.Argument(metavar="BATCH_ID")],
    action: Annotated[str, typer.Argument(metavar="ACTION")],
) -> None:
    """Dispatch the fixed local Finder actions."""
    code = MacOSBatchController(_room(ctx), OsaScriptDialogs()).run(batch_id, action)
    raise typer.Exit(code=code)


@app.command("macos-migrate", hidden=True)
def macos_migrate(ctx: typer.Context) -> None:
    """Run mandatory migration preflight and human confirmation."""
    raise typer.Exit(code=MacOSMigrationController(_room(ctx), OsaScriptDialogs()).run())


if __name__ == "__main__":
    app()
