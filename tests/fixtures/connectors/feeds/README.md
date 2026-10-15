# Remote job feed fixtures

Synthetic responses in the shapes the sources send (#74, ADR 0028). Companies and jobs are
invented; nothing here was copied from a real listing.

- `remotive.json`: Remotive `/api/remote-jobs` (legal notice, `job-count`, `jobs`). Timestamps
  carry no offset, as Remotive sends them. The third job lacks a company.
- `arbeitnow_page1.json`, `arbeitnow_page2.json`: Arbeitnow `/api/job-board-api?page=N` with
  `data`, `links` and `meta`; the description is entity-encoded HTML, `created_at` is epoch
  seconds. Page 2 is the last (`links.next` is null).
- `weworkremotely.rss`: a We Work Remotely category feed; titles are `Company: Job title`.
