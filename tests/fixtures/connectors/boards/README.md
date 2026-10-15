# Career board fixtures

Synthetic responses in the shapes the providers document (#71, ADR 0025). Companies, people and
offers are invented; nothing here was copied from a real board.

- `greenhouse_board.json`, `greenhouse_jobs.json`, `greenhouse_job.json`: Greenhouse Job Board API
  (`/v1/boards/{token}`, `/jobs?content=true`, `/jobs/{id}?questions=true`). `content` is
  entity-encoded HTML, as Greenhouse sends it.
- `lever_page0.json`, `lever_page1.json`, `lever_posting.json`: Lever Postings API with `mode=json`,
  pages of two.
- `teamtailor.rss`: a Teamtailor career site's `jobs.rss`.
- `personio.xml`: a Personio `/xml` positions feed.
