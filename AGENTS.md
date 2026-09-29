# Repository Guidelines

## Scope and Phase Discipline

Read `docs/WORKFLOW.md` and `docs/ROADMAP.md` before changing behavior. Identify the target phase and do not implement later-phase automation early. Phase 1 is local, human-driven, and must not require paid APIs. Deterministic code handles state and validation; humans handle search, editorial judgment, model prompts, final approval, and publishing.

## Project Structure

- `src/newsroom/`: CLI, SQLite persistence, workflow rules, deduplication, and package generation.
- `tests/`: pytest tests mirroring user-visible workflow behavior.
- `docs/`: business rules, architecture, policies, data model, assumptions, and roadmap.
- `prompts/`: versioned, provider-neutral prompt templates; never embed prompts in Python.
- `config/`: local newsroom, taxonomy, and source-registry defaults.
- `research/`, `articles/`, `images/`, `output/`: human-edited and generated workflow artifacts.
- `examples/`: safe sample inputs and demo materials.

## Development Commands

Use `uv` for the local environment:

- `uv sync --dev`: create the environment and install test tooling.
- `uv run newsroom --help`: inspect CLI commands.
- `uv run pytest`: run the complete suite.
- `uv run newsroom --root . init`: initialize the SQLite database.
- `bash scripts/demo.sh`: rebuild and execute the isolated demo workspace.

## Engineering Conventions

Target Python 3.12 or newer. Use Pydantic v2 models at input boundaries and Typer for CLI commands. Use four-space indentation, type hints on public functions, `snake_case` for functions and modules, and `UPPER_CASE` for workflow states. Keep SQL parameterized. Use UTC ISO-8601 timestamps. IDs and slugs must be lowercase ASCII with hyphens.

Prefer small standard-library modules and explicit data flow. Do not add network calls, model SDKs, schedulers, background workers, or web frameworks during Phase 1.

## Tests and Data Changes

Write a failing test before changing executable behavior. Test state transitions, approval gates, deduplication, validation, and generated artifacts using temporary directories and real SQLite databases. Any schema change must update `docs/DATA_MODEL.md`, initialization SQL, and tests. Any workflow or phase change must update `docs/WORKFLOW.md` or `docs/ROADMAP.md`.

## Git and Review

Use focused, imperative commits such as `feat: add approval guard`. Pull requests must state the phase, summarize behavior, list verification commands, and include sample output when artifacts change. Never commit credentials, private source text, local databases, or generated publication packages.
