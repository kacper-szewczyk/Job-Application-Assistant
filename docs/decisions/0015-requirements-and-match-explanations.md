# 0015 — Job requirements and match explanations

- **Status:** Accepted
- **Date:** 2026-10-14
- **Issue:** #13

## Context
T12 compares a saved job with the confirmed profile and the preferences. The user must see the job's requirements, the facts that back each one, the gaps, and what the comparison could not judge. Every match must name the profile version it used, and unreviewed facts must not count as evidence without a label (SPEC §6, §8). Ranking is advisory. T10 left SOLID.Jobs skills, languages, experience level and salary out of storage; ADR 0014 left the decision on what to keep to T12.

## Decision
- **Structured job details.** `JobListing.details` is a `JobDetails` (`jaa.core.job_details`):
  - listed `skills` and `languages` with their level;
  - `experience_level`;
  - `work_arrangement` (`remote`, `hybrid` or `on_site`);
  - `salaries` (min, max, currency, period, contract).

  `ListingField.DETAILS` lets a policy permit it. SOLID.Jobs fills it; an office location with neither the remote nor the hybrid flag counts as on-site. Storage keeps the details, plus the application questions, as JSON in the existing `jobs.requirements` column.
- **Content hash.** A listing's details are part of its content hash only when it has some. Listings without details keep their old hash, so upgrading does not mark them changed. SOLID.Jobs listings change once, on the first fetch after upgrading, and that fetch stores their details.
- **Requirement extraction** (`jaa.matching.requirements`, pure):
  - Listed skills and languages are required requirements.
  - The description is split by headings, in English and Polish ("Requirements", "Wymagania", "Czego od Ciebie oczekujemy?", "Nice to have", "Mile widziane", "Obowiązki", "Oferujemy", …). Lines under a requirements heading are required, lines under a nice-to-have heading are preferred, and lines under a responsibilities heading are responsibilities. Other sections are ignored.
  - A heading is a short line that is not a bullet. It must be a known phrase, end with `:` or `?`, or add at most two words to a phrase of two or more words. This keeps lines like "Strong communication skills" as items.
  - When no requirements heading is recognized, no free-text requirements are taken. Guessing which lines are requirements would invent gaps.
- **Matching** (`jaa.matching.match`, pure) compares by wording:
  - A listed skill matches a skill or certification fact with the same words, or a mention in a headline, summary, job title, job description or degree.
  - A listed language matches a language fact. English and Polish names, including inflected forms such as "języka angielskiego", are understood. Levels are shown but not compared.
  - A description line matches when it names one of the profile's skills, certifications or languages.
  - Tokens keep `+`, `#` and inner dots, so `C++` is not `C`.

  Only the facts of the active profile version count. An unreviewed fact that would match is shown as `unreviewed_only` with its id and state, and adds nothing to the score.
- **Preferences** count only when both sides are known:
  - Seniority, work arrangement and salary can fit or mismatch.
  - Salary is compared only in the same currency and period, and its messages hold no amounts.
  - Target roles fit when the title names one. Locations fit when the location names one of yours.
  - A title or location that does not match is only noted as uncertain, because "Poland" covers "Warszawa".
- **Score.** Required requirements count 2 points, preferred ones 1, and each comparable preference 1. The score is the share of points met, and the reasons spell the sum out. With nothing to compare there is no score; the row stores 0 and the uncertainty `nothing_to_compare`.
- **Uncertainty** is stored as codes and messages:
  - `no_description` or `no_sections`;
  - `compared_by_wording`, because a gap may be something the profile does not name;
  - `unreviewed_evidence` and `language_levels`;
  - `no_preferences`;
  - the preference unknowns: `seniority_unknown`, `arrangement_unknown`, `location_unknown`, `location_unclear`, `no_salary`, `salary_not_comparable` and `title_not_target`.
- **Storage.** Migration 0008 adds `uncertainty`, `reasons`, `job_content_sha256` and `method` (`wording-v1`) to `job_matches`, and a trigger makes the table append-only.
  - Matching again adds a row; the latest one counts.
  - A match is outdated when the active profile version, the job's content hash or the method differs. Outdated matches are marked, never silently reused.
- **CLI:**
  - `jaa jobs match ID…` explains one job: score and reasons, evidence with fact ids, unreviewed-only evidence, gaps, preference fits and uncertainty. Several IDs, or `--all`, are ranked.
  - `jaa jobs matches` ranks the latest matches.
  - `jaa jobs show` lists the extracted requirements and the last match.
- Logs carry ids, counts and the profile version, never requirement or fact text.

## Consequences
- Matching by wording misses synonyms and paraphrases ("event-driven" against "event streaming"). The uncertainty message says so on every match with free-text requirements. Smarter matching can replace `wording-v1`, and older rows show as outdated.
- Description lines that are soft skills ("good communication") count as gaps unless the profile names them. That pulls scores down; the score is advisory and the gaps list explains it.
- Matching uses `jaa.documents.content.text_of` to read fact values the same way the CV does.
- T13 (tailored CVs) can build on the stored evidence (fact ids per requirement), and T14 on the stored application questions.
