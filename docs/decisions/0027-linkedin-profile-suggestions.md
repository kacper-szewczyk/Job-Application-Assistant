# 0027 — LinkedIn profile suggestions

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #73 (from the T20 research, #21)

## Context
Users keep their LinkedIn profile next to their CV, and it drifts: positions and skills on the CV are missing on LinkedIn, titles and dates are out of date, and the jobs the user targets ask for things LinkedIn does not show. LinkedIn has no write API for members, and jaa never automates LinkedIn pages, so jaa can only suggest; the user makes the changes.

To compare, jaa has to know what LinkedIn showed at the last import. Facts cannot tell it: `add_imported_facts` skips a candidate whose value is already stored for the same key (ADR 0006), so when the CV and LinkedIn agree, the LinkedIn import adds no fact and `profile_facts.import_run_id` names the CV import.

## Decision
- **Migration 0014 adds `import_run_facts`** (import run, fact): every fact an import's candidates matched, added or skipped. `add_imported_facts` writes it for every import from now on; `jaa.storage.profile.run_fact_ids` reads it and falls back to the facts a run added for runs stored before the migration. Rows go with their run or fact (`ON DELETE CASCADE`) and are part of the export.
- **What LinkedIn shows** is every fact the latest non-failed LinkedIn import (archive or API) saw, in any review state: LinkedIn shows a value whether or not the user confirmed it. Contact details and sensitive facts are out of scope; the sections compared are headline, summary, experience, education, skills, certifications and languages.
- **Only confirmed facts back a suggestion.** `jaa.documents.linkedin_suggestions.suggest` (pure) yields:
  - `add`: a confirmed item LinkedIn does not show (a position, school, skill, certification, language, the headline or summary, or a position's missing description).
  - `update`: LinkedIn shows a different title, degree, dates, headline, summary, language level or certification. Entries are matched by key, then by folded employer or school name; values are compared by their words, and structured values only on the parts both sides have. Descriptions are free text and are only suggested when missing.
  - `check`: LinkedIn shows a value the user rejected during review, and nothing confirmed covers that item. jaa does not say what to do; the user decides.
- **The wording is the user's.** A suggestion's text is the confirmed facts' wording, copied or joined, never generated prose. `check_suggestion` re-checks every suggestion with the T13 claim rules (ADR 0016), treating every word as a claim: each must appear in a cited confirmed fact. A suggestion that fails is withheld and counted; tests assert none is.
- **Target jobs rank suggestions and show gaps.** By default the target jobs are those the user saved or is applying to (`saved` through `interviewing`); `--job`/`job_ids` names others. Each is matched afresh against the active profile version (ADR 0015), in memory, without storing a match. Suggestions whose facts meet a target job's requirement come first, required before preferred. A skill a target job asks for that a confirmed position or education entry mentions, but LinkedIn does not list, is suggested as a skill, named with the words the fact contains. Requirements no confirmed fact meets are listed as `gaps` and never turned into suggestions; the user adds a fact first if one is true.
- **Surfaces.** `jaa profile linkedin [--job ID…]` and the read-only MCP tool `suggest_linkedin_updates`. Both write nothing; Claude may run them (not user-only). The skill's workflow 8 tells Claude to offer only the cited wording, never claims from gaps, and never to open LinkedIn. Logs carry ids and counts only.

## Consequences
- LinkedIn imports made before this change still work, but a value that the CV had already stored is not known to have been on LinkedIn, so it may be suggested as missing. A fresh LinkedIn import fixes that.
- The comparison is only as current as the last import; the skill suggests a fresh archive when it is old.
- No UI screen yet; the issue made it optional.
