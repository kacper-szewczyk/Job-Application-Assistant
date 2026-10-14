# 0006 — Profile fact review, conflicts and versions

- **Status:** Accepted
- **Date:** 2026-10-14
- **Issue:** #4

## Context
SPEC §6 requires that imported facts are reviewed before use, that re-imports never silently overwrite confirmed facts, and that profile edits are versioned so generated documents can name the profile version they used. T02 already made fact values and provenance immutable (corrections are new facts). T03 has to define what a "conflict" is, what a version contains and when one is created.

## Decision
- **One confirmed value per field key.** A partial unique index (`state = 'confirmed'`) enforces it. Field keys therefore identify a single value; list-like data uses distinct keys per item (e.g. `skills.python`, `experience.<employer-slug>.title`), which importers (T05–T07) must produce.
- **Imports only add `unreviewed` facts.** `jaa.storage.profile.add_imported_facts` takes `FactCandidate`s, which have no state field. A candidate whose key and normalized value equal an existing fact (in any state) is skipped, so re-importing the same source does not refill the review queue, and a previously rejected value does not come back.
- **A conflict** is a field key with unreviewed facts whose values differ from the confirmed one, or from each other when nothing is confirmed. Conflicts are derived (`jaa.core.review.find_conflicts`), not stored.
- **Resolution is explicit.** Confirming a fact for a key that already has a confirmed value fails unless the user passes `--replace`; the old fact is then rejected in the same transaction. Keeping the current value means rejecting the incoming fact. Editing creates a confirmed `correction` fact that points at the original and rejects the original.
- **Transitions:** `unreviewed → confirmed | rejected`, `confirmed ↔ rejected`. Facts never return to `unreviewed`. Every transition is appended to `profile_fact_events`, which rejects updates.
- **Versions:** each review action that changes the set of confirmed facts creates one immutable `ProfileVersion` holding exactly the confirmed facts, and makes it the profile's active version. Rejecting an unreviewed fact changes nothing that documents could use, so it creates no version.

## Consequences
- A profile version is unambiguous: one value per key, so document generation (T08) can render it without choosing.
- Importers must choose stable field keys; unstable keys (e.g. positional indexes that shift between CV versions) would show up as new facts instead of conflicts.
- Replaced values are recorded as `rejected`, so "superseded" and "wrong" are not distinguished in the state; the event history and versions show what happened.
