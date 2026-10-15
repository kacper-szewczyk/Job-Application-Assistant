# 0019 — MCP server for core operations

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #17

## Context
SPEC §7 gives Claude Code an MCP server for explicit operations (`import_profile`, `review_facts`, `search_jobs`, `get_job`, `draft_tailored_cv`, `generate_cv_pdf`, `list_applications`, `update_application_status`) while the core library owns logic and persistence. Issue #17 asks to separate read tools from mutating ones, require confirmation for sensitive or external actions, and avoid leaking sensitive data in tool results. The product rules say that only the user's review confirms a fact and that only the user, or a verifiable integration, records a submission (ADR 0006, ADR 0018). A tool that takes `confirm=true` as an argument would let the model confirm on the user's behalf, so it does not count as the user confirming.

## Decision
- **SDK.** The official `mcp` Python SDK, version 2.x (`MCPServer`, the former FastMCP). Tools return pydantic models (`jaa.mcp.models`), so each has an input and an output schema. `jaa mcp serve` runs the server over stdio; `.mcp.json` registers it for this repository.
- **Thin layer.** `jaa.mcp.server` calls the same services as the CLI. To share them, the CV builds moved to `jaa.storage.documents.build_profile_cv` / `build_tailored_cv` and import storage to `jaa.storage.profile.store_import`, and both the CLI and the MCP server use them. Every tool call opens its own connection to the database. Domain errors become tool errors with their message. Any other exception is logged by type only, because its message may quote personal values.
- **Tools and their checkpoints.**

  | Tool | Kind | Checkpoint |
  |---|---|---|
  | `review_facts`, `search_jobs`, `get_job`, `get_tailored_cv`, `list_applications` | read only | none needed |
  | `import_profile` | adds | facts are stored `unreviewed`; the review is the checkpoint. LinkedIn sources are never deleted from here. |
  | `draft_tailored_cv` | adds | a new append-only revision; the evidence checks block unbacked wording (ADR 0016). Blocked edits can be approved only with `jaa cv tailor approve` in the terminal. |
  | `generate_cv_pdf` | adds | uses only confirmed facts and refuses blocked plans; writes only into the data directory's `documents` folder. |
  | `decide_facts` | confirms | the user confirms in a dialog. |
  | `update_application_status` | confirms | the user confirms in a dialog; for `submitted` the dialog asks "Did you submit … yourself?". |

  Read tools are annotated `readOnlyHint`, confirming tools `destructiveHint`, and all are `openWorldHint: false`: no tool touches the network or opens links.
- **Confirmation is elicitation.** The confirming tools get the user's answer through MCP elicitation (`Resolve` + `Elicit`). The client shows the dialog to the user, and the answer is not a tool argument, so the model cannot supply it. The resolver checks the request first (fact ids, transition rules, duplicate applications), so a request that would fail is refused before the user is asked. Declining, cancelling or leaving the box unticked changes nothing. A client without elicitation support gets a protocol error, so the operation cannot happen without a dialog; the user can still use the CLI. Submissions keep `SubmissionConfirmation.USER` and the rules of ADR 0018.
- **Two tools beyond SPEC §7.** `decide_facts` separates the review decision (mutating, confirmed) from `review_facts` (read only), as the issue asks. `get_tailored_cv` shows a plan without creating a revision, since `draft_tailored_cv` always appends one.
- **Data in results.** Values of facts marked sensitive are left out of `review_facts`. Application notes, employer references and saved answers are not returned. CV text appears where the task needs it (non-sensitive fact values, tailoring plans). The confirmation dialog for facts shows the values being decided, because the user cannot confirm what they cannot see, and the dialog goes to the user. Text missing from a built PDF is reported as a count.
- **Logging.** The SDK installs a Rich handler on the root logger; `serve` replaces it with jaa's scrubbing handler on stderr, so stdout carries only the protocol. Tools log ids and counts.

## Consequences
- Claude Code can run the whole workflow, but cannot confirm facts or record a submission without the user's own answer.
- Clients without elicitation can read and prepare, but cannot confirm. Once they implement elicitation they get the confirming tools with no change to the server.
- The T17 skill (#18) describes when to call each tool and relies on these checkpoints.
