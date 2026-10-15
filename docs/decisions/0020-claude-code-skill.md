# 0020 — Claude Code skill for the assistant workflows

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #18

## Context
SPEC §7 has Claude Code drive jaa through a skill and the MCP server (ADR 0019). The server covers importing, reviewing, tailoring, building PDFs and tracking, but not fetching jobs, matching, preparing answers or the handoff; those are CLI commands. Several CLI commands record the user's own decisions without a dialog (`jaa profile confirm`, `jaa apply status`) or read a yes from stdin (`jaa apply submitted`). Claude could run them through Bash and so skip the confirmation the MCP tools ask for.

## Decision
- **One skill**, `skills/job-application-assistant/SKILL.md`: the hard rules, a table of where the user decides, when to call each MCP tool, the CLI commands Claude may run, and five workflows (onboarding, search, tailoring, answers, applying and tracking). `.claude/skills/job-application-assistant` is a symlink to it, so Claude Code loads it inside the repository; elsewhere the user links it into `~/.claude/skills`.
- **CLI split.** Claude may run commands that only read or add records the user reviews later (`jaa jobs fetch`, `jaa jobs match`, `jaa answers prepare`, `jaa apply handoff`, …), never with `--reveal`. Commands that record the user's decisions (fact review, preferences, tailoring approval, answers, application status, duplicate verdicts, resuming a source) are the user's to run in their terminal. Status changes and submissions from Claude go through `update_application_status`, which asks the user in a dialog.
- **Backstop.** `.claude/settings.json` denies those user-only commands to Claude's Bash tool (with and without `uv run`) and denies WebFetch for linkedin.com. The skill's instructions are the main control; the deny rules catch mistakes. Tests check that every command the skill calls user-only has a deny rule, and that no command Claude may run is denied.
- **Tests keep the skill true.** The skill names every MCP tool the server exposes and only CLI commands that exist; an integration test runs the walkthrough (import → review → match → tailor → PDF → preparing → handoff → submitted) through the same tools and commands, with a stored listing in place of a network fetch.

## Consequences
- Adding an MCP tool or a confirming CLI command means updating the skill and, for the latter, `.claude/settings.json`; the tests fail until both are done.
- The deny rules apply to Claude in this repository only. Users running the skill elsewhere rely on the skill's instructions and the MCP dialogs.
- A real Claude Code session with elicitation dialogs is still the final check of the walkthrough; the integration test answers the dialogs programmatically.
