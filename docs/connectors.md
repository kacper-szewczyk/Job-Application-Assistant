# Job source connectors

A connector reads job listings from one approved source and returns them as data. It never writes to storage, opens its own connections, or works around access controls. The contract lives in `jaa.connectors.base` (types), `jaa.connectors.http` (the client) and `jaa.connectors.run` (running and validation). Decisions behind it are in [ADR 0011](decisions/0011-connector-contract.md).

## Source policy

Every connector declares a `SourcePolicy`. `jaa sources show <key>` prints it.

| Field | Meaning |
|---|---|
| `key` | Stable identifier, `[a-z][a-z0-9_]{1,39}`. Used for `jobs.source` and pause history. |
| `name` | Human-readable name. |
| `access_method` | `public_api` (documented API meant for programmatic use) or `feed` (published RSS/Atom/JSON/XML feed). There is no HTML-scraping method. |
| `allowed_hosts` | Exact host names the client may request over https. A redirect to any other host counts as access denied. |
| `terms_url` | https link to the terms or API documentation that permit this access. |
| `terms_reviewed_on` | Date someone checked those terms. `None` keeps the source paused. |
| `permitted_fields` | `JobListing` fields the connector may keep. Must include `source_job_id`, `url`, `title`, `company`. Other fields are dropped and reported (`field_not_permitted`). |
| `rate_limit` | `requests` per `per_seconds` (sliding window) and `max_requests_per_run` (budget per fetch). |
| `retry` | `max_retries`, `backoff_seconds`, `max_wait_seconds`, `timeout_seconds` (see Failure behavior). |
| `max_response_bytes` | Cap on one response body (default 10 MiB). |
| `requires_credentials` | Must be `False`; a source that needs credentials stays paused. Public listing access never implies permission to submit applications (SPEC §6). |
| `paused_reason` | Set to ship a connector paused, e.g. when terms are unclear or the API is deprecated. |
| `notes` | Coverage, quirks, anything a reviewer should know. |

## Listing output

`Connector.fetch(client, target)` returns a `FetchResult` with `JobListing`s and `SourceIssue`s for one target (a board token, feed path, … — connector-specific). `Connector.target_key(target)` returns the canonical form of a target, which is how it is stored and compared (removal is judged per target), and raises `ConnectorTargetError` for targets the connector cannot fetch, before any request.

| `JobListing` field | Type | Notes |
|---|---|---|
| `source_job_id` | str | Required, unique per source, ≤ 200 chars. |
| `url` | str | Required, public http(s) page of the listing, without credentials. T11 canonicalizes it. |
| `title`, `company` | str | Required, ≤ 500 chars; whitespace is collapsed. |
| `location` | str \| None | Whitespace collapsed, ≤ 500 chars. |
| `description` | str \| None | As published, ≤ 200,000 chars. |
| `posted_at`, `updated_at` | datetime \| None | Must be timezone-aware; converted to UTC. |
| `questions` | tuple of `ApplicationQuestion` | `key`, `label`, `kind` (`text`, `long_text`, `single_select`, `multi_select`, `boolean`, `file`, `other`), `required`, `options`. The employer's questions, not answers. |

`run_connector` validates every listing: invalid ones become `invalid_listing` errors, repeated `source_job_id`s become `duplicate_listing` warnings (the first is kept), and fields outside `permitted_fields` are cleared with one `field_not_permitted` warning per field. Issues carry codes and IDs, never personal data.

## Shared HTTP client, rate limiting and errors

Connectors receive a `SourceClient` bound to their policy and make every request through `client.get(url)` or `client.get_json(url)`. The network is behind a `Transport` protocol. The real one is `jaa.connectors.urllib_transport.UrllibTransport` (stdlib, no cookies, redirects followed only within the same scheme, host and port), and tests replay recorded responses (`tests/fixtures/fake_connector.py`).

The client:

- refuses non-https URLs, hosts outside `allowed_hosts` and URLs with credentials, before sending anything;
- sends `User-Agent: jaa/<version> (local job application assistant)`;
- waits for the rate limiter before every attempt, and stops at `max_requests_per_run`;
- caps response size and applies `timeout_seconds`.

### Failure behavior

| Situation | Result | Retried? |
|---|---|---|
| HTTP 401, 403, 407, 451; `cf-mitigated: challenge`; redirect off the allowed hosts; HTML where JSON was expected | `SourceAccessDeniedError` | Never. The caller pauses the source. |
| Network error, HTTP 429, 500, 502, 503, 504 | retried after `Retry-After`, or `backoff_seconds * 2**attempt` | Up to `max_retries`, then `SourceUnavailableError`. A `Retry-After` longer than `max_wait_seconds` gives up at once. |
| Other non-2xx status, oversized body, invalid JSON, request budget exhausted, URL outside policy | `SourceDataError` | No. |
| Source paused (by policy or user) | `SourcePausedError` before any request | — |

## Pausing

A source is paused when its policy says so (`paused_reason`, unreviewed terms, required credentials — only a code change can resume these) or when the latest row in `source_status_events` is `paused`. Users pause and resume with `jaa sources pause|resume`. When a fetch raises `SourceAccessDeniedError`, the caller records a `system` pause with the error as the reason, so nothing is fetched again until the user has looked at it and resumes the source.

## Storing listings

`jaa.storage.listings.fetch_and_store` runs a connector for one target and stores the result (ADR 0013):

- New listings become `jobs` rows with status `discovered`. Known ones get a new `last_seen_at`, and their fields are updated when the listing's content hash changed.
- After a complete fetch, jobs of that source and target that were not listed get `removed_at`, cleared again if they come back. `jobs.status` is never changed by a fetch.
- Every fetch that reached the source adds a `source_fetch_runs` row with its outcome and counts. A `denied` fetch also pauses the source.

`jaa jobs fetch <source> <target>…` does this from the CLI.

## Sources

| Key | Source | Access | Targets |
|---|---|---|---|
| `solid_jobs` | [SOLID.Jobs](https://solid.jobs/api-ofert-pracy), Poland | Public offers API for integrators and aggregators, no key, `campaign=jaa`; 30 requests/min (the source allows 300) | Divisions: `IT`, `Engineering`, `Marketing`, `Sales`, `HR`, `Logistics`, `Finances`, `Other` |

Checked and not used (#21): justjoin.it and bulldogjob.pl disallow their API and feeds in `robots.txt`; nofluffjobs.com blocks automated clients and documents no API. Offers from those boards can be added by hand from pasted text (#46).

## Adding a connector

1. Check the source's documentation and terms (see T20, #21): public, documented, permits programmatic reading of listings, needs no credentials.
2. Write the policy with `terms_url`, `terms_reviewed_on`, the narrowest `allowed_hosts` and `permitted_fields`, and rate limits at or below what the source documents.
3. Implement `target_key` and `fetch`, with `client.get_json`/`client.get` only. Put unreadable items in `FetchResult.issues`; raise `SourceDataError` when the whole response is unusable.
4. Record real responses as synthetic fixtures (no personal data) and test against them with a replaying transport. Tests never touch the network.
5. Register it in `jaa.connectors.registry`.
