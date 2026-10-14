# 0007 — Onboarding preferences

- **Status:** Accepted
- **Date:** 2026-10-14
- **Issue:** #5

## Context
SPEC §4 step 5 asks for an interview covering search and application preferences (target roles, seniority, locations, work arrangement, salary, employment types, industries, travel/relocation, sponsorship, start date, answer reuse). T04 requires that every question can be skipped and answered later, that answers are durable and editable, and that sensitive preferences are flagged. T02 created a `preferences` table (one row per key, JSON value) with no history and no way to record a skip.

## Decision
- **A fixed question catalogue** (`jaa.core.preferences.QUESTIONS`) defines each preference key, its prompt, its kind (text list, choice, multi-choice, salary range, start date) and whether it is sensitive. Only catalogue keys can be stored, and values are checked against the question's shape, whether they come from typed text (`parse_answer`) or already structured (`check_value`, for MCP in T16).
- **Skips are stored** (migration 0003 adds `preferences.status = answered | skipped`; a skipped row holds `null`). `jaa onboard` asks every question that is not answered — never asked or skipped — so the user can stop at any point and resume. `jaa onboard --all` revisits everything; Enter keeps the current answer.
- **History is append-only.** Every answer, change and skip is appended to `preference_events` (updates are rejected by a trigger). Repeating the current value records nothing.
- **Sensitive preferences:** `salary.range` and `sponsorship.required` are flagged sensitive (the catalogue flag and `jaa.core.sensitivity` agree). They are masked in CLI listings and history unless `--reveal` is given and are never logged.
- **Answer reuse** is two preferences: `answer_reuse.default` (any `ReusePolicy`) and `answer_reuse.sensitive`, which only offers `always_ask` or `reuse_with_review`. A blanket "reuse sensitive answers without asking" is not offered, because SPEC §4 requires a safe reusable sensitive answer to be configured explicitly, per answer. When skipped, the defaults are `reuse_with_review` and `always_ask` (`jaa.core.preferences.reuse_policy`).
- **Sponsorship** is `yes | no | depends`; "depends" means the question is asked per application, since sponsorship needs often differ by country.

## Consequences
- Adding a preference means adding a catalogue entry (and tests); renaming a key needs a migration for existing rows and history.
- Matching (T12) and answer reuse (T15) read answered values via `jaa.storage.preferences.answered_values` and must treat missing keys as "no preference", never guess.
- Salary is one structured value (`min`, optional `max`, ISO-style currency code, period) rather than several keys, so it is answered, skipped and masked as a unit.
