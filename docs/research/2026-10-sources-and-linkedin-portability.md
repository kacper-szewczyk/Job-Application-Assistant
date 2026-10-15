# Additional job sources and the LinkedIn portability API (T20)

Research for [#21](https://github.com/kacper-szewczyk/Job-Application-Assistant/issues/21), done 2026-10-15. It extends the Polish-board review in the #21 comment of 2026-10-14 (solid.jobs: go, now the T10 connector; justjoin.it, bulldogjob.pl, nofluffjobs.com: no-go, manual entry via #46).

Rules applied (SPEC §5–6, CLAUDE.md, ADR 0011): only documented APIs or published feeds meant for programmatic use; no login walls, CAPTCHAs or bot protection; robots.txt and terms respected; no LinkedIn scraping. A "go" here means a follow-up issue may be built; each connector still records its own `terms_reviewed_on` when it is implemented, and anything marked "verify" must be checked against the provider's own documentation first.

Verdicts: **go** (follow-up issue created), **later** (revisit if something changes), **no-go**.

## LinkedIn Member Data Portability API

| Question | Finding |
|---|---|
| Eligibility | Members located in the EEA and Switzerland ([Member Data Portability (Member)](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/member-data-portability-member)). |
| Access | Self-serve: the member creates a developer app on LinkedIn's default "Member Data Portability (Member) Default Company" page, requests the *Member Data Portability API (Member)* product and accepts its terms; access is granted immediately. A token is generated in the developer portal's OAuth Token Generator with scope `r_dma_portability_self_serve`. The third-party variant (`r_dma_portability_3rd_party`) needs LinkedIn's approval and is not needed for a local tool used by the member. |
| Token | About 60 days by default; refresh tokens are limited to approved partners ([refresh tokens](https://learn.microsoft.com/en-us/linkedin/shared/authentication/programmatic-refresh-tokens)), so the member generates a new token when it expires. |
| API | `GET https://api.linkedin.com/rest/memberSnapshotData?q=criteria&domain=…` with `Linkedin-Version: 202312` (the only accepted version) ([Snapshot API](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/shared/member-snapshot-api)). Paged; keep paging until "No data found for this memberId". Some domains appear later than others after consent. A Changelog API covers only the last 28 days of activity (posts, comments) and is not useful here. |
| Profile fields | `PROFILE`, `POSITIONS`, `EDUCATION`, `SKILLS`, `CERTIFICATIONS`, `LANGUAGES`, `PROJECTS`, `HONORS`, `COURSES`, `PUBLICATIONS`, `PATENTS`, `VOLUNTEERING_EXPERIENCES`, `RECOMMENDATIONS`, `PROFILE_SUMMARY` ([domains](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/shared/snapshot-domain)). Values use the same labels as the archive CSVs (e.g. `"First Name"`), so the T07 field mapping should mostly carry over. |
| Job fields | `SAVED_JOBS`, `JOB_APPLICATIONS`, `SAVED_JOB_ALERTS`, `JOB_SEEKER_PREFERENCES`, `JOB_APPLICANT_SAVED_ANSWERS`, `TALENT_QUESTION_SAVED_RESPONSE`. |
| Writing back | No API writes to the member's profile. |
| Data handling | The snapshot also contains messages, contacts, connections, logins, ads data and identity verification. jaa must request only the domains it maps and never fetch everything (calling without `domain` returns all domains). |

**Verdicts**

- **Profile import via the self-serve API: go.** Adds a repeatable pull without waiting for an archive. Facts arrive `unreviewed` with provenance, exactly like T07. Needs an ADR amending the "never ask for LinkedIn tokens" rule narrowly: the CLI prompts for the member's own self-serve token with hidden input, uses it for one pull, never stores or logs it, and neither Claude nor the MCP server ever handles it.
- **Saved jobs, applications, job alerts and job-seeker preferences via the same API: go** (same issue). Saved jobs become manual-style jobs with title, company and link; the description is pasted by the user (#46), because LinkedIn job pages are never fetched. Job-seeker preferences are offered as suggestions for onboarding, never stored as answered preferences without the user.
- **Profile improvement suggestions: go** (separate issue). Read-only: compare the imported LinkedIn profile with confirmed facts and target jobs and suggest edits the user makes on LinkedIn.
- **Searching or browsing LinkedIn job offers: no-go.** There is no job search API; the Job Posting API is for approved partners publishing jobs, and fetching `linkedin.com/jobs` pages is scraping.

## Company career boards (applicant tracking systems)

The company publishes these boards for anyone to read; the user chooses which companies to follow, so jaa never crawls the provider for its customer list.

| Source | Finding | Verdict |
|---|---|---|
| Greenhouse | Official [Job Board API](https://docs.greenhouse.io/job-board.html): "authentication is not required for any GET endpoints". `boards-api.greenhouse.io/v1/boards/{token}/jobs[?content=true]`, single job `…/jobs/{id}`. EU boards on `boards-api.eu.greenhouse.io`. | **Go** |
| Lever | Official [Postings API](https://github.com/lever/postings-api): `api.lever.co/v0/postings/{site}` and `api.eu.lever.co`, `skip`/`limit` paging, single posting endpoint; only published postings. | **Go** |
| Teamtailor | Official [RSS feed](https://support.teamtailor.com/en/articles/11171756-rss-feed-how-to-guide): `<careers site>/jobs.rss`, 100 jobs by default, `offset`/`per_page`. Common in Poland and the Nordics. | **Go** |
| Personio | Public XML feed `{company}.jobs.personio.de/xml` (`language=en`), which the company must enable ([Personio support](https://support.personio.de/hc/en-us/articles/207576365)). Common in Germany and Poland. | **Go** |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{org}`, unauthenticated per third-party descriptions; official docs not found yet. | **Go, verify first** |
| Workable | `apply.workable.com/api/v1/widget/accounts/{subdomain}` (careers widget), unauthenticated per third-party descriptions. | **Go, verify first** |
| Recruitee | `{company}.recruitee.com/api/offers`, unauthenticated per third-party descriptions. | **Go, verify first** |
| SmartRecruiters | Official [Posting API](https://developers.smartrecruiters.com/docs/posting-api) says it supports only API key authentication, which `requires_credentials=False` rules out. | Later |

Teamtailor, Personio and Recruitee serve each company from its own host (subdomain or custom domain), while `SourcePolicy.allowed_hosts` is a fixed list of exact hosts. The follow-up needs a reviewed extension of ADR 0011 (allowed host patterns, or hosts derived from the validated target) rather than a policy that allows any host.

## Aggregators and remote-job feeds

| Source | Finding | Verdict |
|---|---|---|
| Arbeitnow | [Job board API](https://www.arbeitnow.com/blog/job-board-api), no key, European jobs mostly from ATS boards, `remote` and `visa_sponsorship` fields. robots.txt allows everything. No stated terms. | **Go, check terms with the operator** |
| Remotive | [Public API](https://github.com/remotive-com/remote-jobs-api) `remotive.com/api/remote-jobs`: about 4 calls a day, more than 2 a minute is blocked, jobs delayed 24 h, link back and name Remotive, do not resubmit to other sites. A local tool showing the source link fits. | **Go, within those limits** |
| We Work Remotely | Public RSS feeds; robots.txt allows them. | **Go, check terms** |
| RemoteOK | API terms ask only for a followed link back, but robots.txt has `Disallow: /` for all agents. | Later (conflicting signals; ask the operator) |
| Adzuna | Needs `app_id`/`app_key`; [terms](https://developer.adzuna.com/docs/terms_of_service) are written for publishing listings with Adzuna branding or for research; 250 calls a day; Poland (`pl`) reportedly covered. | Later |
| Jooble, Careerjet | Key issued to publishers for a website; links go through click tracking. | No-go |
| EURES | No official public API or reuse terms found; coverage depends on what member states share. | Later |
| Hacker News "Who is hiring" | Official HN API, but each post is free text with no structure. | Later |

## Other Polish and global boards

| Source | Finding | Verdict |
|---|---|---|
| pracuj.pl, theprotocol.it (Grupa Pracuj) | No public API or feed found. | No-go; ask for a partner feed |
| rocketjobs.pl | robots.txt disallows `/api/` and offer listing paths. | No-go |
| Indeed | The public job search (Publisher) API is closed to new integrations; current APIs are employer-side and partner-gated. | No-go |
| Glassdoor | No job search API. | No-go |

## Links the user brings

Proposed flow for `jaa jobs add <url>`:

1. **Recognised ATS host** (Greenhouse, Lever, Teamtailor, Personio, …): a single-offer link fetches that one job through the official API; a board link offers to follow that company (a connector target). Part of the company-boards follow-up.
2. **Known blocked host** (LinkedIn, justjoin.it, nofluffjobs, bulldogjob, pracuj.pl, …): explain why and fall back to pasted text (#46).
3. **Unknown host:** fetching just that page once, when robots.txt allows it and there is no login wall, and reading its [schema.org JobPosting](https://developers.google.com/search/docs/appearance/structured-data/job-posting) data. **Later**: it goes beyond ADR 0011 (APIs and feeds only) and needs its own decision. Pasted text covers it meanwhile.

## Follow-up issues

Created only for items the user approved on 2026-10-15:

- [#71](https://github.com/kacper-szewczyk/Job-Application-Assistant/issues/71) Company career boards connectors and link recognition
- [#72](https://github.com/kacper-szewczyk/Job-Application-Assistant/issues/72) LinkedIn self-serve portability import (profile and job activity) with the token ADR
- [#73](https://github.com/kacper-szewczyk/Job-Application-Assistant/issues/73) LinkedIn profile improvement suggestions
- [#74](https://github.com/kacper-szewczyk/Job-Application-Assistant/issues/74) Remote-job feeds (Remotive, Arbeitnow, We Work Remotely)
