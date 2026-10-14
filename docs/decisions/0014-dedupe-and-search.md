# 0014 — Canonical URLs, duplicate flags and job search

- **Status:** Accepted
- **Date:** 2026-10-14
- **Issue:** #12

## Context
T11 makes stored jobs usable once several sources feed them. A job fetched again must be stored once. The same job on two boards, or reposted under a new id, should be pointed out but never silently merged (SPEC §6). Users need to filter stored jobs, save searches and see when each was last refreshed, and every job must show its source link(s). T10 stored the source's URL as `canonical_url`, and for SOLID.Jobs that URL includes our campaign segment (`/o/<code>/jaa`).

## Decision
- **Two URLs per job.** Migration 0007 adds:
  - `jobs.source_url`: the link exactly as the source gave it, campaign included. This is what jaa shows and what the user opens.
  - `jobs.canonical_url`: now the normalized identity used for dedupe.
- **Connectors may name a cleaner identity.** `JobListing.canonical_url` is optional. SOLID.Jobs sets it to the offer link without the campaign segment, which it removes only when the path is exactly `/o/<code>/jaa`. `run_connector` drops a canonical URL that is not a usable http(s) URL, and storage then uses `url` instead.
- **Normalization** (`jaa.core.dedupe.canonical_url`, a pure function):
  - lowercase the scheme and host, and drop `www.`;
  - drop default ports, fragments and the trailing slash;
  - drop tracking parameters (`utm_*`, `gclid`, `fbclid`, `msclkid`, `mc_cid`, `mc_eid`, `ref`, `trk`);
  - sort the remaining query parameters;
  - leave the path case alone, because ids in paths can be case-sensitive.
- **Same source, same id: one row.** This is the existing `UNIQUE (source, source_job_id)`. Repeated fetches update `last_seen_at` and never add a row.
- **Possible duplicates are flagged, not merged.** A job is flagged against every other job that has either:
  - the same canonical URL (reason `canonical_url`), or
  - the same `dedupe_key` (reason `employer_title_location`): employer, title and location folded to lowercase ASCII words. Diacritics (`ł` becomes `l`), punctuation, trailing legal forms (`Sp. z o.o.`, `S.A.`, `GmbH`, `Inc`, …) and gender tags in titles (`(K/M)`, `(m/f/d)`) are ignored.

  The fallback is deliberately conservative. All three parts must be present and equal after folding, so the same title at the same employer in another city, or a listing without a location, is not flagged.

  Checks run when a job is stored and whenever its canonical URL or key changes. Jobs stored before migration 0007 get their keys on their next fetch.
- **Flags and verdicts are append-only.**
  - `job_duplicates` holds one row per pair (lower id first) with the reason and when it was found.
  - `job_duplicate_reviews` records the user's verdicts, `same_job` or `different_jobs`; the latest one counts.
  - A verdict never changes either job. `same_job` makes each job show the other's source link. `different_jobs` hides the flag.
  - A flag involving a job whose status is past `saved` gets a warning, because that is how a second application to the same job is caught.
- **Search** (`jaa.core.search.JobQuery`, a pure `matches`):
  - every word of the text must appear as a whole word in the title, employer, location or description;
  - company and location filters match substrings;
  - jobs can also be filtered by source and status;
  - removed jobs are left out unless asked for.

  All matching folds text the same way as the dedupe key, so `lodz` finds `Łódź`. Filtering happens in Python over the stored jobs: a few thousand rows is fast enough, and SQLite's `LIKE` does not fold non-ASCII letters.
- **Saved searches** (`saved_searches`) store a `JobQuery` as JSON under a unique name, with `created_at`, `updated_at`, `last_refreshed_at` and `last_result_count`. Running one records the refresh, marks results first seen since the previous refresh as new, and reports when the searched sources were last fetched. Replacing a search requires asking for it explicitly. Deleting one never touches jobs.
- **CLI:**
  - `jaa jobs list` gains `-q/--query`, `--company`, `--location`, `--source`, `--status` and `--save NAME [--replace]`;
  - `jaa jobs show ID` shows a job with its source links, flags and description;
  - `jaa jobs duplicates list [--all]` and `jaa jobs duplicates mark FLAG same|different`;
  - `jaa jobs searches list|run|delete`.
- Logs carry ids and counts, never titles, employers, URLs, query text or search names.

## Consequences
- A big employer posting the same title several times in one city produces a flag per pair. That is noisy, but it is how reposts are caught, and one `different` verdict clears each pair.
- Whole-word search means `java` does not find `JavaScript`, and `c++` folds to `c`. Prefix or fuzzy search can be added later if it turns out to be needed.
- Matching with explanations (T12) and manual job entry (#46) build on `JobQuery` and `flag_duplicates`. SOLID.Jobs salary, skills and experience level are still not stored; T12 decides what matching needs.
