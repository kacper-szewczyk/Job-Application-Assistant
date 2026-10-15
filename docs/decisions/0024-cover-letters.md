# 0024 — Cover letter PDFs

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #53

## Context
ADR 0016 stores cover letter paragraphs with a job's tailoring plan and checks them like rewrites: each must cite confirmed facts that back its wording. They were shown but not built into a document. A cover letter also has text that is not in the paragraphs: a sender, a recipient, a date, and usually a greeting and a closing. SPEC §6 forbids inventing facts, and a generated greeting can invent a person ("Dear Ms Nowak"). The CV pipeline (ADR 0012) already compiles moderncv templates safely and checks the PDF's text.

## Decision
- **Template kinds.** `template.json` gains an optional `kind`: `cv` (the default, so `moderncv-classic@1` is unchanged) or `cover_letter`. A letter template has the slots `babel`, `header`, `letter` and `body`. `get_template(id, kind)` only returns a template of the asked kind, and each builder refuses the other kind. `jaa cv templates` lists both kinds.
- **`moderncv-letter@1`.** It uses moderncv's letter layout in the `classic` style and blue, to match `moderncv-classic`. It keeps the same ATS choices: no hyphenation and no icon glyphs.
- **Where each piece of text comes from.**
  - Sender: the name and contact details (`identity.name`, `contact.*`) of the revision's profile version. The CV headline and summary are not used.
  - Recipient: the job's employer and location, unless the user gives `--recipient` lines (name first, then the address).
  - Date: the day of the build, in the letter's language (`15 October 2026`, `15 października 2026`). It is written into the `.tex`, so a rebuild of the source keeps it.
  - Opening and closing: only from the user (`--opening`, `--closing`). Without them the lines stay empty; jaa never adds "Dear Hiring Manager" or "Kind regards".
  - Body: the plan's paragraphs, in order, unchanged.
  
  All of it goes through `escape()`.
- **Building.** `jaa cv tailor letter JOB [--revision N]` builds the letter of a revision (the latest by default). It refuses when the plan has no paragraphs or any paragraph is blocked. Blocked CV edits do not stop it, since edits only change the CV. `--language en|pl` sets babel and the date. The pure parts live in `jaa.documents.letter`, the compile step is shared with CVs in `jaa.documents.pipeline`, and `jaa.storage.documents.build_cover_letter` picks the facts and records the result.
- **Records.** A build records `COVER_LETTER_PDF` and `COVER_LETTER_SOURCE` artifacts, with the job id and the revision's profile version. Files are named `cover-letter-<time>-job<id>-<template>-<lang>`. The manifest has the shared fields (template id, version and sha256, engine, language, paths, hashes, pages, text check) plus:
  - `job_id`, `profile_version_id`, `letter_date`;
  - `fact_ids`: the sender's facts and every cited fact;
  - whether an opening or closing was given (not their text);
  - `tailoring`: revision, method, whether the recipient came from the job or the user, and each paragraph's number, fact ids and approval.
- **Verification.** As for CVs, the PDF's text is extracted and checked for every piece of letter text. A letter longer than one page gets a layout warning.
- **Claude** may run `jaa cv tailor letter`. It builds a document from checked text and records no decision. The skill tells Claude to ask the user for the recipient, opening and closing and to pass their words only. No MCP tool is added yet.
- Logs carry the job id, paragraph count, page count and missing-text count, never letter text.

## Consequences
- The handoff (ADR 0018) and the review UI (ADR 0022) already list `COVER_LETTER_PDF` artifacts linked to a job, so built letters appear there without changes.
- A letter with no opening or closing has an empty line in its place. The user adds their own words when building, not afterwards in the PDF.
- Further letter layouts are new template versions or new templates (#40, #41).
