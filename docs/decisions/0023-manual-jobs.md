# 0023 — Jobs added by hand

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #46

## Context
justjoin.it and bulldogjob.pl disallow their API and feeds in `robots.txt`, and nofluffjobs.com blocks automated clients and documents no API (#21, decision in #1). jaa must not work around that, but much of the Polish market posts only there. The user can read those offers in their browser and copy them.

## Decision
- **`jaa jobs add`** takes `--url`, `--title`, `--company`, optional `--location`, and the offer text from `--description` or `--description-file` (`-` reads standard input, so Claude can pass text the user pasted). Title, company and location are whitespace-normalized; empty fields, URLs that are not http(s) with a host, and descriptions over 100,000 characters are refused (`JobEntryError`).
- **Source `manual`.** The job is a normal `jobs` row (`jaa.storage.manual_jobs.add_manual_job`) with a random `source_job_id`, no `source_target`, `first_seen_at` = when it was added, and the initial status event recorded with actor `user`. Requirement extraction, matching, tailoring, answers and the tracker read it like any fetched job. `manual` is not a connector: it has no policy, is never fetched and is never marked removed.
- **The URL is recorded only.** It is stored as `source_url` and normalized into `canonical_url` for dedupe. No code path requests it; the CLI test replaces the transport and blocks sockets and `urlopen` to prove that.
- **Dedupe.** Adding the same link (by canonical URL) by hand twice is refused with the existing job's id, since that is almost always a repeated paste. A fetched job with the same link, or the same employer, title and location, is not an error: the pair is flagged through `flag_duplicates` (ADR 0014) for the user to decide.
- **Claude may run it.** Adding a job records data the user supplied, not a decision, so `jaa jobs add` is not user-only. The skill tells Claude to use only what the user pasted and never to open the link. No MCP tool is added yet.

## Consequences
- Manually added jobs never change on their own: if the employer edits the offer, the user deletes the job (`jaa jobs delete`) and adds it again, or keeps the old text.
- Structured details (salary, listed skills, experience level) are not captured; requirements come only from headings in the pasted text (ADR 0015), so a description without headings yields no free-text requirements.

## Amendment (2026-10-15): links to boards jaa can read
ADR 0025: `jaa jobs add <link>` reads an offer on a Greenhouse, Lever, Teamtailor or Personio board through that provider's API or feed (never the offer page itself). For every other link, and with `--no-fetch`, the job is added from the pasted text as described here, and the link is still never requested.
