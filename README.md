# Job Application Assistant (`jaa`)

A local-first assistant that helps one person maintain a verified candidate profile, generate tailored CVs, discover jobs from permitted sources, and track applications. All data stays on your machine.

- Product spec: [`SPEC.md`](SPEC.md)
- Contributor and coding guide: [`CLAUDE.md`](CLAUDE.md)
- Decisions: [`docs/decisions/`](docs/decisions/)

> Status: early development. See the [issues](https://github.com/kacper-szewczyk/Job-Application-Assistant/issues) for the roadmap.

## Requirements

- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 automatically)

## Setup

```bash
uv sync
uv run jaa --help
uv run jaa info   # shows the data directory and database path
uv run jaa init   # creates the data directory and database (owner-only permissions)
uv run jaa backup ~/jaa-backup.sqlite3   # consistent SQLite copy
uv run jaa export ~/jaa-export.json      # JSON export, see docs/export-format.md
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `JAA_DATA_DIR` | platform data dir, e.g. `~/Library/Application Support/jaa` on macOS | Where the database, imported sources and generated documents live. Must be an absolute path. |
| `JAA_LOG_LEVEL` | `WARNING` | Log level. Logs are written to stderr and scrubbed of personal data and secrets. |

## Development

```bash
uv run ruff format .
uv run ruff check --fix .
uv run pyright
uv run pytest
```

Branch per issue, Conventional Commit PR titles, squash merge. Releases and `CHANGELOG.md` are produced by release-please. See [`CLAUDE.md`](CLAUDE.md).
