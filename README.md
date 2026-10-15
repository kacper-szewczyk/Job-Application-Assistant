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
- For PDF CVs: [Tectonic](https://tectonic-typesetting.github.io/) (`brew install tectonic` on macOS), or a TeX distribution with `xelatex` or `lualatex`

### Setup

```bash
uv sync
uv run jaa --help
uv run jaa info   # shows the data directory and database path
uv run jaa init   # creates the data directory and database (owner-only permissions)
uv run jaa backup ~/jaa-backup.sqlite3   # consistent SQLite copy
uv run jaa export ~/jaa-export.json      # JSON export, see docs/export-format.md
uv run jaa restore ~/jaa-backup.sqlite3  # load a backup or export back (see "Your data")
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

### Building a CV

```bash
uv run jaa cv templates                  # shipped LaTeX templates
uv run jaa cv preview --language pl      # what the CV will contain, no PDF
uv run jaa cv build                      # English CV PDF from confirmed facts
uv run jaa cv build -l pl --photo me.jpg --consent-file rodo.txt --open
uv run jaa cv list                       # built CVs with template and profile version
```

The CV uses only confirmed facts. Before a build, jaa lists unreviewed facts and lets you confirm some of them (pass `--confirmed-only` to skip this); the rest are left out. A photo and a consent clause (such as the Polish RODO clause) are added only when you supply them. Each PDF is saved with its LaTeX source and a manifest that records the template version, profile version and facts used. jaa also checks that the PDF's text contains every fact, so applicant tracking systems can read it. See [ADR 0012](docs/decisions/0012-cv-pdf-pipeline.md).

### Job sources

```bash
uv run jaa sources list                  # available sources and whether each is active
uv run jaa sources show <key>            # access method, allowed hosts, terms, rate limits, recent fetches
uv run jaa sources pause <key> --reason "terms changed"
uv run jaa sources resume <key>

uv run jaa jobs fetch solid_jobs IT      # every SOLID.Jobs IT offer; also Engineering, HR, …
uv run jaa jobs follow https://job-boards.greenhouse.io/acme   # follow a company's board (also Lever, Teamtailor, Personio)
uv run jaa jobs fetch --followed         # fetch every board you follow; `jaa jobs following` lists them
uv run jaa jobs add https://jobs.lever.co/acme/<id>   # one offer from such a board, read through its API
uv run jaa jobs add --url <link> --title "Data Engineer" --company "Example" \
  --location Kraków --description-file offer.txt   # an offer you copied by hand; `-` reads stdin
uv run jaa jobs list -q python --location krakow   # search; also --company, --source, --status, --include-removed
uv run jaa jobs list -q python --save python-krk   # …and save the search
uv run jaa jobs searches run python-krk  # rerun it; jobs found since the last run are marked new
uv run jaa jobs searches list            # saved searches and when each was last refreshed
uv run jaa jobs show <id>                # one job, its requirements, source link(s) and possible duplicates
uv run jaa jobs duplicates list          # jobs that may be the same job, e.g. on two boards
uv run jaa jobs duplicates mark <flag> same|different
uv run jaa jobs match <id>               # how a job fits your confirmed profile: evidence, gaps, uncertainty
uv run jaa jobs match --all              # match every listed job and rank them
uv run jaa jobs matches                  # latest ranking; outdated matches are marked
```

The first source is [SOLID.Jobs](https://solid.jobs/api-ofert-pracy), through its public API for aggregators. Fetching again reports new, changed and removed offers; a removed offer keeps your status for it. Connectors only use documented public APIs and feeds, within the rate limits in their source policy. If a source refuses access (login wall, bot check, HTTP 401/403), jaa pauses it instead of retrying. Company career boards on Greenhouse, Lever, Teamtailor and Personio are read through their public APIs and feeds: follow the companies you care about, or add one offer by its link ([ADR 0025](docs/decisions/0025-company-career-boards.md)). justjoin.it, bulldogjob.pl and nofluffjobs.com offer no permitted API, so jaa does not read them: copy an offer's text and add it with `jaa jobs add`. It is stored with source `manual` and matched, tailored and tracked like a fetched job; jaa records its link but never opens it ([ADR 0023](docs/decisions/0023-manual-jobs.md)).

Search matches whole words and ignores case, Polish diacritics and punctuation, so `-q lodz` finds Łódź. A job seen again is stored once. Jobs with the same link (ignoring tracking parameters), or with the same employer, title and location, are flagged as possible duplicates and never merged: you decide with `jaa jobs duplicates mark`, and a job marked the same as another shows both source links. A flag on a job you are already applying to is called out. See [docs/connectors.md](docs/connectors.md), [ADR 0011](docs/decisions/0011-connector-contract.md) and [ADR 0013](docs/decisions/0013-solid-jobs-and-listing-storage.md) and [ADR 0014](docs/decisions/0014-dedupe-and-search.md).

Matching reads a job's requirements from the skills and languages the source lists and from the requirements and nice-to-have sections of its description (English and Polish headings). Each requirement is backed by your confirmed facts, shown as backed only by unreviewed facts (which do not count until you confirm them), or listed as a gap. Seniority, work arrangement, location, salary and target roles are compared with your preferences when both sides are known. Every match records the profile version it used, explains its score and says what it could not judge. Scores are advisory. See [ADR 0015](docs/decisions/0015-requirements-and-match-explanations.md).

### Tailoring a CV for a job

```bash
uv run jaa cv tailor propose <job>       # reorder and emphasize what the job's match found
uv run jaa cv tailor show <job> --full   # every change vs. your baseline CV, its facts and status
uv run jaa cv tailor edit <job>          # hide, move, emphasize, reword; cover letter paragraphs
uv run jaa cv tailor edit <job> -f plan.json   # …or save a plan file (`show --json` prints one)
uv run jaa cv tailor approve <job> --edit 3    # approve an edit whose wording facts do not back
uv run jaa cv tailor build <job>         # the tailored PDF, linked to the job and profile version
uv run jaa cv tailor letter <job> --closing "Kind regards,"   # the cover letter PDF from its paragraphs
uv run jaa cv tailor list <job>          # revisions
```

Tailoring can only hide, reorder, emphasize (bold) and reword what your CV already says. Names, employers, titles, schools and dates cannot be edited. Every edit is linked to the confirmed facts behind it. A rewording, or a cover letter paragraph, is blocked when it adds numbers, names, technologies or job keywords that its target and cited facts do not contain; you can approve a blocked edit explicitly, and the approval is lost when the edit changes. Each change is a new revision checked against one profile version, and a build records the job, the profile version and the list of changes in its manifest. See [ADR 0016](docs/decisions/0016-tailored-cvs.md).

`jaa cv tailor letter` sets the plan's cover letter paragraphs as a letter (`moderncv-letter`, matching the CV), in English or Polish (`--language pl`). It builds only when every paragraph is backed or approved. The sender comes from your confirmed name and contact details, and the recipient is the job's employer and location unless you give `--recipient` lines. jaa adds no greeting or closing of its own: pass `--opening` and `--closing` in your own words, or leave them out. The PDF is linked to the job and profile version, and its manifest lists each paragraph's facts. See [ADR 0024](docs/decisions/0024-cover-letters.md).

### Application questions and saved answers

```bash
uv run jaa answers add-question <job> "Notice period?" --required   # copy a question from the form
uv run jaa answers prepare <job>         # how each question would be answered, and from where
uv run jaa answers review <job>          # answer what needs your input; answers are saved
uv run jaa answers list                  # saved answers, reuse policy and review date
uv run jaa answers show <key>            # every version of an answer and its sources
uv run jaa answers reconfirm <key>       # still right: start a new review period
uv run jaa answers policy <key> reuse    # reuse without asking (asks you first if sensitive)
uv run jaa answers forget <key>          # delete every version
```

Each question is answered from your confirmed profile (email, phone, links, name, location), from an answer you saved, or not at all. Preferences and unreviewed facts only make suggestions you have to confirm. Questions about work authorization, sponsorship, salary, demographics, criminal or medical history, consent and legal statements always need your answer, unless you set a saved answer to that exact question to `reuse`. Saved answers have a review date (180 days for sensitive ones, 365 for others); after it they must be confirmed again. Every answer records where it came from: the facts, preference or earlier answer you accepted, and the job it was given for. See [ADR 0017](docs/decisions/0017-answer-memory.md).

### Applying and tracking applications

```bash
uv run jaa apply start <job>             # check for duplicates, then move the job to preparing
uv run jaa apply handoff <job> --open    # link, CV/cover letter paths and answers; opens the page
uv run jaa apply submitted <job>         # after you submitted: asks you to confirm, then records it
uv run jaa apply status <job> interviewing --note "first call"   # any later status, with a note
uv run jaa apply list                    # your applications, most recently changed first
uv run jaa apply show <job>              # status history, submission, documents and answers used
```

Rejected, withdrawn and closed jobs are marked **archived** in `jaa apply list`, `jaa jobs list` and the MCP tools. They stay with their full history until you delete them.

jaa never submits an application. `handoff` shows everything to take to the employer's page and can open it in your browser; you fill in the form and submit it. Only `jaa apply submitted`, which asks you every time, marks an application submitted. It stores the time, the link, the documents and the saved answer versions used, and that record cannot be edited afterwards. Starting or submitting is refused while another prepared or submitted job has the same link or the same employer and role, until you mark the pair as different jobs or pass `--allow-duplicate`. See [ADR 0018](docs/decisions/0018-application-tracker.md).

### Your data: backups, restore and deletion

jaa deletes nothing on its own: archived jobs, every version of a saved answer and the history of your facts stay until you delete them. Everything you delete is gone from the database for good, so take a backup first if you might want it back.

```bash
uv run jaa backup ~/jaa-backup.sqlite3   # or: jaa export ~/jaa-export.json
uv run jaa restore ~/jaa-backup.sqlite3  # into an empty database; --force to replace your records
uv run jaa profile delete <fact>...      # delete facts, their history and their place in versions
uv run jaa import delete <run>           # delete an import and every fact it added
uv run jaa import delete-source <run>    # delete the file an import read, if it is unchanged
uv run jaa jobs delete <job>...          # delete jobs with their application, history and documents
uv run jaa answers forget <key>          # delete every version of a saved answer
```

Each delete command asks first (`--yes` skips the question). `restore` builds the restored database next to yours and swaps it in only once it is complete and checked; when it replaces records, a copy of your current database is saved in the `backups` folder of the data directory first. Stop `jaa mcp serve` before restoring. Generated documents are deleted with their job only when they are in the data directory and unchanged. See [ADR 0021](docs/decisions/0021-retention-and-deletion.md).

### Reviewing in the browser

```bash
uv run jaa ui               # prints a one-time link and opens it in your browser
uv run jaa ui --no-browser  # just print the link
```

`jaa ui` serves a review desk on this computer only (127.0.0.1, a free port): confirm, reject or correct facts and settle conflicts with their sources side by side, look at the tailored CV changes next to the built PDF, answer a job's questions (sensitive ones are never filled in for you), and see your applications by status with possible duplicates flagged. You still submit applications yourself; the UI records a submission only when you tick "I submitted this application myself". The link works once; press Ctrl+C to stop the server. The pages load nothing from the network and run no scripts. See [ADR 0022](docs/decisions/0022-local-review-ui.md).

### Using jaa from Claude Code (MCP)

jaa ships an MCP server, so Claude Code can call it with typed tools. Inside this repository the server is already registered in [`.mcp.json`](.mcp.json); Claude Code asks you to approve it the first time. Elsewhere, register it yourself:

```bash
claude mcp add --scope user jaa -- uv run --project /path/to/Job-Application-Assistant jaa mcp serve
```

| Tool | Effect |
|---|---|
| `review_facts`, `search_jobs`, `get_job`, `get_tailored_cv`, `list_applications` | Read only. |
| `import_profile` | Adds **unreviewed** facts from a CV, LinkedIn archive or pasted text. |
| `draft_tailored_cv` | Adds a tailoring revision; unbacked wording is blocked by the checks. |
| `generate_cv_pdf` | Builds a CV PDF from confirmed facts into the data directory. |
| `decide_facts` | Confirms, rejects or corrects facts: **asks you in a dialog first**. |
| `update_application_status` | Starts an application or changes its status, including recording your submission: **asks you in a dialog first**. |

The two confirmation dialogs are answered by you, not by Claude. A client that cannot show them gets an error, and nothing changes. Blocked tailoring edits can only be approved in the terminal (`jaa cv tailor approve`). Sensitive fact values are not returned to Claude. See [ADR 0019](docs/decisions/0019-mcp-server.md).

#### The skill

The [`job-application-assistant`](skills/job-application-assistant/SKILL.md) skill tells Claude how to run the workflows: onboarding, searching and matching jobs, tailoring a CV, preparing answers, and applying and tracking. It says when to call each tool, which CLI commands Claude may run, and where you decide. Inside this repository Claude Code loads it automatically; ask for help with your job search, or type `/job-application-assistant`. To use it elsewhere, link it into your user skills:

```bash
ln -s /path/to/Job-Application-Assistant/skills/job-application-assistant ~/.claude/skills/
```

Commands that record your own decisions (`jaa profile confirm`, `jaa onboard`, `jaa answers review`, `jaa cv tailor approve`, `jaa apply submitted`, …) are yours to run in your terminal. In this repository, [`.claude/settings.json`](.claude/settings.json) also denies them to Claude, and denies fetching linkedin.com. See [ADR 0020](docs/decisions/0020-claude-code-skill.md).

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `JAA_DATA_DIR` | platform data dir, e.g. `~/Library/Application Support/jaa` on macOS | Where the database, imported sources and generated documents live. Must be an absolute path. |
| `JAA_LATEX_ENGINE` | first of `tectonic`, `xelatex`, `lualatex` on `PATH` | LaTeX engine for CV PDFs: one of those names, or a path to the binary. |
| `JAA_LOG_LEVEL` | `WARNING` | Log level. Logs are written to stderr and scrubbed of personal data and secrets. |

## Architecture

```
Claude Code ── skill (workflows, checkpoints)
     │
     └── MCP server ─┐
                     ├── core services (profile, facts, jobs, answers, applications)
CLI (Typer + Rich) ──┤        │
Review UI (jaa ui) ──┘        │
                              ├── SQLite (numbered migrations, backup/export)
                              ├── importers   (PDF, DOCX, LaTeX, LinkedIn archive, text)
                              ├── connectors  (one per permitted job source)
                              └── documents   (content model → LaTeX → PDF + manifest)
```

The CLI, the MCP server and the review UI are thin layers over the same core services. Importers and connectors return data and never write to storage directly. The CLI covers every step; the local review UI adds side-by-side review (see [ADR 0022](docs/decisions/0022-local-review-ui.md)).

## Tech stack

Python 3.12+ managed with [uv](https://docs.astral.sh/uv/), Typer + Rich, Starlette + uvicorn + Jinja2 for the review UI, pydantic v2, stdlib `sqlite3`, the official `mcp` Python SDK, LaTeX for PDF output, and platformdirs for the data directory. Tooling: ruff, pyright (strict) and pytest. See [`docs/decisions/`](docs/decisions/) for the rationale.

## Privacy

- All data stays local, in the data directory described under [Configuration](#configuration) (see [ADR 0003](docs/decisions/0003-data-directory.md)).
- Logs record IDs and counts, never CV contents, profile values, answers or secrets. A scrubbing log formatter acts as a safety net.
- Nothing is deleted automatically. Backup, export, restore and delete commands cover source files, profile facts, answers, jobs and application records (see [Your data](#your-data-backups-restore-and-deletion)).
- Backups, exports and the database are written owner-only; backup and export refuse to write over the database.

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
