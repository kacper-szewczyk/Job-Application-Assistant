# 0002 — CLI-first review surface

- **Status:** Accepted; the UI reconsidered in T18 was built, see ADR 0022
- **Date:** 2026-10-14
- **Issue:** #1

## Context
SPEC §11 asked whether to start with CLI-only workflows or a minimal local web UI for previews and review.

## Decision
Start with a Typer/Rich CLI for fact review, answer confirmation and application tracking. Generated PDFs open in the system viewer. A local web UI is reconsidered in T18 (#19) if the CLI proves insufficient.

## Consequences
- Smallest surface to build and test; the same core services back the CLI and MCP server.
- Side-by-side PDF/fact review is less convenient until a UI exists.
