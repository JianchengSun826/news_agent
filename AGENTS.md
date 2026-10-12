# Repository Guidelines

## Scope and Phase Discipline

Read `docs/WORKFLOW.md` and `docs/ROADMAP.md` before changing behavior. Identify the target phase and do not implement later-phase automation early. Phase 1 is local, human-driven, and must not require paid APIs. Deterministic code handles state and validation; humans handle search, editorial judgment, model prompts, final approval, and publishing.

## Project Structure

- `src/newsroom/`: CLI, SQLite persistence, workflow rules, deduplication, and package generation.
- `docs/`: business rules, architecture, policies, data model, assumptions, and roadmap.
- `prompts/`: versioned, provider-neutral prompt templates; never embed prompts in Python.
- `config/`: local newsroom, taxonomy, and source-registry defaults.
- `workspace/`: active batch materials and final articles/images.
- `data/`: SQLite state, backups, and migration recovery records.

## Development Commands

Use `uv` for the local environment:

- `uv sync --locked`: install runtime dependencies.
- `uv run newsroom --help`: inspect CLI commands.
- `uv run newsroom --root . init`: initialize the SQLite database.

## Engineering Conventions

Target Python 3.12 or newer. Use Pydantic v2 models at input boundaries and Typer for CLI commands. Use four-space indentation, type hints on public functions, `snake_case` for functions and modules, and `UPPER_CASE` for workflow states. Keep SQL parameterized. Use UTC ISO-8601 timestamps. IDs and slugs must be lowercase ASCII with hyphens.

Prefer small standard-library modules and explicit data flow. Do not add network calls, model SDKs, schedulers, background workers, or web frameworks during Phase 1.

## Verification and Data Changes

The user explicitly requested a runtime-only repository without test files, fixtures, demos, or test dependencies. Verify changes with CLI smoke checks and non-mutating state/material validation; do not reintroduce test artifacts without a new request. Any schema change must update `docs/DATA_MODEL.md` and initialization SQL. Any workflow or phase change must update `docs/WORKFLOW.md` or `docs/ROADMAP.md`.

## Git and Review

Use focused, imperative commits such as `feat: add approval guard`. Pull requests must state the phase, summarize behavior, list verification commands, and include sample output when artifacts change. Never commit credentials, private source text, local databases, or generated publication packages.
