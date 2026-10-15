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
| `host_suffixes` | Provider domains that serve each company from its own subdomain: exactly one DNS label under the suffix is allowed (`teamtailor.com` allows `acme.teamtailor.com` only). A redirect from such a host to one outside the policy fails that target (`SourceDataError`) without pausing the source (ADR 0025). |
| `terms_url` | https link to the terms or API documentation that permit this access. |
| `terms_reviewed_on` | Date someone checked those terms. `None` keeps the source paused. |
| `permitted_fields` | `JobListing` fields the connector may keep. Must include `source_job_id`, `url`, `title`, `company`. Other fields are dropped and reported (`field_not_permitted`). |
| `rate_limit` | `requests` per `per_seconds` (sliding window), `max_requests_per_run` (budget per fetch) and optional `max_requests_per_day` (checked before a run against the stored fetch runs of the last 24 hours, every run counting as at least one request; over it, `SourceQuotaError` and nothing is sent, ADR 0028). |
| `retry` | `max_retries`, `backoff_seconds`, `max_wait_seconds`, `timeout_seconds` (see Failure behavior). |
| `max_response_bytes` | Cap on one response body (default 10 MiB). |
| `requires_credentials` | Must be `False`; a source that needs credentials stays paused. Public listing access never implies permission to submit applications (SPEC §6). |
| `paused_reason` | Set to ship a connector paused, e.g. when terms are unclear or the API is deprecated. |
| `notes` | Coverage, quirks, anything a reviewer should know. |

## Listing output

`Connector.fetch(client, target)` returns a `FetchResult` with `JobListing`s and `SourceIssue`s for one target, plus `complete`: False when the connector saw only part of the target (a feed of the newest jobs, a page cap), so nothing missing from it is marked removed (a board token, feed path, … — connector-specific). `Connector.target_key(target)` returns the canonical form of a target, which is how it is stored and compared (removal is judged per target), and raises `ConnectorTargetError` for targets the connector cannot fetch, before any request.

| `JobListing` field | Type | Notes |
|---|---|---|
| `source_job_id` | str | Required, unique per source, ≤ 200 chars. |
| `url` | str | Required, public http(s) page of the listing, without credentials. Stored as the job's `source_url`, the link the user opens. |
| `canonical_url` | str \| None | Optional identity of the page when the connector knows a cleaner one than `url` (e.g. without a campaign segment). Dropped if it is not a usable http(s) URL. Normalized and used to find the same job on other sources (ADR 0014). |
| `title`, `company` | str | Required, ≤ 500 chars; whitespace is collapsed. |
| `location` | str \| None | Whitespace collapsed, ≤ 500 chars. |
| `description` | str \| None | As published, ≤ 200,000 chars. |
| `posted_at`, `updated_at` | datetime \| None | Must be timezone-aware; converted to UTC. |
| `questions` | tuple of `ApplicationQuestion` | `key`, `label`, `kind` (`text`, `long_text`, `single_select`, `multi_select`, `boolean`, `file`, `other`), `required`, `options`. The employer's questions, not answers. |
| `details` | `JobDetails` (`jaa.core.job_details`) | Structured `skills` and `languages` (name, level), `experience_level`, `work_arrangement` (`remote`, `hybrid`, `on_site`) and `salaries` (min, max, currency, period, contract) as the source lists them. Stored in `jobs.requirements` with the questions and used for matching (ADR 0015). |

`run_connector` validates every listing: invalid ones become `invalid_listing` errors, repeated `source_job_id`s become `duplicate_listing` warnings (the first is kept), and fields outside `permitted_fields` are cleared with one `field_not_permitted` warning per field. Issues carry codes and IDs, never personal data.

## Shared HTTP client, rate limiting and errors

Connectors receive a `SourceClient` bound to their policy and make every request through `client.get(url)`, `client.get_json(url)` or `client.get_xml(url)` (RSS/XML feeds; documents with a DOCTYPE are refused, so no entity expansion). The network is behind a `Transport` protocol. The real one is `jaa.connectors.urllib_transport.UrllibTransport` (stdlib, no cookies, redirects followed only within the same scheme, host and port), and tests replay recorded responses (`tests/fixtures/fake_connector.py`).

The client:

- refuses non-https URLs, hosts outside `allowed_hosts` and `host_suffixes`, and URLs with credentials, before sending anything;
- sends `User-Agent: jaa/<version> (local job application assistant)`;
- waits for the rate limiter before every attempt, and stops at `max_requests_per_run`;
- caps response size and applies `timeout_seconds`.

### Failure behavior

| Situation | Result | Retried? |
|---|---|---|
| HTTP 401, 403, 407, 451; `cf-mitigated: challenge`; redirect off the allowed hosts; HTML where JSON or a feed was expected | `SourceAccessDeniedError` | Never. The caller pauses the source. |
| Redirect from a per-company host (`host_suffixes`) to a host outside the policy | `SourceDataError` (that target only) | No. |
| HTTP 404 | `SourceNotFoundError` (a `SourceDataError`); a single offer that is gone returns nothing | No. |
| Network error, HTTP 429, 500, 502, 503, 504 | retried after `Retry-After`, or `backoff_seconds * 2**attempt` | Up to `max_retries`, then `SourceUnavailableError`. A `Retry-After` longer than `max_wait_seconds` gives up at once. |
| Other non-2xx status, oversized body, invalid JSON or XML, XML with a DOCTYPE, request budget exhausted, URL outside policy | `SourceDataError` | No. |
| Source paused (by policy or user) | `SourcePausedError` before any request | — |
| Daily allowance (`max_requests_per_day`) used up | `SourceQuotaError` before any request, with the time it frees up | — |

## Pausing

A source is paused when its policy says so (`paused_reason`, unreviewed terms, required credentials — only a code change can resume these) or when the latest row in `source_status_events` is `paused`. Users pause and resume with `jaa sources pause|resume`. When a fetch raises `SourceAccessDeniedError`, the caller records a `system` pause with the error as the reason, so nothing is fetched again until the user has looked at it and resumes the source.

## Storing listings

`jaa.storage.listings.fetch_and_store` runs a connector for one target and stores the result (ADR 0013):

- New listings become `jobs` rows with status `discovered`. Known ones get a new `last_seen_at`, and their fields are updated when the listing's content hash changed.
- After a complete fetch (`FetchResult.complete`), jobs of that source and target that were not listed get `removed_at`, cleared again if they come back. `jobs.status` is never changed by a fetch.
- Every fetch that reached the source adds a `source_fetch_runs` row with its outcome and counts. A `denied` fetch also pauses the source.
- New jobs, and jobs whose canonical URL or employer, title and location changed, are checked for duplicates. A possible duplicate gets a `job_duplicates` flag; jobs are never merged (ADR 0014).

`jaa jobs fetch <source> <target>…` does this from the CLI; `jaa jobs fetch --followed` fetches every target the user follows (`jaa jobs follow|unfollow|following`, append-only `source_follows`, ADR 0025).

`fetch_and_store_job` does the same for one offer of a target (`OfferConnector.fetch_job`), for links the user brings with `jaa jobs add <link>`. It never marks other jobs of the target removed.

## Links the user brings

`jaa.connectors.links.recognize(url)` (pure, no request) sorts a link into an offer or a board on a source jaa can read (with the connector's target and the offer id), a blocked site with the reason, or an unknown site. `jaa jobs add <link>` reads recognised offers through the provider's API or feed, suggests following for board links, and needs the pasted offer for everything else (ADR 0023, ADR 0025).

## Sources

| Key | Source | Access | Targets |
|---|---|---|---|
| `solid_jobs` | [SOLID.Jobs](https://solid.jobs/api-ofert-pracy), Poland | Public offers API for integrators and aggregators, no key, `campaign=jaa`; 30 requests/min (the source allows 300) | Divisions: `IT`, `Engineering`, `Marketing`, `Sales`, `HR`, `Logistics`, `Finances`, `Other` |
| `greenhouse` | [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html) | Public GET endpoints, no key; board name plus `jobs?content=true`, one request per offer (with application questions); 20 requests/min | Board tokens: `acme`, or `eu:acme` for EU boards |
| `lever` | [Lever Postings API](https://github.com/lever/postings-api) | Published postings, no key, pages of 100; the site name stands in for the employer; 20 requests/min | Site names: `acme`, or `eu:acme` |
| `teamtailor` | [Teamtailor RSS feed](https://support.teamtailor.com/en/articles/11171756-rss-feed-how-to-guide) | `jobs.rss` of each career site, pages of 100; only `<company>.teamtailor.com`, not custom domains; 20 requests/min | Sites: `acme` or `acme.teamtailor.com` |
| `personio` | [Personio positions feed](https://developer.personio.de/docs/retrieving-open-job-positions) | `/xml` of each career page, one request; employer is the subcompany or the subdomain | Pages: `acme`, `acme.jobs.personio.de` or `acme.jobs.personio.com` |
| `remotive` | [Remotive public API](https://github.com/remotive-com/remote-jobs-api) | One request per run, at most 4 a day across all targets, no retries; jobs appear 24 h late; each job keeps its Remotive link and source (the terms require naming Remotive and linking back, and forbid resubmitting jobs elsewhere) | Category slugs such as `software-dev`, or `all` |
| `arbeitnow` | [Arbeitnow job board API](https://www.arbeitnow.com/blog/job-board-api), mostly Germany | Free API, terms §11 ask for a link back; only the newest 5 pages (a few hundred jobs each), so a fetch that stops early marks nothing removed; 6 requests/min | `all`, or `remote` (jobs flagged remote) |
| `weworkremotely` | [We Work Remotely RSS feeds](https://weworkremotely.com/remote-job-rss-feed) | **Paused by policy**: the feed page is behind a bot check, so its terms could not be confirmed. The connector is complete; enabling it needs only a reviewed policy. Feeds never mark jobs removed | `all`, `programming`, `back-end-programming`, `front-end-programming`, `full-stack-programming`, `devops-sysadmin`, `design`, `product`, `customer-support`, `sales-and-marketing`, `management-and-finance`, `other` |

Remotive, Arbeitnow and We Work Remotely link to their own job pages, so the same job on two of them rarely shares a canonical URL; it is flagged through the employer, title and location key instead (ADR 0014). None of them can read one offer by link, so `jaa jobs add` treats their links as unknown sites. RemoteOK (robots.txt disallows everything) and the others in the T20 research stay out.

Ashby, Workable and Recruitee are not added until their own documentation confirms unauthenticated read access; SmartRecruiters requires an API key.

Checked and not used (#21): justjoin.it and bulldogjob.pl disallow their API and feeds in `robots.txt`; nofluffjobs.com blocks automated clients and documents no API. Add offers from those boards by hand from their pasted text with `jaa jobs add` (source `manual`, ADR 0023); jaa records the link and never fetches it.

## Adding a connector

1. Check the source's documentation and terms (see T20, #21): public, documented, permits programmatic reading of listings, needs no credentials.
2. Write the policy with `terms_url`, `terms_reviewed_on`, the narrowest `allowed_hosts` and `permitted_fields`, and rate limits at or below what the source documents.
3. Implement `target_key` and `fetch`, with `client.get_json`/`client.get` only. Put unreadable items in `FetchResult.issues`; raise `SourceDataError` when the whole response is unusable.
4. Record real responses as synthetic fixtures (no personal data) and test against them with a replaying transport. Tests never touch the network.
5. Register it in `jaa.connectors.registry`. If it can read one offer by id, implement `fetch_job` (`OfferConnector`) and teach `jaa.connectors.links` its offer and board links.
