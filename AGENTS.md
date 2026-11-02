# AGENTS.md

Instructions for AI agents working in this repository. Read this file before
any change. Also read [docs/development.md](docs/development.md).

## Scope

This is a plain Python project. No build step, no frontend toolchain.
Services: `src/collector`, `src/web`, `src/db`, shared code in `src/common`.

## Documentation

Write documentation for every user-visible change.

- Update `README.md` for changes to quick start or basic commands.
- Update the matching page in `docs/` for deeper changes.
- Add a new page only when no existing page fits.
- Keep `docs/README.md` as the table of contents. Add every new page there.
- Document behavior, not implementation history. Do not write changelogs in
  prose.

Write easy to read documentation.

- Lead with the task the reader wants to do.
- Use short sentences and concrete commands.
- Use tables for fixed sets of keys, flags or files.
- Show a working example for every non-trivial setting.
- Prefer plain words over jargon. Expand an abbreviation on first use.
- State units and time zones. Stored timestamps are UTC.
- Keep one fact in one place. Link instead of repeating.

Example:

```sh
./scripts/logs.sh web
```

Not:

```text
Invoke the log aggregation helper with the web service selector argument.
```

## Code

Write simple code.

- Prefer the standard library. Add a dependency only when it removes real work.
- Prefer a plain function over a class when there is no state to hold.
- Keep functions short and single-purpose.
- Return early instead of nesting.
- Avoid clever expressions, metaprogramming and deep call chains.
- Handle errors at the boundary. Do not wrap every call in `try`.
- Reuse existing helpers before writing new ones. Check `src/common/` and the
  target package first.

Write easy to read code.

- Name things by what they mean in the domain: `checkpoint`, `snapshot`,
  `watermark`, `coverage`, not `data1`, `tmp`, `obj`.
- Name booleans after the condition: `is_backfill`, `has_gap`.
- Prefer relative imports inside a package: `from ..common import storage`.
  Use absolute imports from the package root only at entry points and in
  tests.
- Keep one responsibility per module. Match the module map in
  `docs/development.md`; update that map when you add or move a module.
- Docstrings state what a function does and its units or time zone. Do not
  restate the signature.
- Comments explain why, not what. Delete comments that repeat the code.
- Use f-strings for formatting. Exceptions: logging calls keep `%s`
  placeholders for lazy formatting, and URL templates loaded from config keep
  `str.format` because the placeholder names are part of the config contract.
- Follow the existing style: 4 spaces, double quotes, `snake_case`, module
  constants in `UPPER_CASE`.

Example:

```python
def checkpoint_at(timestamp: int) -> str:
    """Return the UTC date for a checkpoint timestamp in seconds."""
    return datetime.fromtimestamp(timestamp, UTC).date().isoformat()
```

Tests:

```sh
python -m unittest discover -s tests -t .
```

Add or update a test in the matching `tests/test_*.py` file. Database tests
need a disposable database and must never point at production data.

## Structure

Separate and organize stuff cleanly.

- Put basic, widely reused code first: `src/common/` holds config, DSN, storage
  and CLI helpers with no service-specific behavior.
- Put service-specific code deeper: `src/collector/`, `src/web/`, `src/db/`.
- Put entry points at the package edge: `__main__.py` and `main.py` only wire
  arguments to functions. Logic lives in a named module.
- Do not create a new package for one function. Do not grow a module past one
  clear responsibility.
- Keep generated output out of source trees. Reports go to `reports/`, backups
  to `backups/`, configuration to `config/`.
- Keep operational scripts in `scripts/`. Shared container setup goes in
  `scripts/internal/`; periodic jobs go in `scripts/maintenance/`.
- Keep container files in `docker/`.
- Do not import `src.web` from `src.collector` or the reverse. Share through
  `src/common/`.

Layout example:

```text
src/common/       reusable: config, storage, CLI
src/collector/    collection: api, grid, priority, run
src/web/          reporting: report, player_report, status_report
src/db/           schema and one-shot schema command
```

## Nomenclature

Use one term per concept, everywhere: code, docs, commit messages, output.

| Term | Meaning |
| --- | --- |
| `skill` | A tracked stat, such as `overall` or `attack`. |
| `checkpoint` | A recorded fetch attempt time, including unchanged values. |
| `snapshot` | A stored xp value, written only when the value changes. |
| `backfill` | Filling missing historical checkpoints. |
| `gap` | A checkpoint range that is missing or needs a retry. |
| `priority player` | A player tracked on the priority schedule. |
| `coverage` | Share of expected checkpoints that have data. |
| `report` | Generated text or HTML output under `reports/`. |

Rules:

- Use the table terms exactly. Do not mix `checkpoint` and `sample`, or
  `snapshot` and `record`, for the same thing.
- Use `player`, not `user`, for a tracked account.
- Use `xp`, not `exp` or `experience`.
- Use `web service`, `collector` and `db` for the three services.
- Match the spelling in `config/*.yaml` key names. Config keys are the source
  of truth.
- Rename consistently in one change. Leave no old term behind in code, docs,
  tests or output.

## Git

Work in development branches.

- Branch from `main`. Do not commit or push directly to `main`.
- Use a type prefix matching the change: `feature/`, `fix/`, `docs/`,
  `refactor/`, `chore/`. Example: `fix/coverage-bar-density`.
- Keep the branch focused on one topic. Rebase or merge `main` when it drifts.
- Merge through a pull request.
- Apply the branch and commit rules from now on. Do not rewrite existing
  history to comply.

Do not bundle unrelated changes into the same commit.

- One commit per logical change. A docs fix and a code fix are two commits.
- A commit must build and pass tests on its own.
- Prefix commits authored by an AI agent with `[AI]`.
  Example: `[AI] Clamp negative derived xp to zero`.
- Write the subject in the imperative, lower case, under 72 characters.
  Human-authored commits have no prefix.
- Use the body to state what changed and why, wrapped at 72 characters.
- Do not mix formatting-only churn with behavior changes.
- Stage explicitly. Do not use `git add .` in a mixed working tree.
- Never commit `.env`, credentials, report output or backups.

## Boundaries

- Do not change the database schema without an idempotent statement in
  `src/db/schema.py` and a note in `docs/development.md`.
- Do not add highscore API calls to the web service.
- Do not change report output formats without updating `docs/reports.md` and
  the tests that compare text and HTML.
- Do not rewrite user work you did not create. Inspect first, then edit.
