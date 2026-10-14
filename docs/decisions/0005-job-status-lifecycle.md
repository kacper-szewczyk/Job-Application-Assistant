# 0005 — Job status is the single application lifecycle

- **Status:** Accepted
- **Date:** 2026-10-14
- **Issue:** #3

## Context
SPEC §8 lists both `Job.status` and `Application.state` with status history, and SPEC §6 lists one set of states (`discovered` … `closed`) that covers both discovery and the application. Storing a state in two places invites them to disagree.

## Decision
- `jobs.status` is the only lifecycle state, using the nine SPEC §6 values.
- Every change is appended to `job_status_events` (from, to, actor, time, note) in the same transaction (`jaa.storage.jobs.set_job_status`). The history table rejects updates.
- `applications` holds submission details for a job (one per job): submitted-at, who confirmed the submission (`user` or `integration_receipt`), external reference, notes, and the answers used. A database CHECK prevents a `submitted_at` without a confirmation source.
- Transition rules (e.g. `submitted` only after user confirmation) are enforced by the application tracker in T15.

## Consequences
- One place to query "where is this job/application"; history is complete and append-only.
- An application's state is read from its job.
