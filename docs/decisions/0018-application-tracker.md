# 0018 — Assisted-apply handoff and application tracker

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #16

## Context
SPEC §4 (steps 7–8) and §6 end the MVP at the handoff: jaa opens the employer's application page and hands over the prepared CV and answer notes; the user completes and submits the form, then marks the outcome. jaa must not claim a submission unless the user confirms it or a permitted integration returns a verifiable receipt, and it must warn about duplicate preparation or submission by canonical URL, source id or company + role. ADR 0005 made `jobs.status` the single lifecycle and left the transition rules to this task; `applications` (one per job), `application_answers` and `document_artifacts.application_id` exist since T02.

## Decision
- **Transition rules** (`jaa.core.applications.check_transition`, pure):
  - `submitted` is never a plain status change; it is recorded only through `record_submission`.
  - Once a submission is recorded, the job cannot go back to `discovered`, `saved`, `preparing` or `ready_for_review`.
  - `interviewing` and `rejected` need a recorded submission. `withdrawn` and `closed` are always allowed.
  - `preparing` and `ready_for_review` need an application record, so that starting one checked for duplicates first.
- **Duplicates.** Another job conflicts when it has an application record or a status past `saved`, and has the same canonical URL (ADR 0014) or the same employer and role after folding (`company_key`, `title_key`; location is ignored, unlike the ADR 0014 dedupe key, because this is a warning). The same source and source id is the same `jobs` row, which can have only one application. Pairs the user judged `different_jobs` are left out. Starting or submitting is refused while conflicts exist (`DuplicateApplicationError`) unless the user overrides; the override is noted in the status history with the other job ids.
- **Handoff** (`jaa.storage.applications.handoff`): the link to apply at (the listing's `source_url`, else the canonical URL), the newest CV PDF and cover letter PDF built for the job with whether each file still exists and matches its stored hash, the classified answers from `prepare_answers` (ADR 0017), conflicts, and warning codes (not started, no CV, missing/changed document, questions needing input, possible duplicate, no longer listed, already submitted). It changes nothing.
- **Submission** (`record_submission`) accepts only `SubmissionConfirmation.USER`. No authorized submission integration exists, so an `integration_receipt` cannot be verified and is refused; a future integration must be source-specific and employer-authorized (SPEC §6) and gets its own ADR. In one transaction it stores the submission time (not in the future), the link, an optional employer reference, the documents (by default the newest existing CV and cover letter; chosen ids must belong to the job) via `document_artifacts.application_id`, the answer versions offered for filling or review via `application_answers` (questions the user had to answer on the form are not linked), and a `handoff` snapshot: question keys with class, action and provenance ids, document ids and overridden duplicates. Values stay in `answers` and `profile_facts`. The job moves to `submitted` with actor `user`.
- **Storage.** Migration 0011 adds `applications.source_url` and `applications.handoff` and a trigger that rejects changes to the submission columns once `submitted_at` is set. Deleting an application stays possible (privacy controls, T19). `application_answers` has no insert guard because importing an export inserts links after their application.
- `jaa.storage.jobs.change_job_status` changes status inside the caller's transaction; `set_job_status` wraps it. Both remain the only way to change status.
- **CLI** `jaa apply start|handoff|submitted|status|list|show`. `handoff --open` opens only http(s) links in the user's browser. `submitted` asks "Did you submit … yourself?" every time and has no flag to skip the question, so neither a script nor Claude can record a submission without the user answering. `jaa jobs show` mentions the application; `jaa cv tailor propose` lists possible duplicate applications.
- Logs carry job, application, document and answer ids and counts only; notes, references, links and answers are not logged.

## Consequences
- The application record is durable, append-only in its history and fixed once submitted, and it says what was sent without copying documents or values.
- The MCP server (T16) must keep the user's confirmation step for submissions; it cannot offer a tool that records one on its own.
- Company + role conflicts can be false positives for genuinely different openings at the same employer; the user resolves them with a duplicate verdict or `--allow-duplicate`.
