# 0028 — Remote job feeds: Remotive, Arbeitnow, We Work Remotely

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #74 (from the T20 research, #21)
- **Amends:** ADR 0011 (daily request allowance, partial fetches), ADR 0013 (removal only after a complete fetch)

## Context
The T20 research approved three sources of remote jobs, each subject to a terms check when built: Remotive's public API, the Arbeitnow job board API and We Work Remotely's RSS feeds. They differ from the earlier connectors in two ways ADR 0011 did not cover. Remotive allows only about four calls a day, a limit that spans runs, while `RateLimit` only bounded one run. Arbeitnow lists many thousands of jobs over dozens of pages and asks not to be abused, and an RSS feed may list only the newest jobs. ADR 0013 marks every job missing from a fetch as removed, which is wrong when the fetch saw only part of the source.

## Decision
- **Terms reviewed on 2026-10-15:**
  - **Remotive: active.** The API documentation and the legal notice in every response grant developers access: link back to the Remotive URL and name Remotive, do not resubmit jobs to other sites, jobs are delayed 24 hours, at most 4 calls a day, more than 2 a minute blocked, no lead collection. jaa keeps the Remotive link as the job's link and the source on every job, and it is a local tool that publishes nothing. Remotive's robots.txt disallows `/api/*` for crawlers. jaa is not a crawler: it calls the documented API on the user's request, within the operator's stated limits. This conflict is recorded here and in the policy notes, and the source can be paused by policy if Remotive objects.
  - **Arbeitnow: active.** The terms (https://www.arbeitnow.com/terms, §11 API) let anyone use the API, ask for a link back, and say permission may be revoked. Every response repeats "free public API, please do not abuse". robots.txt allows everything.
  - **We Work Remotely: paused by policy** (`terms_reviewed_on` None, `paused_reason` set). robots.txt allows the feeds, but the feed page that states the conditions sits behind a bot check, so they could not be read and confirmed. The connector is complete; enabling it needs only a reviewed policy after confirming the terms with the operator.
- **Daily allowance (amends ADR 0011).** `RateLimit.max_requests_per_day` is optional. Before a run starts, `jaa.storage.listings.check_daily_quota` adds up the stored `source_fetch_runs` of that source in the last 24 hours, across all targets. Each run counts as at least one request, because failed runs are recorded without a request count. If one more full run could exceed the allowance, the run raises `SourceQuotaError` before any request, records nothing, and says when the allowance frees up. A policy's daily allowance must fit at least one full run. Remotive uses one request per run, 4 a day, 1 a minute and no retries, since a retry would spend the daily allowance.
- **Partial fetches (amends ADR 0013).** `FetchResult.complete` (default True) tells storage whether the connector saw the whole target. Jobs missing from an incomplete fetch are not marked removed. Arbeitnow reads at most its newest 5 pages and is complete only when it reaches the last page. We Work Remotely feeds are never complete, because nothing says a feed lists every job. Single-offer fetches were already incomplete.
- **Targets.** Remotive: a category slug (`software-dev`, …) or `all`. Arbeitnow: `all`, or `remote` (the API's `remote` flag, filtered locally). We Work Remotely: a fixed list of the published feeds (`all`, `back-end-programming`, …); jaa does not build feed URLs from user input.
- **Mapping.** All three are remote sources, so their jobs get `work_arrangement` `remote` and `<region> (remote)` as their location (Arbeitnow only for jobs flagged remote). Remotive sends timestamps without an offset, which are read as UTC. Descriptions become text. Free-text salaries and tags are not parsed: they are too irregular to match on.
- **Dedupe.** These sources link to their own job pages, so the same job on two of them shares no canonical URL. It is flagged through the employer, title and location key (ADR 0014). No connector here reads one offer by link, so `jaa jobs add` treats their links as unknown sites.

## Consequences
- A Remotive fetch over the allowance fails at once with the time it frees up. `jaa jobs fetch` skips the source's remaining targets and exits 1.
- Jobs from Arbeitnow beyond the newest 5 pages, and jobs that drop out of a We Work Remotely feed, are never marked removed. Users see them as open until they change their status.
- RemoteOK stays out (robots.txt disallows everything). The research found the same kind of conflict there; Remotive differs because its operator documents the API endpoint itself and asks developers to use it.
