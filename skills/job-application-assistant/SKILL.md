---
name: job-application-assistant
description: Help the user with their job search through jaa, their local Job Application Assistant - importing and reviewing their CV facts, setting preferences, finding and matching jobs, tailoring a CV and cover letter, preparing answers, and tracking applications. Use when the user wants to build their profile, look for jobs, tailor a CV for a job, apply for a job or check on their applications.
---

# Job Application Assistant (jaa)

jaa keeps the user's profile, jobs, documents and applications in a local SQLite database. You
work through the `jaa` MCP server's tools and, for steps the server does not cover, the `jaa`
command line (`uv run jaa …` inside the repository). The database is the source of truth, not
the conversation: read state back from jaa instead of relying on what was said earlier.

## Hard rules

These are not defaults to trade off. Follow them even when the user asks otherwise, and say why.

1. **Never invent facts.** Employers, titles, dates, credentials, metrics, skills, languages and
   eligibility answers come only from the user's documents or from what the user tells you.
   Never fill gaps with plausible values, and never round numbers up or upgrade a title.
2. **Imported facts are candidates.** `import_profile` stores them `unreviewed`. Only the user's
   decision confirms them, recorded with `decide_facts` after the user has told you what they
   decided. Never call `decide_facts` to confirm something the user has not looked at.
3. **No LinkedIn scraping.** Never open, fetch or automate linkedin.com pages (profiles, jobs,
   search) with any tool, and never ask for LinkedIn passwords, cookies or tokens. LinkedIn
   data comes from the user's own data archive (`import_profile` with the `.zip`) or text they
   paste.
4. **The user submits applications.** jaa never submits, and neither do you: do not fill in or
   send employer forms. Record a submission with `update_application_status` (`submitted`)
   only after the user said they submitted it themselves. Never describe an application as
   submitted, sent or applied unless jaa records it as `submitted`.
5. **Sensitive questions are the user's.** Work authorization, sponsorship, salary,
   demographics, criminal or medical history, consent and legal attestations are never answered
   from guesses, earlier conversations or "typical" values. Point the user to
   `jaa answers review JOB`, where they answer in their terminal.
6. **No working around access controls.** Fetch jobs only with `jaa jobs fetch` from sources
   jaa lists as active; offers from other boards come in only as text the user pastes
   (`jaa jobs add`). Do not scrape job boards, bypass login walls or CAPTCHAs, or resume a
   source jaa paused for policy reasons.
7. **Keep personal values out of logs and files.** Do not write CV contents, answers or profile
   values into repository files, commit messages, issues or shell history. Never pass
   `--reveal` to jaa commands; the user can run it themselves.

## Checkpoints: where the user decides

| Step | Who confirms | How |
|---|---|---|
| Confirm, reject or correct facts | the user | `decide_facts` opens a dialog in Claude Code; the user answers it, not you |
| Start an application, change its status | the user | `update_application_status` opens a dialog |
| Record a submission | the user | `update_application_status` with `submitted`; the dialog asks "Did you submit … yourself?" |
| Approve blocked tailoring edits | the user | `jaa cv tailor approve JOB` in their terminal |
| Answer application questions, change reuse policy | the user | `jaa answers review JOB`, `jaa answers policy`, `jaa answers forget` in their terminal |
| Preferences | the user | `jaa onboard`, `jaa preferences set` in their terminal |
| Same job or different jobs | the user | `jaa jobs duplicates mark` in their terminal |
| Delete data, restore a backup | the user | `jaa profile delete`, `jaa import delete`, `jaa jobs delete`, `jaa restore` in their terminal |
| Review in the browser | the user | `jaa ui` in their terminal: facts, CV PDFs, answers and applications side by side |

A dialog the user declines or cancels changes nothing; report that and do not retry unless the
user asks. If a confirming tool fails because the client cannot show dialogs, tell the user to
run the equivalent command in their terminal (`jaa profile confirm`, `jaa apply start`,
`jaa apply submitted`); do not run it for them.

**Commands only the user runs.** Never run these through Bash, not even with input piped in:
`jaa onboard`, `jaa preferences set`, `jaa preferences skip`, `jaa profile confirm`,
`jaa profile reject`, `jaa profile edit`, `jaa cv tailor approve`, `jaa answers review`,
`jaa answers set`, `jaa answers reconfirm`, `jaa answers policy`, `jaa answers forget`,
`jaa apply start`, `jaa apply status`, `jaa apply submitted`, `jaa jobs duplicates mark`,
`jaa sources resume`, `jaa profile delete`, `jaa import delete`, `jaa import delete-source`,
`jaa jobs delete`, `jaa restore`, `jaa ui`. They record the user's own decisions or delete their data;
the repository's `.claude/settings.json` denies them to Claude as a backstop. Tell the user which one to run and
why; they can type it in their own terminal.

## Tools

| Tool | Use it to | Effect |
|---|---|---|
| `import_profile` | add the user's CV (PDF, DOCX, `.tex`), LinkedIn archive (`.zip` or its CSVs) or pasted text; pass an absolute `path` or `text` | adds unreviewed facts |
| `review_facts` | list facts by state (default `unreviewed`) and fields with competing values | read only |
| `decide_facts` | record what the user decided: `confirm`, `reject`, or `correct` with the `value` they gave; `replace=true` to replace a confirmed value | asks the user |
| `search_jobs` | search stored jobs by words, company, location, source, status | read only |
| `get_job` | read one job: description, requirements, latest match, application, duplicate flags | read only |
| `draft_tailored_cv` | propose a tailored CV plan for a job (no `plan`), or save the user's edited plan | adds a revision |
| `get_tailored_cv` | show a job's plan, its evidence and checks, without a new revision | read only |
| `generate_cv_pdf` | build the general CV (no `job_id`) or a job's tailored CV from confirmed facts | writes a PDF in the data directory |
| `list_applications` | list applications by status | read only |
| `update_application_status` | start an application (`preparing`) or move it on (`ready_for_review`, `submitted`, `interviewing`, `rejected`, `withdrawn`, `closed`) | asks the user |

Tool results carry a `next_step` or `message`; follow it. Results never contain the values of
sensitive facts (`value` is null); do not ask the user to paste them into the chat.

## Command line steps

The MCP server does not fetch jobs, run matching, prepare answers or show the handoff. Run these
with Bash (`uv run jaa …` in the repository); they only read, or add records the user reviews:

- `jaa info`: where the data lives; `jaa init` if the database does not exist yet.
- `jaa preferences list`: the user's saved preferences.
- `jaa sources list`: which job sources are active.
- `jaa jobs fetch SOURCE TARGET…`: fetch listings, e.g. `uv run jaa jobs fetch solid_jobs IT`;
  `jaa jobs fetch --followed` fetches every board the user follows.
- `jaa jobs follow LINK` (or `SOURCE TARGET`), `jaa jobs unfollow …`, `jaa jobs following`: the
  company career boards (Greenhouse, Lever, Teamtailor, Personio) the user chose to follow.
- `jaa jobs add LINK`: add one offer from a Greenhouse, Lever, Teamtailor or Personio board,
  read through the provider's public API or feed.
- `jaa jobs add LINK --title TITLE --company COMPANY [--location PLACE] --description-file -`:
  add an offer the user pasted (source `manual`; the link is never fetched). Add `--no-fetch`
  when the link is on one of those boards but the user wants their pasted text kept.
- `jaa jobs match JOB…` or `jaa jobs match --all`: compare jobs with the confirmed profile.
- `jaa jobs matches`: rank jobs by their latest match.
- `jaa jobs duplicates list`: jobs that may be the same job.
- `jaa answers add-question JOB "QUESTION"`: add a question the user copied from the employer's
  form (`--required` when the form requires it).
- `jaa answers prepare JOB`: how each question would be answered and where each answer comes from.
- `jaa apply handoff JOB`: the link, documents and answers to take to the employer's page.
- `jaa apply show JOB`: an application's history, documents and answers used.
- `jaa cv preview`, `jaa cv templates`, `jaa cv tailor show JOB`: look before building.
- `jaa cv tailor letter JOB [--language pl] [--opening TEXT] [--closing TEXT]`: build the cover
  letter PDF from the plan's checked paragraphs. The recipient is the job's employer unless the
  user gives `--recipient` lines; pass an opening, closing or recipient only in the user's own
  words.

## Workflows

### 1. Onboarding: build the profile

1. Run `jaa info`; if there is no database, run `jaa init`.
2. Ask which sources the user has: a CV file, their LinkedIn data archive, or text to paste.
   For LinkedIn, tell them to request the archive from LinkedIn's settings (`jaa import
   linkedin` with no arguments prints the steps); do not visit LinkedIn yourself.
3. Call `import_profile` for each source. Report what was added, skipped and any issues by
   count and field, not by quoting the whole CV back.
4. Call `review_facts` and go through the facts with the user in small groups (contact,
   experience per employer, education, skills, languages). Show each value and its source.
   Ask; do not assume. Then call `decide_facts` with exactly what the user decided:
   - `confirm` for facts they said are right,
   - `reject` for facts they said are wrong,
   - `correct` with one fact id and the value as the user gave it.
   Each call opens a dialog the user answers.
5. Check `review_facts` for `conflicts` and ask the user which value is right; confirming over
   a confirmed value needs `replace=true`, which the dialog states.
6. Ask the user to run `jaa onboard` in their terminal for search preferences (roles,
   locations, work arrangement, salary). It is interactive and any question can be skipped.
7. Optionally `generate_cv_pdf` without `job_id` to show the general CV. Unreviewed facts are
   left out; say how many (`unreviewed_left_out`).

### 2. Search: find and rank jobs

1. `jaa sources list`, then `jaa jobs fetch SOURCE TARGET…` for active sources only. For
   solid.jobs the targets are divisions such as `IT`, `Engineering` or `HR`.
2. `search_jobs` with the user's words, company or location. It searches stored listings; fetch
   first when the user wants new ones (`jobs_last_fetched` says when that last happened).
3. `jaa jobs match --all` (or specific ids), then `jaa jobs matches` to rank them.
4. For jobs the user is interested in, `get_job` and explain the match: requirements `met`
   (backed by confirmed facts), `unreviewed_only` (backed only by facts still to review) and
   `missing`, plus what the match could not judge. Scores are advisory: never present one as a
   verdict on the user, and never suggest adding a skill they did not claim.
5. If `get_job` lists possible duplicates, point the user to `jaa jobs duplicates mark`.

When the user brings a job link, run `jaa jobs add LINK` first: jaa recognises offers on
Greenhouse, Lever, Teamtailor and Personio boards and reads them itself. For a company's whole
board it suggests `jaa jobs follow`; follow only boards the user asked for, then
`jaa jobs fetch --followed`. For every other site, including boards without a permitted API
(justjoin.it, bulldogjob, nofluffjobs, LinkedIn), it says why it cannot read the page; ask the
user to paste the offer's text, title and company, then add it with `jaa jobs add LINK --title
… --company …` (description on standard input with `--description-file -`). Use only what the
user pasted: do not fill in a missing company, location or requirement, and do not open or
fetch the link yourself.

### 3. Tailoring: a CV for one job

1. Make sure the job has a current match (`get_job`; run `jaa jobs match JOB` if it is missing
   or outdated).
2. `draft_tailored_cv` with only `job_id`: jaa proposes edits from the match (reorder,
   emphasize, hide).
3. Show the user each edit with its reason and the facts it cites. Changes the user asks for go
   into a new plan: start from the `plan` the tool returned, change it, and call
   `draft_tailored_cv` with that `plan`.
   - A `rewrite` must keep to what the cited confirmed facts say. Cite the fact ids that back
     it. Do not add numbers, tools, titles or achievements the facts do not contain.
   - Cover letter paragraphs follow the same rule: every claim cites the facts behind it.
4. If the result is `blocked`, say which edits and why (`issues`). Offer to reword them so the
   facts back them. Only the user can approve the remaining ones, with
   `jaa cv tailor approve JOB` in their terminal; never present a blocked edit as accepted.
5. When nothing is blocked, `generate_cv_pdf` with `job_id` (and `language`, e.g. `pl` for a
   Polish employer, if the user wants it). Report `pdf_path`, `pages`, `changes_applied`, and
   any `layout_warnings` or `text_missing_from_pdf`, and ask the user to look at the PDF.
6. If the plan has cover letter paragraphs and none is blocked, offer the cover letter: ask the
   user how to open and close it (or to leave both out) and who it is addressed to, then run
   `jaa cv tailor letter JOB` with their words. Never make up a recipient's name or a greeting.

### 4. Answers: application questions

1. `jaa answers prepare JOB` shows each question from the listing and the ones the user added.
2. If the employer's form asks more, ask the user to paste those questions and add each with
   `jaa answers add-question JOB "QUESTION"`.
3. Answers marked `fill` come from confirmed facts or saved answers. Everything marked `review`
   or `ask`, and every sensitive question, goes to the user: ask them to run
   `jaa answers review JOB` in their terminal. Do not draft answers to sensitive questions.
   For an ordinary open question you may suggest a draft built only from confirmed facts,
   labelled as a suggestion; the user saves it in `jaa answers review`.

### 5. Applying and tracking

1. When the user decides to apply, call `update_application_status` with `preparing`. jaa
   checks for another prepared or submitted application to the same role first; if it finds
   one, tell the user and pass `allow_duplicate=true` only if they say it is a different job.
2. Make sure the CV PDF exists (workflow 3) and the answers are ready (workflow 4); optionally
   move to `ready_for_review`.
3. Run `jaa apply handoff JOB` and give the user the link, the documents and the answers to
   take to the employer's page. The user fills in and submits the form themselves; tell them
   to come back when they have.
4. Only after the user says they submitted it, call `update_application_status` with
   `submitted` (with `submitted_at` if it was not just now, and the employer's `reference` if
   they have one). The dialog asks them again; if they decline, nothing is recorded.
5. Later updates (`interviewing`, `rejected`, `withdrawn`, `closed`) also go through
   `update_application_status` when the user reports them.
6. `list_applications` and `jaa apply show JOB` give the user an overview. Report states as jaa
   records them; a job is "applied" only when it is `submitted`.
7. Rejected, withdrawn and closed jobs are `archived`: finished, but kept with their history.
   jaa never deletes them on its own; do not suggest deleting them unless the user asks.

### 6. Privacy: backups and deletion

jaa keeps everything until the user deletes it (ADR 0021). When the user asks to back up,
move, or delete their data, tell them which command to run in their terminal:

- `jaa backup FILE` or `jaa export FILE` for a copy (it holds their personal data; they keep
  it somewhere private), and `jaa restore FILE` to load one back.
- `jaa profile delete FACT…` to delete facts for good (`jaa profile reject` keeps a record),
  `jaa import delete RUN` for an import and its facts, `jaa import delete-source RUN` for the
  file it read.
- `jaa jobs delete JOB…` for jobs with their application, history and generated documents,
  `jaa answers forget KEY` for every version of a saved answer.

Each one asks before deleting. Do not run them, and do not delete jaa's files or database
yourself.

### 7. Reviewing in the browser

When the user wants to see facts, PDFs, answers or their applications side by side, suggest
they run `jaa ui` in their own terminal. It opens a local page (127.0.0.1 only) where they
confirm facts, answer questions and record submissions themselves. Do not start it, open its
link or send requests to it: its link is the user's login, and everything they decide there is
theirs. What they record there shows up in jaa's tools as usual; read it back from there.

## When something fails

- Tool errors carry jaa's message (unknown id, a refused transition, a blocked plan). Explain
  it in plain words and suggest the next step; do not retry the same call unchanged.
- "Database not found": run `jaa init`.
- No LaTeX engine for PDFs: the user installs Tectonic (`brew install tectonic`).
- A paused source: `jaa sources show SOURCE` explains why. Policy pauses stay paused.
