"""Profile fact persistence and the review workflow (SPEC §6, ADR 0006).

Importers hand candidates to `add_imported_facts`, which only ever stores `unreviewed` facts.
Only `confirm_facts`, `reject_facts` and `correct_fact` change review state; each call is one
transaction that appends to `profile_fact_events` and, when the set of confirmed facts changes,
snapshots a new immutable profile version and makes it active.

Logging records ids and counts only.
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from jaa.core.clock import utcnow
from jaa.core.errors import FactConflictError, NotFoundError, ReviewError, StorageError
from jaa.core.models import (
    FactCandidate,
    FactOrigin,
    FactState,
    ImportRun,
    JsonValue,
    Profile,
    ProfileFact,
    ProfileFactEvent,
    ProfileVersion,
)
from jaa.core.review import check_transition, find_conflicts, same_value, value_key
from jaa.core.sensitivity import is_sensitive_key
from jaa.storage.repository import (
    import_runs,
    profile_fact_events,
    profile_facts,
    profile_versions,
    profiles,
)

log = logging.getLogger(__name__)


def _id(value: int | None) -> int:
    if value is None:
        raise StorageError("record has no id")
    return value


@dataclass(frozen=True, slots=True)
class ImportResult:
    run: ImportRun
    added: tuple[ProfileFact, ...]
    # Candidates identical to a fact already stored (in any state) for the same key.
    duplicates: int


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """Facts whose state changed (or were created) and the version created, if any."""

    changed: tuple[ProfileFact, ...]
    version: ProfileVersion | None


def add_imported_facts(
    conn: sqlite3.Connection,
    run: ImportRun,
    candidates: Sequence[FactCandidate],
) -> ImportResult:
    """Store an import run and its candidates as `unreviewed` facts.

    Confirmed facts are never touched. A candidate equal to an existing fact for the same key
    is skipped, so re-importing the same source does not refill the review queue; a differing
    value stays unreviewed and shows up in `conflicts`.
    """
    with conn:
        stored_run = import_runs.insert(conn, run)
        seen: set[tuple[str, str]] = {
            (str(row["field_key"]), str(row["value_normalized"]))
            for row in conn.execute("SELECT field_key, value_normalized FROM profile_facts")
        }
        added: list[ProfileFact] = []
        duplicates = 0
        for candidate in candidates:
            # value_normalized is stored as value_key() text, so the stored text is comparable.
            marker = (candidate.field_key, value_key(candidate.value_normalized))
            if marker in seen:
                duplicates += 1
                continue
            seen.add(marker)
            sensitive = (
                is_sensitive_key(candidate.field_key)
                if candidate.is_sensitive is None
                else candidate.is_sensitive
            )
            added.append(
                profile_facts.insert(
                    conn,
                    ProfileFact(
                        field_key=candidate.field_key,
                        value_raw=candidate.value_raw,
                        value_normalized=candidate.value_normalized,
                        state=FactState.UNREVIEWED,
                        origin=FactOrigin.IMPORT,
                        confidence=candidate.confidence,
                        is_sensitive=sensitive,
                        import_run_id=stored_run.id,
                        source_location=candidate.source_location,
                        source_excerpt=candidate.source_excerpt,
                        created_at=stored_run.imported_at,
                        updated_at=stored_run.imported_at,
                    ),
                )
            )
    log.info(
        "import run id=%s stored facts=%d duplicates=%d",
        stored_run.id,
        len(added),
        duplicates,
    )
    return ImportResult(stored_run, tuple(added), duplicates)


@dataclass(frozen=True, slots=True)
class StoredImport:
    result: ImportResult
    # Earlier runs of the same source file, whose known values were skipped.
    earlier: tuple[ImportRun, ...]
    # Field keys of the added facts that now have competing values.
    conflicts: tuple[str, ...]


def store_import(
    conn: sqlite3.Connection, run: ImportRun, candidates: Sequence[FactCandidate]
) -> StoredImport:
    """Store an importer's run and candidates, and report earlier runs and new conflicts."""
    earlier = import_runs.list(conn, source_sha256=run.source_sha256) if run.source_sha256 else []
    result = add_imported_facts(conn, run, candidates)
    added_keys = {f.field_key for f in result.added}
    conflicts = [c.field_key for c in find_conflicts(profile_facts.list(conn))]
    return StoredImport(result, tuple(earlier), tuple(k for k in conflicts if k in added_keys))


def _confirmed_for_key(conn: sqlite3.Connection, field_key: str) -> ProfileFact | None:
    found = profile_facts.list(conn, field_key=field_key, state=FactState.CONFIRMED)
    return found[0] if found else None


def _set_state(
    conn: sqlite3.Connection, fact: ProfileFact, target: FactState, at: datetime
) -> ProfileFact:
    check_transition(fact.id, fact.state, target)
    stamp = {"confirmed_at": at} if target is FactState.CONFIRMED else {"rejected_at": at}
    return profile_facts.update(conn, _id(fact.id), state=target, updated_at=at, **stamp)


def _record(
    conn: sqlite3.Connection,
    transitions: Sequence[tuple[ProfileFact, FactState | None]],
    at: datetime,
    version: ProfileVersion | None,
) -> None:
    for fact, from_state in transitions:
        profile_fact_events.insert(
            conn,
            ProfileFactEvent(
                fact_id=_id(fact.id),
                from_state=from_state,
                to_state=fact.state,
                occurred_at=at,
                profile_version_id=version.id if version else None,
            ),
        )


def _snapshot(conn: sqlite3.Connection, at: datetime, note: str) -> ProfileVersion:
    """Freeze the current confirmed facts into a new version and make it active."""
    version = profile_versions.insert(conn, ProfileVersion(created_at=at, note=note))
    conn.execute(
        "INSERT INTO profile_version_facts (profile_version_id, fact_id) "
        "SELECT ?, id FROM profile_facts WHERE state = 'confirmed'",
        (version.id,),
    )
    if existing := profiles.list(conn):
        profiles.update(conn, _id(existing[0].id), active_version_id=version.id, updated_at=at)
    else:
        profiles.insert(conn, Profile(active_version_id=version.id, created_at=at, updated_at=at))
    log.info("created profile version id=%s", version.id)
    return version


def _get_all(conn: sqlite3.Connection, fact_ids: Sequence[int]) -> list[ProfileFact]:
    if not fact_ids:
        raise ReviewError("no fact ids given")
    if len(set(fact_ids)) != len(fact_ids):
        raise ReviewError("fact ids must be unique")
    return [profile_facts.get(conn, i) for i in fact_ids]


def confirm_facts(
    conn: sqlite3.Connection,
    fact_ids: Sequence[int],
    *,
    replace: bool = False,
    at: datetime | None = None,
) -> ReviewResult:
    """Confirm facts and snapshot a new profile version.

    If a different fact is already confirmed for the same key, this raises `FactConflictError`
    unless `replace` is set, in which case the old fact is rejected in the same transaction.
    Facts that are already confirmed are left unchanged.
    """
    at = at or utcnow()
    with conn:
        facts = _get_all(conn, fact_ids)
        keys = [f.field_key for f in facts]
        if len(set(keys)) != len(keys):
            raise ReviewError("cannot confirm two facts for the same field key at once")
        pending = [f for f in facts if f.state is not FactState.CONFIRMED]
        for fact in pending:
            check_transition(fact.id, fact.state, FactState.CONFIRMED)

        transitions: list[tuple[ProfileFact, FactState | None]] = []
        for fact in pending:
            current = _confirmed_for_key(conn, fact.field_key)
            if current is not None:
                if not replace:
                    raise FactConflictError(
                        f"fact {fact.id} conflicts with confirmed fact {current.id} "
                        f"for {fact.field_key}"
                    )
                transitions.append(
                    (_set_state(conn, current, FactState.REJECTED, at), current.state)
                )
            transitions.append((_set_state(conn, fact, FactState.CONFIRMED, at), fact.state))

        version = _snapshot(conn, at, "confirmed facts") if transitions else None
        _record(conn, transitions, at, version)
    log.info("confirmed facts=%d", len(pending))
    return ReviewResult(tuple(f for f, _ in transitions), version)


def reject_facts(
    conn: sqlite3.Connection, fact_ids: Sequence[int], *, at: datetime | None = None
) -> ReviewResult:
    """Reject facts. A new version is created only if a confirmed fact was rejected."""
    at = at or utcnow()
    with conn:
        facts = _get_all(conn, fact_ids)
        pending = [f for f in facts if f.state is not FactState.REJECTED]
        transitions = [(_set_state(conn, f, FactState.REJECTED, at), f.state) for f in pending]
        was_confirmed = any(old is FactState.CONFIRMED for _, old in transitions)
        version = _snapshot(conn, at, "rejected facts") if was_confirmed else None
        _record(conn, transitions, at, version)
    log.info("rejected facts=%d", len(pending))
    return ReviewResult(tuple(f for f, _ in transitions), version)


def correct_fact(
    conn: sqlite3.Connection,
    fact_id: int,
    value_raw: str,
    value_normalized: JsonValue,
    *,
    replace: bool = False,
    at: datetime | None = None,
) -> ReviewResult:
    """Record the user's corrected value as a new confirmed fact and reject the original.

    Fact values are immutable, so the correction is a new fact (`origin=correction`) that
    points at the original; the original keeps its provenance. `replace` has the same meaning
    as in `confirm_facts`, for a confirmed fact other than the one being corrected.
    """
    at = at or utcnow()
    with conn:
        original = profile_facts.get(conn, fact_id)
        if same_value(original.value_normalized, value_normalized):
            raise ReviewError(f"fact {fact_id} already has this value; confirm it instead")

        transitions: list[tuple[ProfileFact, FactState | None]] = []
        current = _confirmed_for_key(conn, original.field_key)
        if current is not None and current.id != original.id:
            if not replace:
                raise FactConflictError(
                    f"fact {current.id} is already confirmed for {original.field_key}; "
                    "replace it explicitly"
                )
            transitions.append((_set_state(conn, current, FactState.REJECTED, at), current.state))
        if original.state is not FactState.REJECTED:
            transitions.append((_set_state(conn, original, FactState.REJECTED, at), original.state))

        correction = profile_facts.insert(
            conn,
            ProfileFact(
                field_key=original.field_key,
                value_raw=value_raw,
                value_normalized=value_normalized,
                state=FactState.CONFIRMED,
                origin=FactOrigin.CORRECTION,
                is_sensitive=original.is_sensitive,
                corrected_from_fact_id=original.id,
                created_at=at,
                updated_at=at,
                confirmed_at=at,
            ),
        )
        transitions.append((correction, None))
        version = _snapshot(conn, at, "corrected fact")
        _record(conn, transitions, at, version)
    log.info("corrected fact id=%s new id=%s", fact_id, correction.id)
    return ReviewResult(tuple(f for f, _ in transitions), version)


def fact_history(conn: sqlite3.Connection, fact_id: int) -> list[ProfileFactEvent]:
    return profile_fact_events.list(conn, fact_id=fact_id)


def correction_chain(conn: sqlite3.Connection, fact_id: int) -> list[ProfileFact]:
    """The fact followed by the facts it corrects, newest first."""
    chain = [profile_facts.get(conn, fact_id)]
    seen = {fact_id}
    while (previous := chain[-1].corrected_from_fact_id) is not None and previous not in seen:
        seen.add(previous)
        try:
            chain.append(profile_facts.get(conn, previous))
        except NotFoundError:
            break
    return chain


def active_version_id(conn: sqlite3.Connection) -> int | None:
    found = profiles.list(conn)
    return found[0].active_version_id if found else None


def version_fact_ids(conn: sqlite3.Connection, version_id: int) -> list[int]:
    profile_versions.get(conn, version_id)  # raises NotFoundError for unknown versions
    rows = conn.execute(
        "SELECT fact_id FROM profile_version_facts WHERE profile_version_id = ? ORDER BY fact_id",
        (version_id,),
    ).fetchall()
    return [int(r["fact_id"]) for r in rows]


def version_fact_counts(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute(
        "SELECT profile_version_id AS v, COUNT(*) AS n FROM profile_version_facts GROUP BY v"
    ).fetchall()
    return {int(r["v"]): int(r["n"]) for r in rows}


@dataclass(frozen=True, slots=True)
class DeletedFacts:
    """How many facts were deleted, and the version created if confirmed facts were among them."""

    facts: int
    version: ProfileVersion | None


def _delete_facts(
    conn: sqlite3.Connection, facts: Sequence[ProfileFact], at: datetime
) -> DeletedFacts:
    ids = [_id(f.id) for f in facts]
    conn.executemany("DELETE FROM profile_facts WHERE id = ?", [(i,) for i in ids])
    # The deleted facts drop out of every earlier version too; a new version makes the change
    # visible to matches and tailored CVs, which are compared against the active version.
    was_confirmed = any(f.state is FactState.CONFIRMED for f in facts)
    version = _snapshot(conn, at, "deleted facts") if was_confirmed else None
    return DeletedFacts(len(ids), version)


def delete_facts(
    conn: sqlite3.Connection, fact_ids: Sequence[int], *, at: datetime | None = None
) -> DeletedFacts:
    """Delete facts, their review history and their place in every profile version (ADR 0021).

    Unlike rejecting, this removes the value from the database. Corrections made from a deleted
    fact are kept: they hold what the user typed.
    """
    at = at or utcnow()
    with conn:
        result = _delete_facts(conn, _get_all(conn, fact_ids), at)
    log.info("deleted facts=%d", result.facts)
    return result


def delete_import(
    conn: sqlite3.Connection, run_id: int, *, at: datetime | None = None
) -> DeletedFacts:
    """Delete an import run, every fact it added and any template registered from it.

    The imported file itself is not touched; `privacy.delete_import_source` deletes that.
    """
    at = at or utcnow()
    with conn:
        import_runs.get(conn, run_id)
        facts = profile_facts.list(conn, import_run_id=run_id)
        result = _delete_facts(conn, facts, at)
        conn.execute("DELETE FROM template_candidates WHERE import_run_id = ?", (run_id,))
        conn.execute("DELETE FROM import_runs WHERE id = ?", (run_id,))
    log.info("deleted import run id=%s facts=%d", run_id, result.facts)
    return result
