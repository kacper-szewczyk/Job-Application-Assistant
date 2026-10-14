# Local review UI — design proposal (T18, #19)

Status: **design exploration**. Nothing here changes ADR 0002 yet; if we adopt it, an ADR amending 0002 comes with the first implementation PR.

Open [`prototype.html`](prototype.html) in a browser. It is a single static file with synthetic data (no server, no network beyond Google Fonts). Clicks update in-memory state only.

## What the UI is for

SPEC §6 asks for a review surface where the user inspects before anything external happens. The CLI covers this one fact or question at a time. The UI covers the cases where seeing things side by side matters:

| Need (SPEC / T18) | Screen | Why a UI helps over the CLI |
|---|---|---|
| Fact review with provenance | **Profile review** | Source snippet, extracted value and the confirmed value in a conflict are visible together; bulk-confirm by confidence |
| CV preview | **Job → Tailored CV** | Diff of the tailored CV next to the rendered page, every edit tied to a confirmed fact key |
| Application question review | **Job → Questions** | Reused answers show their source facts; sensitive questions are visibly locked to manual input |
| Local status dashboard | **Overview**, **Applications** | Pending decisions and pipeline counts on one screen; append-only status history |
| Import / source visibility | **Imports & sources** | Import runs, retention outcome, conflicts; connector policy and pause/resume (T09) |

## Product rules expressed in the design

- **Candidates vs. truth.** Facts carry a state chip (Unreviewed / Confirmed / Rejected / Conflict). Only confirmed facts are listed as evidence in match explanations and CV diffs; unreviewed evidence is called out ("confirm it before it counts").
- **Re-imports never overwrite.** A conflict shows *Confirmed now* vs. *New import* with both sources, and the only actions are *Keep confirmed* or *Replace with new* (replacement keeps history, per ADR 0006).
- **Sensitive questions.** Work authorization, sponsorship and salary get a distinct colour, never a pre-filled value, and "Save for future applications" is an explicit opt-in per question (ADR 0007's reuse policy).
- **The user submits.** The Apply tab is a handoff checklist. *Mark as submitted* is disabled until the user ticks "I submitted this application myself"; the change goes through `set_job_status` with actor `user` (ADR 0005).
- **No invented facts.** Match gaps stay gaps. The CV diff lists removed items and states that no new skills were added.
- **Local only.** The rail shows the bound address (127.0.0.1) and data directory. No values in URLs: routes are IDs only.

## Visual language

- Two-pane working layout: list on the left, provenance/detail panel on the right; collapses to one column under 900px.
- State colour is semantic and separate from the accent: amber = needs you, green = confirmed/done, red = rejected/conflict, plum = sensitive.
- Monospace for field keys, file names, line numbers and hashes, so provenance reads as data.
- Light and dark themes from the same token set.

## Proposed implementation (to decide before building)

Smallest thing that meets T18, consistent with the stack in CLAUDE.md:

- `jaa ui` starts a server bound to `127.0.0.1` on a random port, prints a URL with a one-time token, and opens the browser. Refuses non-loopback binds.
- Server-rendered HTML (Jinja2 templates) with a small amount of htmx for in-place updates; no JS build step, no Node toolchain. Handlers live in `src/jaa/ui/` and call the same `core/` and `storage/` services as the CLI (same layering rule as `cli/` and `mcp/`).
- Candidate dependencies (each needs a justification in the PR): `starlette` + `uvicorn` (or stdlib `http.server` if we want zero deps, at the cost of more hand-written routing), `jinja2`. htmx vendored as a static file.
- Mutations are POSTs with CSRF token; every status/fact change goes through the existing services so events stay append-only.
- Tests: render each page against a temp data dir, assert no personal values appear in logs, and assert the server refuses non-loopback hosts.

### Dependencies on other tasks

Overview, Profile review, Imports & sources and the sources table can be built now (T03, T05–T07, T09 exist). Tailored CV needs T08/T14, Questions needs T13/T14, Apply/Applications need T15. Those screens can ship behind "not available yet" states.

## Open questions for the user

1. Adopt a local web UI now (amend ADR 0002), or keep it as the T18 design and stay CLI-first until then?
2. Server stack: Starlette + Jinja2 + htmx, or stdlib-only?
3. Should the UI also start/launch Claude Code actions ("Edit in Claude Code"), or stay a review surface only?
