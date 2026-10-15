# 0022 — Local review UI

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #19 (design in `docs/ui/README.md`, accepted 2026-10-14)

## Context
T18 asks for reliable CV preview, fact review, application question review and a status dashboard, with nothing leaving the machine. ADR 0002 started CLI-first and left a local web UI to T18. The CLI covers every step one item at a time; it cannot show a fact's source next to a conflicting value, or a tailored CV's changes next to the PDF. The user accepted a UI design and stack (Starlette + uvicorn, Jinja2, vendored htmx) in `docs/ui/README.md`.

A web server adds risks the CLI does not have: other machines on the network, other local users and processes, web pages in the user's browser that post to `127.0.0.1` (CSRF) or rebind their own name to it (DNS rebinding), and request logs that record URLs.

## Decision
- **`jaa ui`** starts a server in `jaa.ui` (Starlette, served by uvicorn) and opens the browser. Pages are rendered with Jinja2 (autoescaping on) and call the same `storage`/`core` services as the CLI; `jaa.ui` is a thin layer like `cli/` and `mcp/`. Shared logic moved out of the CLI where the UI needed it (`jaa.storage.answers.accept_suggestion`).
- **Loopback only.** The server binds to a loopback IP literal (`127.0.0.1` default, `::1` allowed) on a free port; any other `--host`, including names such as `localhost`, is refused before binding. Requests whose `Host` is not that address or `localhost` with the same port are refused (DNS rebinding).
- **One-time link, then a session cookie.** `jaa ui` prints a link carrying a random token. Its first use sets an HttpOnly, `SameSite=Strict` cookie holding a separate random session id; a second use is refused. Every page except the login and the stylesheet needs the cookie. The secrets live in memory and die with the server.
- **Every change is a form POST with a CSRF token**, and a cross-origin `Origin` is refused. Handlers call one service function and redirect back with a message that names ids and counts only. Routes carry ids; values never appear in URLs.
- **No scripts, nothing from the network.** Pages are plain HTML forms; the CSP is `default-src 'none'` with same-origin styles and frames, `frame-ancestors 'self'`, plus `no-store`, `no-referrer` and `nosniff`. The prototype's Google Fonts were replaced by system fonts. htmx, part of the accepted design, was left out: post/redirect/get covers every screen, and no JavaScript keeps the CSP strict. It can be added (vendored) if a screen needs in-place updates.
- **Request lines are not logged** (uvicorn's access log is off; the first request carries the token). Application logging stays ids and counts.
- **Screens:** Overview (queues: unreviewed facts, conflicts, questions to answer, possible duplicates; applications by status), Profile review (facts by state with source excerpt and location, bulk confirm/reject, conflicts side by side with *Replace with this* / *Reject*, fact detail with history and correction), Jobs, Job (overview and match summary; tailored CV changes with cited facts next to the PDF; questions; apply checklist), Applications (grouped by status, archived separately, duplicate flags on every row) and Documents.
- **Product rules on these screens:**
  - Sensitive fact values, excerpts and answer suggestions are masked until the user asks to show them; a sensitive suggestion can be used only from the revealed page. Sensitive questions are never prefilled.
  - Conflicts are replaced only through the explicit *Replace* action (ADR 0006).
  - A submission is recorded only when the user ticks "I submitted this application myself" on the form (`SubmissionConfirmation.USER`, ADR 0018); `submitted` is never offered as a plain status. Start, status changes and duplicate verdicts go through the tracker services, actor `user`.
  - PDFs are served only for generated CV/cover-letter records whose file resolves inside the data directory, inline, with a CSP that allows only same-origin framing.
- **Claude Code** stays out of the UI. `jaa ui` is a user-only command, denied to Claude in `.claude/settings.json` and listed as such in the skill: Claude could otherwise start it, read the link and post decisions, bypassing the elicitation dialogs of ADR 0019. The tailored CV screen tells the user what to ask Claude Code by job id; the UI passes nothing to Claude itself.
- **Dependencies:** `starlette`, `uvicorn` and `python-multipart` (form parsing) were already installed through `mcp`; they are now direct dependencies. `jinja2` is new: templating with autoescaping that Starlette supports directly.

## Consequences
- Fact review, PDF preview, answer review and the status dashboard are available side by side, and nothing leaves the machine.
- Anyone who can read the user's terminal output before the user opens the link could take the session. The link is single-use and the server lives only as long as `jaa ui` runs.
- Not built yet: an Imports & sources screen, approving blocked tailoring edits, preferences and deletion. Those stay in the CLI.
- The UI renders values to the user's own browser only; `no-store` keeps them out of the browser cache.
