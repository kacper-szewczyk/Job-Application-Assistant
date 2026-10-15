# 0025 — Company career boards and job links

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #71 (from the T20 research, #21)
- **Amends:** ADR 0011 (per-company hosts), ADR 0023 (links the user brings)

## Context
Many employers publish their openings on an applicant tracking system's public board: Greenhouse and Lever document public read APIs, Teamtailor and Personio publish feeds (`docs/research/2026-10-sources-and-linkedin-portability.md`). These boards belong to one company each, so the user picks which companies to follow; jaa must never list a provider's customers. Teamtailor and Personio serve every company from its own subdomain, while ADR 0011's `SourcePolicy.allowed_hosts` is a fixed list of exact hosts. Users also bring single offer links, which `jaa jobs add` (ADR 0023) only recorded.

## Decision
- **Four connectors**, each with a reviewed `SourcePolicy` (terms reviewed 2026-10-15): `greenhouse` (Job Board API, `boards-api.greenhouse.io` and `boards-api.eu.greenhouse.io`), `lever` (Postings API, `api.lever.co` and `api.eu.lever.co`), `teamtailor` (`<company>.teamtailor.com/jobs.rss`, access method `feed`) and `personio` (`<company>.jobs.personio.de|com/xml`, `feed`). Targets are one company's board: a Greenhouse token or Lever site (`eu:` prefix for the EU region), a Teamtailor or Personio host. Ashby, Workable and Recruitee wait until their own documentation confirms public read access; SmartRecruiters needs a key and stays out.
- **Per-company hosts (amends ADR 0011).** `SourcePolicy.host_suffixes` lists provider domains under which exactly one more DNS label is allowed (`teamtailor.com` allows `acme.teamtailor.com`, not `teamtailor.com`, `a.b.teamtailor.com` or a custom domain). Hosts are never derived from user input beyond that one label, so a target cannot point the client at an arbitrary site. Companies that moved their career site to their own domain are not read.
- **A board that redirects elsewhere fails that target only.** For a host matched through `host_suffixes`, a redirect off the allowed hosts is a `SourceDataError` for that fetch rather than an access denial: it means the company moved its board, not that the provider refused jaa, so the whole source is not paused. 401/403/451, bot checks and HTML instead of data still pause the source as in ADR 0011.
- **XML feeds** go through `SourceClient.get_xml`, which refuses HTML (treated as a login wall) and any document with a DOCTYPE, so entity expansion is impossible without adding a parser dependency. A 404 is now `SourceNotFoundError`, a `SourceDataError` subclass, so connectors can tell a closed offer from a broken response.
- **Single offers.** Connectors that can read one offer implement `OfferConnector.fetch_job` (Greenhouse and Lever have single-posting endpoints, with Greenhouse's application questions; Teamtailor and Personio read their feed and pick the offer). `jaa.storage.listings.fetch_and_store_job` stores it with its source and target like a fetched job, records the run, and never marks other jobs of the target removed.
- **Links (amends ADR 0023).** `jaa.connectors.links.recognize` is pure and never makes a request. `jaa jobs add <link>` (or `--url`):
  - an offer on a recognised board is read through that provider's API or feed; the offer page itself is never requested. Pasted fields are refused unless `--no-fetch` stores the pasted text as a `manual` job instead (e.g. while the source is paused);
  - a board link adds nothing and suggests `jaa jobs follow` or `jaa jobs fetch`;
  - a known blocked site (LinkedIn, justjoin.it, nofluffjobs, bulldogjob, pracuj.pl, theprotocol.it, rocketjobs.pl, Indeed, Glassdoor) says why and needs the pasted offer, as before;
  - any other site needs the pasted offer; reading one page's schema.org JobPosting stays "later" (#21).
- **Following.** `jaa jobs follow <link | SOURCE TARGET>`, `jaa jobs unfollow`, `jaa jobs following`, and `jaa jobs fetch --followed` to fetch every followed target. Follows are append-only events in `source_follows` (migration 0012; the latest event per source and target decides) and are exported. Following works for any source target, solid.jobs divisions included. Unfollowing keeps the jobs already fetched.
- **Claude may run these.** Adding an offer by link, following and fetching record what the user asked for, not a decision about their profile or applications, so they are not user-only (ADR 0020). The skill tells Claude to follow only boards the user chose.

## Consequences
- Lever and Personio do not name the employer (Personio only for subcompanies), so the site name or subdomain is stored as the employer. Duplicate detection by employer, title and location may miss the same offer on another board; canonical URLs still match.
- Greenhouse boards embedded on a company's own site (`?gh_jid=`) are not recognised; the user follows the board token instead.
- Teamtailor's feed names no job-level update date, so a changed offer is noticed by its content hash only.
