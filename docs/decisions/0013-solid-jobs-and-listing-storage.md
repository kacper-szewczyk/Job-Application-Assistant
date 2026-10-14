# 0013 — First job source (SOLID.Jobs) and storing listings

- **Status:** Accepted
- **Date:** 2026-10-14
- **Issue:** #11

## Context
T10 adds the first connector on the T09 contract (ADR 0011) and stores what it fetches. The Polish market comes first (#1). Of the Polish boards checked (#21), only SOLID.Jobs offers a documented public API meant for programmatic use. justjoin.it and bulldogjob.pl disallow their API and feeds in `robots.txt`, and nofluffjobs.com blocks automated clients and documents no API. Fetching from those would mean working around access controls, which is not allowed. The connector must find new, changed and removed listings, record when it saw them, and handle errors and rate limits as the policy says. Listing access never implies permission to apply (SPEC §6).

## Decision
- **SOLID.Jobs through its public offers API.** `GET https://solid.jobs/public-api/offers/{division}` (https://solid.jobs/api-ofert-pracy) is published for integrators and aggregators, AI assistants included. It needs no key or registration, and requests carry `campaign=jaa` for the source's traffic analytics. The site terms are at https://solid.jobs/about/terms. Both were reviewed on 2026-10-14. The policy (`jaa.connectors.solid_jobs.POLICY`):
  - only `solid.jobs`;
  - 30 requests per minute, well under the documented 300;
  - at most 20 requests per run;
  - 16 MiB per response;
  - every listing field except application questions, which the API does not have.
- **Targets are divisions** (`IT`, `Engineering`, `Marketing`, `Sales`, `HR`, `Logistics`, `Finances`, `Other`), matched case-insensitively. A fetch reads every page of 500 offers by `pageIndex`. If the page index in a response does not match the request, or the envelope is unreadable, the fetch fails.
- **Mapping.**
  - `jobOfferKey` becomes `source_job_id`; `url` is kept as given.
  - `locations` plus `isRemote` or `isHybrid` become one location text, e.g. `Warszawa (hybrid)` or `Remote`.
  - The HTML description becomes plain text with `jaa.connectors.html_text` (paragraphs and list items kept, markup dropped). HTML from a source is never stored or rendered.
  - `validFrom` becomes `posted_at` and `updatedAt` becomes `updated_at`. Timestamps without an offset are dropped.
  - Salary, skills and experience level are in the API but not kept yet: `JobListing` has no fields for them. That is for T11 and T12.
- **Connectors canonicalize targets.** The `Connector` protocol gains `target_key(target)`. It returns the stored form of a target, or raises `ConnectorTargetError` before any request. Removal is judged per target, so `it` and `IT` must be the same target.
- **Real transport.** `jaa.connectors.urllib_transport.UrllibTransport` uses the standard library, so there is no new dependency. It sends no cookies. It follows redirects only to the same scheme, host and port. Any other redirect is returned with its target as the response URL, unfollowed, and the client then treats it as access denied (ADR 0011). It reads at most `max_bytes + 1` bytes of a body, so the client can report an oversized response.
- **Storing listings** (`jaa.storage.listings`, migration 0006):
  - New listings become `jobs` rows (status `discovered`) with `first_seen_at`, `last_seen_at`, `source_target`, `content_sha256` and `source_updated_at`.
  - On every later fetch, `last_seen_at` moves forward. When the content hash differs, the listing fields are updated.
  - After a successful, complete fetch, the target's jobs that were not listed get `removed_at`. It is cleared if they come back ("reappeared").
  - Removal is an observation, not a status. `jobs.status` stays the user's lifecycle (ADR 0005), so a job the user is applying to is never closed by a fetch.
  - A failed fetch never marks anything removed.
- **Fetch history.** Every fetch that reached the source adds an append-only `source_fetch_runs` row. It records the target, start and end times, outcome (`ok`, `denied`, `unavailable`, `failed`), request count, counts of new, changed, unchanged, reappeared and removed listings, the issues (codes and ids), and the error message. A `denied` outcome also records a `system` pause, so nothing is fetched again until the user resumes the source. Paused sources and unknown targets fail before any request and record nothing.
- **CLI:** `jaa jobs fetch <source> <target>…`, `jaa jobs list [--source] [--include-removed]`. `jaa sources show` now lists recent fetches.
- **Boards without a permitted API** are left out, not scraped. Users can add their offers by hand from pasted text (#46).

## Consequences
- A first `jaa jobs fetch solid_jobs IT` stores about 1,600 Polish IT offers in 4 requests, and later fetches report what changed.
- The source caches responses for an hour, so fetching more often shows nothing new.
- Paging over a listing that changes mid-fetch can repeat an offer (reported as `duplicate_listing`) or miss one. A missed offer is marked removed and comes back on the next fetch as reappeared.
- Canonical URLs and dedupe across sources are T11 (#12). Until then, `canonical_url` is the source's URL, including the campaign suffix the source adds.
