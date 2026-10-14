# Local-first Job Application Assistant

**Status:** Initial product scope  
**Date:** 2026-10-12  
**Working name:** Job Application Assistant

## 1. Product summary

A local-first assistant that helps one person organize a job search, maintain a verified candidate profile, prepare role-specific CVs, and track applications. Claude Code is the first conversational interface; it should call predictable local tools for stateful operations rather than being treated as the database or the entire product.

The assistant can discover jobs from explicitly supported, permitted sources, explain role fit, prepare tailored materials, and open the employer’s application page for the user to complete and submit. It records application history locally so it can prevent duplicate work across sessions.

## 2. Goals

- Build a reusable candidate profile from user-provided CVs and a LinkedIn data export.
- Generate clean, reviewable, ATS-friendly PDF CVs from a maintained source/template.
- Find and deduplicate relevant job listings from a small set of approved sources.
- Tailor application materials using only candidate facts that have been confirmed by the user.
- Track jobs and application outcomes persistently on the user’s machine.
- Ask the user about preferences and missing application answers, then reuse approved answers with provenance.
- Keep the user in control of final application submission.

## 3. Non-goals for the MVP

- Scraping LinkedIn pages, using browser automation against LinkedIn, or bypassing access controls.
- Applying automatically across arbitrary websites or submitting applications without a final user review.
- A hosted multi-user service, shared account, or cloud CV repository.
- Guaranteeing ATS ranking or promising that a tailored CV will result in an interview.
- Reconstructing original LaTeX exactly from a PDF. PDF import is text extraction and profile reconstruction, followed by user review.
- Supporting every job board or ATS from day one.

## 4. Users and primary workflow

### Initial setup

1. Choose local data directory and create the local database.
2. Import one or more sources: PDF, DOCX, LaTeX/source files, or LinkedIn account data archive.
3. Parse into a candidate profile. Show source snippets and confidence for extracted fields.
4. Ask the user to confirm or correct important facts, including employment dates, titles, education, skills, location, work authorization, and contact details.
5. Interview for search and application preferences: target roles, seniority, locations, remote/hybrid/on-site, salary range/currency, employment types, industries, travel/relocation, sponsorship needs, start date, and answer reuse preferences.
6. Generate a baseline CV PDF and let the user review it.

### Job search and application preparation

1. Search configured sources for new or changed listings.
2. Normalize and deduplicate listings; retain canonical URL, source identifiers, and observed timestamps.
3. Rank and explain matches against user preferences and verified profile evidence.
4. User saves a job. Assistant extracts required and preferred qualifications, responsibilities, and application questions.
5. Assistant proposes a tailored CV and optionally a cover letter. It shows edits and evidence mapping; user reviews and approves the generated files.
6. For application questions, fill only confirmed/reusable answers. Ask about missing or ambiguous answers. Sensitive or legal attestations require explicit user input each time unless the user explicitly configures a safe reusable answer.
7. Open the official application page and provide the prepared files and answer notes. User completes any remaining fields and submits.
8. User marks submitted (or records another outcome); store timestamp, source URL, documents, and answer set used.

## 5. LinkedIn import scope

LinkedIn is an import source, not a page-scraping target.

### MVP behavior

- Provide instructions for downloading the user’s own LinkedIn data archive.
- Accept the archive or selected exported files through a local import command/UI.
- Inspect archive contents safely; support only documented/recognized formats and report skipped files.
- Map available fields into the candidate profile while preserving source filename, import time, and raw source value.
- Display a review-and-confirm step before any imported value becomes verified profile data.
- Allow the user to delete the imported archive after extraction; make the retention choice clear.
- If archive fields are insufficient, allow importing a user-provided profile PDF/document or pasting profile sections.

### Explicit limitations

- Do not fetch or scrape the supplied LinkedIn profile URL.
- Do not request LinkedIn passwords, session cookies, or browser tokens.
- Do not promise all profile sections will be present in an archive.
- OAuth/profile APIs are not an MVP dependency. LinkedIn’s generally available sign-in permissions expose limited profile fields; broader access is restricted. The EU/EEA/Switzerland Member Portability API may be explored later after eligibility, access, field coverage, and data handling are verified.

References: [LinkedIn data download instructions](https://www.linkedin.com/help/linkedin/answer/a1339364/downloading-your-account-data?lang=en), [LinkedIn prohibited software and extensions](https://www.linkedin.com/help/linkedin/answer/a1341387/prohibition-of-scraping-software?intendedLocale=en&lang=en-us), [LinkedIn API access](https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access), [Member Profile API restrictions](https://learn.microsoft.com/en-gb/linkedin/shared/integrations/people/profile-api?view=li-lms-2026-02).

## 6. Product requirements

### Candidate profile and source provenance

- Profile facts must carry a state: `unreviewed`, `confirmed`, or `rejected`.
- Track provenance per fact: source file/import, extraction timestamp, optional source excerpt/page, user correction, and last confirmation time.
- Never invent employers, titles, dates, credentials, quantified outcomes, skills, or eligibility answers.
- Generated documents may use confirmed facts by default. Any use of unreviewed facts must be surfaced for approval.
- Profile edits are versioned so generated application materials can refer to the profile version used.

### CV creation

- Treat structured profile data as canonical; LaTeX templates are render targets.
- Support import of existing LaTeX/source files as a starting template where practical.
- Support PDF and DOCX text extraction into candidate facts with review.
- Generate PDF and retain the source inputs, template version, profile version, and job-specific tailoring diff.
- Render/check layout and report compilation or extraction errors clearly.
- Tailoring must select/reorder/emphasize true experience; it must not add unsupported claims or materially alter dates/meaning.

### Job discovery and matching

- Start with a small, configurable list of sources and connectors.
- Prefer official feeds/APIs and employer career sites that permit programmatic access. Each connector documents access method, permitted fields, rate limits, and failure behavior.
- Do not work around login walls, CAPTCHAs, bot protections, or site restrictions.
- Normalize title, company, location, description, URL, posted/updated date, source ID, and questions where available.
- Dedupe using source ID and canonical URL, with conservative company/title/location matching as a fallback.
- Explain match scores with reasons and gaps; ranking is advisory.

### Application question handling

- Classify each question as profile-backed, reusable user answer, inferred suggestion, or unanswered.
- Show answer provenance and confidence. Inferences must be clearly labeled and never silently submitted.
- Require explicit user action for work authorization, sponsorship, salary expectations, demographic data, criminal/medical disclosures, consent, and other legal/sensitive questions.
- Allow saved answers with per-question reuse settings and a review/expiry date.

### Application tracking and memory

- Use durable local storage, not conversational memory, as the source of truth.
- Store job state: `discovered`, `saved`, `preparing`, `ready_for_review`, `submitted`, `interviewing`, `rejected`, `withdrawn`, or `closed`.
- Record status changes with timestamps and optional notes.
- Prevent duplicate preparation/submission warnings based on canonical URL/source ID/company+role.
- Store links and file paths for generated documents; do not duplicate sensitive documents unnecessarily.
- Provide search, export, backup, and delete controls.

### Human control and submission

- MVP ends with opening the employer application page and handing over prepared CV/answers.
- The assistant must not claim an application was submitted unless the user confirms it or a permitted integration returns a verifiable receipt.
- Any future direct submission integration must be source-specific, employer-authorized, and display the final payload for confirmation immediately before submission.
- Public listing APIs do not imply permission or credentials to submit applications. Greenhouse’s documented public GET endpoints require no authentication, while its application POST requires a board API key; that is not an applicant credential. ([Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html))

## 7. Suggested local architecture

Keep the first implementation simple and modular:

- **Core library:** profile, job, answer, document, and application domain logic.
- **Local persistence:** SQLite database plus a user-selected workspace for source files and generated artifacts. Use migrations and backup/export from the beginning.
- **Import adapters:** PDF, DOCX, LaTeX/source, LinkedIn archive, and pasted text. Extractors produce candidate facts plus provenance; they do not write confirmed values directly.
- **Job source connectors:** one adapter per approved source. Initial connector candidates: Greenhouse public job boards and employer-provided feeds; add others after checking source documentation and terms.
- **Document pipeline:** structured profile → tailored content model/diff → LaTeX template → PDF; retain a manifest for each output.
- **Claude Code interface:** a small skill for workflows and an MCP server for explicit operations such as `import_profile`, `review_facts`, `search_jobs`, `get_job`, `draft_tailored_cv`, `generate_cv_pdf`, `list_applications`, and `update_application_status`.
- **Review surface:** begin with CLI prompts or a minimal local UI for profile review, CV preview, and answer confirmation. A user must be able to inspect before an external action.

The skill should explain when to call tools and how to guide the user; the MCP server/core library should own deterministic operations and persistence. MCP is designed to connect AI clients to tools and local data sources. ([MCP overview](https://modelcontextprotocol.io/docs/2026-07-28/getting-started/intro), [Claude Code MCP documentation](https://docs.anthropic.com/en/docs/claude-code/mcp))

## 8. Initial data model

- `Profile`: identity/contact references, preferences, active profile version.
- `ProfileFact`: field key, normalized value, raw value, verification state, provenance, confidence, confirmed-at, sensitivity flag.
- `ProfileVersion`: immutable snapshot/version identifier and creation time.
- `Answer`: question key/text, answer value, source fact(s), reuse policy, sensitivity, confirmation/expiry timestamps.
- `Job`: source, source ID, canonical URL, employer, title, location, description, posted/seen timestamps, normalized requirements, status.
- `JobMatch`: job/profile version, score, evidence, gaps, generated timestamp.
- `Application`: job ID, state, status history, submitted-at, external receipt/reference if user provides it, notes.
- `DocumentArtifact`: path, type, job/application association, profile version, template version, content hash, created-at.
- `ImportRun`: source type, source path, imported-at, parse summary, errors, deletion/retention choice.

Sensitive values should be minimized, kept local by default, and excluded from logs. Provide a clear way to remove source files, profile facts, answers, and application records.

## 9. Acceptance criteria for the MVP

- A new user can import at least one CV source and a LinkedIn archive, review extracted facts, and save a confirmed profile.
- The user can create and inspect a baseline PDF CV from the confirmed profile.
- Re-running an import does not silently overwrite confirmed facts; conflicts are shown for review.
- Job discovery can ingest at least one permitted source, deduplicate listings, and show source links and match explanations.
- A user can create a tailored CV for a saved job, see all changes, and regenerate after edits.
- Unknown or sensitive application questions are surfaced for user input and are not filled from guesses.
- Application records persist across restarts, and duplicate jobs/applications are flagged.
- The user can export or back up the local tracker and delete stored data.
- No application is marked submitted without user confirmation or a verifiable authorized integration result.

## 10. Suggested implementation tasks

Tasks are grouped by dependency. A first vertical slice should deliver a verified profile, one generated PDF, and persistent tracking before adding broad job discovery.

### Foundation

1. **T01 — Define project skeleton and configuration**  
   Establish language/runtime, package structure, local data directory configuration, logging rules, and developer setup.  
   **Done when:** the app starts locally, identifies its data directory, and does not log CV contents or secrets.

2. **T02 — Define domain types and SQLite schema**  
   Implement profile facts, provenance, preferences, answers, jobs, applications, artifacts, import runs, migrations, and backup/export format.  
   **Depends on:** T01.

3. **T03 — Implement candidate profile review workflow**  
   Add fact confirmation/rejection/editing, conflict handling, profile versioning, and source provenance display.  
   **Depends on:** T02.

4. **T04 — Implement onboarding interview and preference storage**  
   Ask for search preferences and answer-reuse rules; allow skipping and later updating.  
   **Depends on:** T02, T03.

### Import and document generation

5. **T05 — Import PDF and DOCX CVs**  
   Extract text and candidate fact candidates with page/section references where possible; send all candidates to review.  
   **Depends on:** T03.

6. **T06 — Import LaTeX/source CVs**  
   Parse or preserve source structure, identify reusable sections, and map supported content into profile candidates.  
   **Depends on:** T03.

7. **T07 — Import LinkedIn data archive**  
   Add archive file selection, safe extraction, supported-file detection, mapping, review, and retention/deletion choices. Never fetch profile URLs.  
   **Depends on:** T03.

8. **T08 — Create baseline LaTeX CV templates and PDF pipeline**  
   Render confirmed profile data, retain template/profile version manifest, and provide preview/error feedback.  
   **Depends on:** T03, T06.

### Job discovery and preparation

9. **T09 — Define connector interface and source policy metadata**  
   Standardize listing/job/question output and document each source’s access and rate-limit requirements.  
   **Depends on:** T02.

10. **T10 — Add first permitted job source connector**  
    Prefer public employer/ATS job-board endpoints. Parse listings, store source IDs/URLs, and handle changes/errors.  
    **Depends on:** T09.

11. **T11 — Normalize, deduplicate, and search jobs**  
    Add query filters, canonicalization, duplicate warnings, saved searches, and refresh timestamps.  
    **Depends on:** T10.

12. **T12 — Implement job requirement extraction and match explanations**  
    Compare listings with confirmed facts/preferences; show evidence, gaps, and uncertainty.  
    **Depends on:** T03, T11.

13. **T13 — Create tailored CV review and generation**  
    Produce evidence-backed edits, a diff, per-job PDF, and artifact association. Block unsupported claims by default.  
    **Depends on:** T08, T12.

14. **T14 — Implement application question classification and answer memory**  
    Link known answers to confirmed profile facts; classify unknown/sensitive questions; apply reuse rules and expiry.  
    **Depends on:** T02, T03, T04.

15. **T15 — Build assisted-apply handoff and application tracker**  
    Show prepared materials/questions, open the employer page, and record status only after user confirmation.  
    **Depends on:** T11, T13, T14.

### Claude Code integration and usability

16. **T16 — Implement MCP server for core operations**  
    Expose narrow, typed tools for importing, searching, reviewing, generating, and tracking. Separate read tools from mutating tools and require confirmation for sensitive/external actions.  
    **Depends on:** T03, T08, T11, T13, T15.

17. **T17 — Add Claude Code skill/workflows**  
    Document onboarding, search, review, tailoring, and tracking workflows; point to MCP operations and required checkpoints.  
    **Depends on:** T16.

18. **T18 — Add local review UI or polished CLI**  
    Provide reliable CV preview, fact review, application question review, and local status dashboard. Choose the smallest interface that meets those needs.  
    **Depends on:** T03, T08, T14, T15.

19. **T19 — Add privacy controls and operational hardening**  
    Add data export/backup/delete, retention controls, safe archive extraction, path validation, and log scrubbing.  
    **Depends on:** T02, T07, T15.

20. **T20 — Evaluate additional sources and portability API feasibility**  
    Review permissions, terms, API eligibility, coverage, and maintenance cost before adding connectors or OAuth import.  
    **Depends on:** T07, T10.

## 11. Open decisions

- Preferred implementation language and packaging (Python CLI/MCP, TypeScript CLI/MCP, or another stack).
- Whether to start with CLI-only workflows or a minimal local web UI for previews and review.
- First job source(s) and target countries/languages.
- CV languages and initial LaTeX template style.
- Whether generated documents should live in the app data directory or a user-selected folder.
- How long rejected/closed jobs and answer history should be retained by default.

## 12. Risks and mitigations

- **Import errors create false CV facts:** preserve provenance and require review before confirmation.
- **Tailoring exaggerates experience:** evidence-link every proposed edit and reject unsupported claims by default.
- **Job sources change or restrict access:** isolate connectors; prefer documented APIs/feeds; pause a connector when policy/access is unclear.
- **Accidental duplicate or incorrect application:** deduplicate and make submission handoff explicit; require user confirmation of status.
- **Sensitive personal data exposure:** local-first storage, minimal logging, explicit retention controls, and no unnecessary cloud sync.
- **PDF layout or extraction failures:** keep source and rendered preview; report errors and allow edits/regeneration.
- **Unclear LinkedIn API eligibility:** ship archive import first and gate OAuth/API work behind an access and terms review.
