# 0011 — Job source connector contract and source policy

- **Status:** Accepted
- **Date:** 2026-10-14
- **Issue:** #10

## Context
T09 sets the contract every job source connector follows (SPEC §6 Job discovery, §7, §10). Sources change their terms and access over time. We must not work around login walls, CAPTCHAs or bot protection, and each connector must document its access method, permitted fields, rate limits and failure behavior. Connectors depend on `core` types and return data. They must not write to storage, and tests must never hit the network.

## Decision
- **Policy as code.** Each connector carries a frozen `SourcePolicy` (`jaa.connectors.base`): access method, exact allowed https hosts, terms URL and review date, permitted listing fields, rate limit and per-run request budget, retry policy, response size cap, credentials flag, optional `paused_reason`, notes. It is validated at import time. The schema is documented in `docs/connectors.md`. Policies live in code, not in the database, so a change in access rules is a reviewed code change.
- **Access methods: `public_api` and `feed` only.** HTML scraping is not an option in the enum. Adding one would need a new ADR.
- **One shared client.** Connectors never open connections. They get a `SourceClient` bound to their policy. It enforces https and allowed hosts, sets an identifying User-Agent, applies a sliding-window rate limiter and request budget, caps response size and times out. It retries only network errors, 429 and 5xx, honouring `Retry-After` up to `max_wait_seconds`. The network is behind a `Transport` protocol. No real transport ships yet: T10 adds a stdlib one, which avoids a new dependency, and tests use recorded fixtures.
- **Access denial is final for the run.** 401/403/407/451, Cloudflare challenge markers, redirects to non-allowed hosts and HTML where data was expected raise `SourceAccessDeniedError` and are never retried. The caller records a `system` pause with that reason. Fetching resumes only after the user runs `jaa sources resume`.
- **Pause state** is append-only history in `source_status_events` (migration 0005). The latest row decides, and no row means active. `effective_state` combines it with the policy, and policy pauses win: `paused_reason` set, terms never reviewed, or credentials required. These are `locked` and cannot be resumed from the CLI. `run_connector` refuses paused sources before any request.
- **Listing output** (`JobListing`): `source_job_id`, `url`, `title`, `company` are required. Location, description, posted/updated dates (aware, converted to UTC) and application questions are optional. `run_connector` validates and tidies listings, drops fields the policy does not permit, and reports invalid, duplicate and dropped data as `SourceIssue`s instead of failing the run. Turning listings into `Job` rows, canonical URLs and dedupe across sources is T10/T11.
- **CLI:** `jaa sources list|show|pause|resume`. The registry (`jaa.connectors.registry`) is empty until T10.

## Consequences
- Every connector behaves the same under rate limits and failures, and a new connector only writes the parsing.
- A source that starts blocking us goes quiet until a person looks, instead of being retried.
- Shipping a connector paused (`paused_reason`) or with `terms_reviewed_on=None` keeps it out of use until its terms are checked.
- Connectors that need pagination must stay within `max_requests_per_run`. Sources too large for that need a higher budget, set in the policy and justified in review.
