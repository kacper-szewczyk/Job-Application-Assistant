# 0008 — PDF and DOCX CV import

- **Status:** Accepted
- **Date:** 2026-10-14
- **Issue:** #6

## Context
T05 imports PDF and DOCX CVs as candidate facts with provenance and confidence (SPEC §3, §6). CVs have no common structure, so extraction is heuristic and will be wrong sometimes. The spec's guard is review: nothing is confirmed by an importer. T05 also has to report extraction failures clearly and must not overwrite confirmed facts on re-import. ADR 0006 requires stable per-item field keys.

## Decision
- **Extraction libraries.** PDF text comes from `pypdf` (pure Python, typed, permissive licence, no system dependencies). DOCX is read with the standard library (`zipfile` + `xml.etree`) instead of `python-docx` + `lxml`, because we need only paragraph text, styles and numbering. Reading the parts directly also lets us enforce our own limits: each part is read straight into memory with a byte cap (so a ZIP bomb cannot exhaust memory, and nothing is extracted to disk), and parts with a DTD are rejected. There is no OCR. Scanned pages are reported as having no text.
- **The format comes from the content**, not the extension (`%PDF-`, or a ZIP containing `word/document.xml`). Legacy `.doc` files and other formats are refused with a message explaining what to do. Paths are resolved, must be regular files, and are capped at 25 MB. PDFs are capped at 30 pages.
- **Pipeline:** `extract_pdf`/`extract_docx` → located `SourceLine`s (`page N`, or `header`/`paragraph N` for DOCX, which has no fixed pages) → `jaa.importers.cv.parse_cv` (pure) → `FactCandidate`s plus `ImportIssue`s → `jaa.importers.cv_file.prepare_cv_import` builds the `ImportRun`. The CLI stores it with `add_imported_facts`, so importers never write to storage.
- **Sections** are recognized from an English heading vocabulary (plus DOCX heading styles). The CV language decision is still open in #1, and the vocabulary is a table, so adding languages is cheap. Supported: header/contact, summary, experience, education, skills, languages, certifications. Known but unsupported sections (projects, volunteering, …) and unknown styled headings are reported, so their text is not misread as part of the previous section.
- **Field keys** follow ADR 0006: `identity.name`, `contact.email|phone`, `contact.links.<linkedin|github|gitlab|website>`, `summary`, `skills.<slug>`, `languages.<slug>`, `certifications.<slug>`, and for entries `experience.<employer-slug>.{employer,title,dates,description}` and `education.<institution-slug>.{institution,degree,dates,details}`. A second entry for the same employer gets `-<start date>`. When the employer/title split is ambiguous, the entry gets a single low-confidence `….heading` fact with the whole heading, not a guessed split. Slugs keep symbols distinct (`c-plus-plus`, `c-sharp`, `node-dot-js`).
- **Confidence** is a fixed value per extraction rule, not a statistical score. Roughly: email 0.95, links 0.9, dates and phone 0.8, " at "-split titles 0.7, skills and languages 0.7, keyword-based splits, summaries and descriptions 0.6, the name 0.6, ambiguous headings 0.3. Its job is to order review attention.
- **Nothing is dropped silently.** Every line that does not become a fact is reported as an issue, with a location and a short quote: unreadable or empty pages (`error`), and unsupported sections, uninterpreted lines, extra heading parts and second values for a single-valued key (`warning`). Issues are stored in `ImportRun.errors` as `{code, severity, message, location}`. Run status: `failed` when no facts were extracted (including unreadable files, which still record a run), `partial` when there are issues, otherwise `succeeded`.
- **Re-import** relies on ADR 0006: identical values are skipped, and changed values become unreviewed conflicts. The CLI also notes when the same file (by SHA-256) was imported before.
- **Retention** is `keep`. The importer reads the user's file in place and keeps no copy. Deleting sources is T19's job.

## Consequences
- Two-column PDFs may interleave text. The user sees this as odd facts and "not imported" warnings during review, not as silent loss.
- Changing a slug rule or heading vocabulary changes keys for existing users' facts. Treat these rules like a schema: change them deliberately, with tests.
- T06 (LaTeX) and the pasted-text importer can reuse `parse_cv` by producing `SourceLine`s.
