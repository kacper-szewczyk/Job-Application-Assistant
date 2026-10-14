# Job Application Assistant (`jaa`)

A local-first assistant that helps one person run their job search: keep a verified candidate profile, generate tailored, ATS-friendly PDF CVs, discover jobs from permitted sources, and track applications — with all data kept on your own machine.

Claude Code is the first conversational interface. It drives the assistant through a Claude Code skill and an MCP server, while a local core library and SQLite database own all state and deterministic operations. Conversation is never the source of truth.

> **Status:** early development. The project skeleton and configuration are in place; product features are being built. The product scope is defined in [`SPEC.md`](SPEC.md), and implementation is tracked in the [GitHub issues](https://github.com/kacper-szewczyk/Job-Application-Assistant/issues).

## What it does

- **Builds a candidate profile from your own documents.** Import CVs (PDF, DOCX, LaTeX/source), your LinkedIn data archive, or pasted text. Every extracted value starts as `unreviewed`, keeps its provenance (source file, excerpt, import time) and becomes `confirmed` only after you review it.
- **Generates CVs you can trust.** Structured profile data is canonical; LaTeX templates are render targets. Each PDF comes with a manifest of the template version, profile version and tailoring diff.
- **Tailors without inventing.** Per-job CVs select, reorder and emphasise true experience. Every proposed edit is linked to evidence, and unsupported claims are blocked by default.
- **Finds and ranks jobs from permitted sources.** Connectors use official feeds and public job-board APIs (e.g. Greenhouse public boards). Listings are normalised and de-duplicated, and match scores come with reasons and gaps.
- **Handles application questions carefully.** Answers are reused only where you approved reuse, with provenance shown. Sensitive questions (work authorisation, sponsorship, salary, demographics, legal attestations) always need your explicit input.
- **Tracks applications persistently.** Job and application states, status history, generated documents and answer sets are stored locally, with warnings for duplicate applications.
- **Leaves submission to you.** The assistant prepares materials and opens the employer's application page. You submit. Nothing is marked `submitted` without your confirmation.

## What it deliberately does not do

- Scrape LinkedIn, automate LinkedIn pages, or ask for LinkedIn passwords, cookies or tokens. LinkedIn data comes only from the archive you download yourself.
- Work around login walls, CAPTCHAs, bot protection or site terms.
- Submit applications automatically or on arbitrary websites.
- Run as a hosted or multi-user service, or sync your data to the cloud.
- Promise ATS rankings or interviews.

## Typical workflow

1. **Set up:** choose the data directory, import your CVs and LinkedIn archive, and review the extracted facts.
2. **Onboard:** answer a short interview about target roles, locations, remote preferences, salary range, sponsorship needs and answer-reuse rules.
3. **Baseline CV:** generate and review a PDF CV from your confirmed profile.
4. **Search:** pull listings from configured sources, then review ranked matches with their explanations.
5. **Prepare:** save a job, review the tailored CV diff and the question answers, and fill in anything missing.
6. **Hand off and track:** open the employer's page, submit it yourself, then record the outcome.

## Getting started

### Requirements

- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 automatically)

### Setup

```bash
uv sync
uv run jaa --help
uv run jaa info   # shows the data directory and database path
uv run jaa init   # creates the data directory and database (owner-only permissions)
uv run jaa backup ~/jaa-backup.sqlite3   # consistent SQLite copy
uv run jaa export ~/jaa-export.json      # JSON export, see docs/export-format.md
```

### Importing a CV

```bash
uv run jaa import cv ~/Documents/cv.pdf   # PDF or DOCX; facts go to the review queue
uv run jaa import cv ~/cv/main.tex --as-template  # LaTeX; also keep the source as a template
uv run jaa import linkedin                # how to download your LinkedIn data archive
uv run jaa import linkedin ~/Downloads/Basic_LinkedInDataExport.zip --delete-source
pbpaste | uv run jaa import text          # pasted profile sections (Experience, Skills, …)
uv run jaa import runs                    # past imports, status and issue counts
uv run jaa import show 3                  # everything that was not imported, and why
uv run jaa import templates               # LaTeX CVs kept as template candidates
```

Extraction is heuristic: every fact keeps its page/paragraph (or `.tex` file and line) and a source excerpt, and anything that could not be interpreted is listed as an issue instead of being dropped. Re-importing never changes confirmed facts. LaTeX imports follow `\input`/`\include` files in the CV's folder and understand moderncv, awesome-cv and simple `\newcommand`s; unknown commands are reported. See [ADR 0008](docs/decisions/0008-cv-file-import.md) and [ADR 0009](docs/decisions/0009-latex-cv-import.md).

LinkedIn archives are read locally and safely (nothing is extracted, unsafe archives are refused, messages and other non-profile files are never opened); jaa never signs in to LinkedIn or fetches profile URLs. See [ADR 0010](docs/decisions/0010-linkedin-archive-import.md).

### Reviewing your profile

Imported facts start as `unreviewed` and are only used once you confirm them.

```bash
uv run jaa profile facts --state unreviewed   # review queue (sensitive values hidden; --reveal)
uv run jaa profile show 12                    # value, confidence, source excerpt, history
uv run jaa profile confirm 12 13              # confirm; creates a new profile version
uv run jaa profile reject 14
uv run jaa profile edit 15 "Senior Engineer"  # correction; the original stays in history
uv run jaa profile conflicts                  # re-imported values next to confirmed ones
uv run jaa profile versions
```

### Search and application preferences

```bash
uv run jaa onboard                       # interview; press Enter to skip any question
uv run jaa onboard --all                 # revisit every answer (Enter keeps it)
uv run jaa preferences list              # answers, skips, unasked questions (--reveal)
uv run jaa preferences set salary.range "70000-90000 EUR per year"
uv run jaa preferences skip search.industries
uv run jaa preferences history search.target_roles
```

Salary and sponsorship answers are treated as sensitive: hidden in listings unless you pass `--reveal`, and never logged. See [ADR 0007](docs/decisions/0007-onboarding-preferences.md).

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `JAA_DATA_DIR` | platform data dir, e.g. `~/Library/Application Support/jaa` on macOS | Where the database, imported sources and generated documents live. Must be an absolute path. |
| `JAA_LOG_LEVEL` | `WARNING` | Log level. Logs are written to stderr and scrubbed of personal data and secrets. |

## Architecture

```
Claude Code ── skill (workflows, checkpoints)
     │
     └── MCP server ─┐
                     ├── core services (profile, facts, jobs, answers, applications)
CLI (Typer + Rich) ──┘        │
                              ├── SQLite (numbered migrations, backup/export)
                              ├── importers   (PDF, DOCX, LaTeX, LinkedIn archive, text)
                              ├── connectors  (one per permitted job source)
                              └── documents   (content model → LaTeX → PDF + manifest)
```

The CLI and the MCP server are thin layers over the same core services. Importers and connectors return data and never write to storage directly. Review happens in the CLI first; a local web UI may follow (see [ADR 0002](docs/decisions/0002-cli-first-review-surface.md)).

## Tech stack

Python 3.12+ managed with [uv](https://docs.astral.sh/uv/), Typer + Rich, pydantic v2, stdlib `sqlite3`, the official `mcp` Python SDK, LaTeX for PDF output, and platformdirs for the data directory. Tooling: ruff, pyright (strict) and pytest. See [`docs/decisions/`](docs/decisions/) for the rationale.

## Privacy

- All data stays local, in the data directory described under [Configuration](#configuration) (see [ADR 0003](docs/decisions/0003-data-directory.md)).
- Logs record IDs and counts, never CV contents, profile values, answers or secrets. A scrubbing log formatter acts as a safety net.
- Export, backup and delete controls cover source files, profile facts, answers and application records.

## Roadmap

Work is split into tasks T01–T20 (issues #2–#21), grouped as:

- **Foundation:** project skeleton, domain types and SQLite schema, profile review, onboarding.
- **Import and documents:** PDF/DOCX, LaTeX and LinkedIn archive importers; LaTeX CV templates and the PDF pipeline.
- **Jobs and applications:** connector interface, the first permitted source, dedupe and search, match explanations, tailored CVs, answer memory, and the assisted-apply tracker.
- **Claude and UX:** MCP server, Claude Code skill, review UI or polished CLI.
- **Privacy and research:** privacy hardening and evaluation of additional sources.

The first vertical slice (`mvp-slice` label) delivers a verified profile, one generated PDF and persistent tracking before broad job discovery. Open product decisions are tracked in [#1](https://github.com/kacper-szewczyk/Job-Application-Assistant/issues/1).

## Development

```bash
uv run ruff format .
uv run ruff check --fix .
uv run pyright
uv run pytest
```

Read [`CLAUDE.md`](CLAUDE.md) for the product rules, coding standards and git workflow. In short: one branch per issue, Conventional Commit PR titles, squash merges, and releases plus `CHANGELOG.md` produced by release-please. Fixtures must be synthetic. Never commit real CVs, LinkedIn archives or personal data.
