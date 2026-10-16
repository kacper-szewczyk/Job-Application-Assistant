# 0032 — More LinkedIn profile sections

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #91
- **Amends:** ADR 0010 (imported archive files), ADR 0026 (requested API domains)

## Context
The LinkedIn archive importer (ADR 0010) read profile, contact, positions, education, skills, languages and certifications, and reported projects, honors, courses, publications, patents and volunteering as `section_not_imported`. The portability API import (ADR 0026) did not request those domains, so that jaa never downloads data it then drops. Users with side projects, papers, patents or volunteering had to retype them during review. The CV content model (`jaa.documents.content`) and the template contract (ADR 0029) have no slots for these sections.

## Decision
- **Archive files and API domains.** `Projects.csv` ↔ `PROJECTS`, `Volunteering.csv` ↔ `VOLUNTEERING_EXPERIENCES`, `Publications.csv` ↔ `PUBLICATIONS`, `Patents.csv` ↔ `PATENTS`, `Honors.csv` ↔ `HONORS`, `Courses.csv` ↔ `COURSES`. The API rows use the archive's column labels and go through the same parser (`parse_linkedin_tables`), so both sources produce the same keys and values. One domain per request, as before.
- **Entry sections, one fact per field** (like `experience.*`), keyed by the slug of the entry's name. A repeated name gets its start or issue date, then a counter, as for employers (ADR 0006: stable per-item keys):
  - `projects.<title>.{title,dates,url,description}` (required column: Title). An empty "Finished On" is *not* read as ongoing: LinkedIn leaves it empty for one-off projects too, and "Present" would be a claim the user did not make.
  - `volunteering.<organization>.{organization,role,dates,description}` (required: Company Name). An empty "Finished On" means ongoing, as for positions.
  - `publications.<title>.{title,publisher,date,url,description}` (required: Name).
  - `patents.<title>.{title,number,application_number,date,url,description}` (required: Title; the number comes from "Patent Number", or "Number" if LinkedIn renames it).
  - `honors.<title>.{title,issuer,date,description}` (required: Title).
  - `courses.<name>` = `{name, number}`, one fact per course like certifications (required: Name).
  `dates` has the CV importer's `{start, end, current}` shape; a single `date` (Issued On, Published On) is normalized to `YYYY-MM` or `YYYY`, and a date that does not parse is left out with `date_not_recognized`. Descriptions are split into lines with confidence 0.8; other fields 0.9. Everything is `unreviewed` with provenance `<file> row N` or `LinkedIn <DOMAIN> row N`.
- **Volunteering's "Cause" is not imported.** A cause such as politics, religion or human rights can reveal special-category data, and CVs do not need it. The warning names the column, never its values (data minimization, as for Profile.csv's address and birth date).
- **Profile-only for now.** The CV content model and templates do not render these sections; adding them means new contract slots in every shipped and uploaded template (ADR 0029), which is its own change. Confirmed facts in these sections show up in `jaa cv preview|build` as "Not shown by this template", tailoring cannot address them, and LinkedIn update suggestions (ADR 0027) still cover only the sections they did before. They are available to review, export and any later renderer.
- **Still not imported:** Organizations, Test Scores, Recommendations Received and Causes You Care About stay `section_not_imported`, and their API domains are not requested. The CV importer's "Projects", "Publications", … headings are still reported as not imported; parsing free-form CV sections is a separate problem.

## Consequences
- Re-imports follow ADR 0006: identical values are skipped and a different value for a confirmed key goes to review, never over the confirmed one.
- The fixtures follow the archive's column names; the API's labels for these domains are taken from the archive files (as in ADR 0026) and have not been checked against the live API. A renamed required column is reported as `unexpected_format`.
- A follow-up can add CV sections and template slots for projects, publications and volunteering, which are the ones most CVs carry.
