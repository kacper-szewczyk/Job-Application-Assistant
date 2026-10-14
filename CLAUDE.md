# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## Project

Local-first Job Application Assistant. `SPEC.md` is the product spec; GitHub issues track implementation tasks (T01–T20 map to issues #2–#21, open decisions live in #1). Accepted decisions are in `docs/decisions/` — read the relevant ADR before changing anything it covers.

## Non-negotiable product rules

These come from `SPEC.md` and apply to every change:

- **Never invent candidate facts.** Employers, titles, dates, credentials, metrics, skills and eligibility answers come only from user-provided sources or user input.
- **Extractors produce candidates, not truth.** Importers write `unreviewed` facts with provenance; only the review workflow can mark a fact `confirmed`.
- **Re-imports never silently overwrite confirmed facts** — conflicts go to review.
- **No LinkedIn scraping.** Never fetch LinkedIn profile URLs, automate LinkedIn pages, or ask for LinkedIn passwords, cookies or tokens. LinkedIn data comes only from the user's data archive or pasted/uploaded documents.
- **No working around access controls** (login walls, CAPTCHAs, bot protection, ToS restrictions) in job connectors.
- **The user submits applications.** Never mark an application `submitted` without explicit user confirmation or a verifiable authorized integration receipt.
- **Sensitive questions** (work authorization, sponsorship, salary, demographics, criminal/medical, consent, legal attestations) are never answered from guesses.
- **Never log CV contents, profile values, answers or secrets.** Log IDs and counts, not values.

## Stack

- Python 3.12+, managed with **uv** (`uv sync`, `uv run …`, `uv add …`). Do not use pip/poetry directly.
- **ruff** for lint + format, **pyright** in strict mode for type checking, **pytest** for tests.
- **Typer + Rich** for the CLI; **pydantic v2** for models crossing boundaries (MCP tool I/O, config, import/export); plain dataclasses are fine for internal value objects.
- **SQLite** via stdlib `sqlite3` with numbered SQL migrations. No ORM.
- **MCP**: official `mcp` Python SDK.
- **PDF output**: LaTeX templates compiled locally (engine chosen in T08).
- **platformdirs** for the default data directory.

## Commands

```bash
uv sync                      # install deps
uv run ruff format .         # format
uv run ruff check --fix .    # lint
uv run pyright               # type check
uv run pytest                # tests
uv run jaa --help            # CLI
```

Before finishing any change, run format, lint, pyright and pytest; all must pass.

## Folder structure

```
src/jaa/
  config.py              # settings, data dir resolution (platformdirs + JAA_DATA_DIR override)
  logging.py             # logging setup + scrubbing
  core/                  # domain types and services (profile, facts, jobs, answers, applications)
  storage/               # sqlite connection, repositories, backup/export
    migrations/          # 0001_init.sql, 0002_….sql (shipped with the package)
  importers/             # pdf.py, docx.py, latex.py, linkedin.py, text.py
  connectors/            # base.py (interface + policy metadata), one module per job source
  documents/             # content model, tailoring diff, LaTeX render, PDF build, manifests
    templates/           # LaTeX CV templates (versioned)
  matching/              # requirement extraction, match scoring/explanations
  answers/               # question classification, answer memory
  mcp/                   # MCP server: thin layer over core services
  cli/                   # Typer app: thin layer over core services
tests/
  unit/                  # mirrors src/jaa/ layout
  integration/           # sqlite, pipeline, CLI/MCP end-to-end
  fixtures/              # synthetic CVs, archives, recorded HTTP responses
skills/                  # Claude Code skill(s) for the assistant workflows (T17)
docs/
  decisions/             # ADRs: NNNN-short-title.md
```

Layering rule: `cli/` and `mcp/` depend on `core/` services; `core/` never imports from `cli/`, `mcp/`, or concrete importers/connectors. Importers and connectors depend on `core/` types and return data — they do not write to storage directly.

## Coding standards

- Full type hints everywhere; pyright strict must pass. No `Any` without a comment explaining why.
- Small modules with explicit public functions; prefer pure functions for parsing, normalization, dedupe and matching.
- Use `pathlib.Path`, timezone-aware UTC `datetime`s, and explicit enums for states (`FactState`, `JobStatus`, …).
- Raise domain-specific exceptions from `jaa.core.errors`; CLI/MCP layers translate them into user-facing messages.
- Every state change that matters to the user (fact confirmation, job/application status) is recorded with a timestamp and is append-only/versioned.
- Validate and resolve every user-supplied path; extract archives with path-traversal and size checks.
- Comments explain *why*, not *what*. Docstrings on public functions and MCP tools.
- Keep dependencies minimal; justify new ones in the PR description.

## Testing

- Every behavior change ships with tests. Bug fixes start with a failing test.
- Tests never hit the network: connectors use recorded fixtures.
- **Fixtures are synthetic.** Never commit real CVs, LinkedIn archives, or personal data.
- Use a temp data dir per test (`tmp_path`); never touch the real user data dir.
- Include tests asserting that sensitive values do not appear in logs for code paths that handle them.

## Git workflow

- One branch per issue: `<type>/<issue>-<slug>`, e.g. `feat/12-dedupe-jobs`, `fix/30-pdf-encoding`.
- **Conventional Commits** for commit messages and PR titles: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `ci:`, `build:`; `!` or `BREAKING CHANGE:` for breaking changes. Optional scope = module, e.g. `feat(importers): parse DOCX CVs`.
- PRs reference the issue (`Closes #12`) and are **squash-merged**; the PR title becomes the commit on `main`, so it must be a valid Conventional Commit.
- CI (GitHub Actions) runs ruff, pyright and pytest on every PR.
- Releases are automated with **release-please**: it reads Conventional Commits on `main`, maintains a release PR with the version bump and `CHANGELOG.md`, and creates the tagged GitHub release with release notes when merged. Do not edit `CHANGELOG.md` or version numbers by hand.
- Significant technical or product decisions get an ADR in `docs/decisions/`.
