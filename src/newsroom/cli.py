from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Callable, TypeVar

import typer
from pydantic import ValidationError

from .service import Newsroom, WorkflowError

T = TypeVar("T")

app = typer.Typer(help="Human-in-the-loop technology newsroom", no_args_is_help=True)
candidate_app = typer.Typer(help="Manage news candidates", no_args_is_help=True)
brief_app = typer.Typer(help="Manage research briefs", no_args_is_help=True)
article_app = typer.Typer(help="Register article drafts", no_args_is_help=True)
fact_check_app = typer.Typer(help="Register manual fact checks", no_args_is_help=True)
image_app = typer.Typer(help="Register image rights and provenance", no_args_is_help=True)
published_app = typer.Typer(help="Query published articles", no_args_is_help=True)
prompt_app = typer.Typer(help="Create manual provider prompts", no_args_is_help=True)
research_app = typer.Typer(help="Register manual research", no_args_is_help=True)

app.add_typer(candidate_app, name="candidate")
app.add_typer(brief_app, name="brief")
app.add_typer(article_app, name="article")
app.add_typer(fact_check_app, name="fact-check")
app.add_typer(image_app, name="image")
app.add_typer(published_app, name="published")
app.add_typer(prompt_app, name="prompt")
app.add_typer(research_app, name="research")


@app.callback()
def root_callback(
    ctx: typer.Context,
    root: Annotated[Path, typer.Option(help="Newsroom workspace root")] = Path.cwd(),
) -> None:
    ctx.obj = Newsroom(root)


def _room(ctx: typer.Context) -> Newsroom:
    return ctx.ensure_object(Newsroom)


def _json(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _run(action: Callable[[], T]) -> T:
    try:
        return action()
    except (WorkflowError, ValidationError, ValueError, FileExistsError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(code=2) from error


@app.command("init")
def initialize(ctx: typer.Context) -> None:
    """Initialize local directories and the SQLite database."""
    room = _room(ctx)
    _run(room.init)
    typer.echo(f"Initialized newsroom at {room.root}")


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
    secondary_source: Annotated[list[str], typer.Option("--secondary-source")],
    keyword: Annotated[list[str] | None, typer.Option("--keyword")] = None,
    discovered_from: Annotated[str, typer.Option("--discovered-from")] = "manual",
    priority: Annotated[int, typer.Option(min=1, max=5)] = 3,
) -> None:
    """Add a manually discovered candidate."""
    result = _run(lambda: _room(ctx).add_candidate(candidate_title=candidate_title, core_fact=core_fact, event_date=event_date, company=company, product=product, topic=topic, subtopic=subtopic, event_type=event_type, keywords=keyword or [company, product], primary_source=primary_source, secondary_sources=secondary_source, discovered_from=discovered_from, priority=priority))
    _json(result)


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


@brief_app.command("create")
def brief_create(ctx: typer.Context, candidate_id: Annotated[str, typer.Argument(metavar="ID")]) -> None:
    _json(_run(lambda: _room(ctx).create_brief(candidate_id)))


@prompt_app.command("create")
def prompt_create(ctx: typer.Context, article_id: Annotated[str, typer.Argument(metavar="ID")], purpose: Annotated[str, typer.Option()]) -> None:
    _json({"path": str(_run(lambda: _room(ctx).create_prompt(article_id, purpose)))})


@research_app.command("register")
def research_register(ctx: typer.Context, article_id: Annotated[str, typer.Argument(metavar="ID")], file: Annotated[Path, typer.Option(exists=True, dir_okay=False)]) -> None:
    room = _room(ctx)
    _run(lambda: room.register_research(article_id, file))
    _json(room.get_article(article_id))


@article_app.command("register")
def article_register(ctx: typer.Context, article_id: Annotated[str, typer.Argument(metavar="ID")], file: Annotated[Path, typer.Option(exists=True, dir_okay=False)], title: Annotated[str, typer.Option()], slug: Annotated[str, typer.Option()]) -> None:
    room = _room(ctx)
    _run(lambda: room.register_article(article_id, file, title, slug))
    _json(room.get_article(article_id))


@fact_check_app.command("register")
def fact_check_register(ctx: typer.Context, article_id: Annotated[str, typer.Argument(metavar="ID")], file: Annotated[Path, typer.Option(exists=True, dir_okay=False)]) -> None:
    room = _room(ctx)
    _run(lambda: room.register_fact_check(article_id, file))
    _json(room.get_article(article_id))


@image_app.command("register")
def image_register(ctx: typer.Context, article_id: Annotated[str, typer.Argument(metavar="ID")], image_type: Annotated[str, typer.Option("--type")], source_url: Annotated[str | None, typer.Option()] = None, creator: Annotated[str | None, typer.Option()] = None, license_name: Annotated[str | None, typer.Option("--license")] = None, license_url: Annotated[str | None, typer.Option()] = None, attribution: Annotated[str | None, typer.Option()] = None, ai_provider: Annotated[str | None, typer.Option()] = None, ai_model: Annotated[str | None, typer.Option()] = None, prompt_path: Annotated[str | None, typer.Option()] = None) -> None:
    image_id = _run(lambda: _room(ctx).register_image(article_id, image_type=image_type, source_url=source_url, creator=creator, license=license_name, license_url=license_url, attribution_text=attribution, ai_provider=ai_provider, ai_model=ai_model, prompt_path=prompt_path))
    _json({"image_id": image_id})


@app.command("review")
def review(ctx: typer.Context, article_id: Annotated[str, typer.Argument(metavar="ID")], actor: Annotated[str, typer.Option()], reason: Annotated[str, typer.Option()]) -> None:
    room = _room(ctx)
    _run(lambda: room.complete_review(article_id, actor, reason))
    _json(room.get_article(article_id))


@app.command("approve")
def approve(ctx: typer.Context, article_id: Annotated[str, typer.Argument(metavar="ID")], actor: Annotated[str, typer.Option()], reason: Annotated[str, typer.Option()]) -> None:
    room = _room(ctx)
    _run(lambda: room.approve(article_id, actor, reason))
    _json(room.get_article(article_id))


@app.command("publish-package")
def publish_package(ctx: typer.Context, article_id: Annotated[str, typer.Argument(metavar="ID")]) -> None:
    _json({"path": str(_run(lambda: _room(ctx).create_publication_package(article_id)))})


@app.command("publish")
def publish(ctx: typer.Context, article_id: Annotated[str, typer.Argument(metavar="ID")], actor: Annotated[str, typer.Option()], url: Annotated[str | None, typer.Option()] = None) -> None:
    room = _room(ctx)
    _run(lambda: room.publish(article_id, actor, url))
    _json(room.get_article(article_id))


@published_app.command("list")
def published_list(ctx: typer.Context) -> None:
    _json(_run(lambda: _room(ctx).db.all("SELECT article_id,event_id,title_cn,published_at,article_url FROM articles WHERE status='PUBLISHED' ORDER BY published_at DESC")))


@app.command("stats")
def stats(ctx: typer.Context) -> None:
    _json(_run(_room(ctx).stats))


if __name__ == "__main__":
    app()
