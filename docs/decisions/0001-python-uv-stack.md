# 0001 — Python + uv as the implementation stack

- **Status:** Accepted
- **Date:** 2026-10-14
- **Issue:** #1

## Context
SPEC §11 left the language and packaging open (Python vs TypeScript CLI/MCP). The product leans heavily on document parsing (PDF, DOCX, LaTeX), local SQLite, and an MCP server.

## Decision
Use Python 3.12+ managed with uv, packaged as `jaa` with a `src/` layout. Tooling: ruff (lint/format), pyright strict, pytest. Typer + Rich for the CLI, pydantic v2 at boundaries, stdlib sqlite3 with numbered SQL migrations, the official `mcp` Python SDK.

## Consequences
- Mature libraries for PDF/DOCX extraction and LaTeX tooling.
- One language for core, CLI and MCP server.
- Strict typing must be enforced in CI to keep the domain model reliable.
