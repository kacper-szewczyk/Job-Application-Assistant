# 0017 — Application questions and answer memory

- **Status:** Accepted
- **Date:** 2026-10-15
- **Issue:** #15

## Context
SPEC §6 asks to classify each application question as profile-backed, reusable user answer, inferred suggestion or unanswered; to show provenance and confidence; never to use inferences silently; to require explicit user input for work authorization, sponsorship, salary, demographic data, criminal/medical disclosures, consent and other legal questions unless the user configures a safe reusable answer; and to keep saved answers with a per-question reuse policy and a review/expiry date. T02 created `answers` (key, text, value, reuse policy, sensitive flag, confirmed/expiry dates) and `answer_sources` (answer → facts). Onboarding (ADR 0007) stores the default reuse policies `answer_reuse.default` and `answer_reuse.sensitive`. Listings may publish questions in `jobs.requirements` (ADR 0015), but SOLID.Jobs, the only source so far, does not.

## Decision
- **Topics give questions stable keys** (`jaa.answers.topics`, pure, English and Polish patterns on folded text). Explicit topics: `sponsorship`, `work_authorization`, `salary`, `criminal`, `medical`, `demographic`, `consent`, `legal`. Ordinary topics: contact details, name, location, start date/notice, relocation, work arrangement, travel. Explicit topics are checked first, so a question that mentions salary and start date is explicit.
  - Ordinary topics share one key (`start_date`), so "Notice period?" and "When can you start?" reuse one answer.
  - Explicit topics get one key per wording (`work_authorization.are_you_authorized_to_work_in_poland`), because the same topic asked about another country or another statement can need a different answer.
  - Other questions get `custom.<folded wording>` (long ones end in a short digest), so only the same question reuses its answer.
- **Classification** (`jaa.answers.classify`, pure) gives every question a class, an action (`fill`, `review`, `ask`), a confidence (`high`, `medium`, `low`), the value, its provenance (answer id, fact ids, preference key) and note codes. In order:
  1. A saved answer for the key → `reusable_answer`, following its reuse policy (`reuse` → fill, `reuse_with_review` → review, `always_ask` → ask). An ordinary answer saved for a different wording on the same topic is reviewed, not filled.
  2. For an explicit topic with no answer for this wording, the newest confirmed answer on the topic → `inferred_suggestion`, ask.
  3. A confirmed profile fact for the topic → `profile_backed`, with the user's `answer_reuse.default` policy. An unreviewed fact → `inferred_suggestion`, ask.
  4. A preference for the topic (salary range, sponsorship, start date, relocation, work arrangement, travel) → `inferred_suggestion`, ask. Sponsorship "depends" suggests nothing (ADR 0007).
  5. Otherwise `unanswered`, ask.

  Overrides: explicit topics are always `ask` unless the saved answer is for the same wording and has the `reuse` policy (the safe reusable answer). Unconfirmed or expired answers, and values that are not one of the question's options, are always `ask`.
- **Saving.** Saving an answer confirms it. Without an explicit policy, the answer-reuse preferences decide (sensitive policy for explicit topics). Setting `reuse` on an explicit question asks for confirmation in the CLI. Review dates default to 180 days for explicit topics and 365 for others; "never" is allowed only for ordinary questions.
- **Storage.** Migration 0010:
  - makes `answers` and `answer_sources` append-only by trigger: the newest row per key is current, and saving, re-confirming (`reconfirmed`) or changing the policy (`policy_changed`) adds a row that keeps the sources. Rows can still be deleted (`jaa answers forget`), because the user must be able to remove answers. The trigger leaves out the reference columns, so deleting a job or a forgotten answer can set them to null;
  - adds `answers.origin` (`user`, `accepted`, `reconfirmed`, `policy_changed`), `source_preference_key`, `source_answer_id` (an earlier answer the user accepted for another wording) and `job_id` (the job the answer was given for);
  - adds `job_questions` for questions the user copies from an application form. Listing questions stay in `jobs.requirements`.
- **CLI** `jaa answers add-question|remove-question|questions|prepare|review|list|show|set|reconfirm|policy|forget`. Sensitive values (explicit topics and sensitive keys such as contact details) are masked unless `--reveal` is given. `review` goes through questions that need input; `y` accepts the suggestion and records its provenance, typed text is saved as the user's answer.
- Logs carry answer ids, job ids and counts only. Question keys are not logged, because they quote the question.

## Consequences
- T15 links the answers used for an application through `application_answers`, which `prepare_answers` provenance makes possible; it does not need to change classification.
- Topic patterns can misfire. They err towards explicit topics: a question that only looks sensitive is asked, never filled. A misclassified ordinary question is at worst reviewed, because a reused answer for a different wording is never filled silently.
- Adding a topic changes keys for questions that used to be `custom.`; their old answers are then only found again if saved anew. Topic and key rules should change deliberately.
- Answers are free text. Structured answers (choices, dates) are matched against a question's options by folded text only.
