"""Tailored CV plans for jobs: proposing, editing, approving and evaluating them (ADR 0016).

Every change adds a `cv_tailorings` revision; the latest revision of a job counts. A revision is
checked against the active profile version when it is saved, and always evaluated against that
same version later, so a tailored CV can be rebuilt exactly. Logs carry ids and counts only.
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from jaa.core.clock import utcnow
from jaa.core.errors import TailoringError
from jaa.core.models import CvTailoring, FactState, Job, JsonValue, ProfileFact, TailoringOrigin
from jaa.documents import tailoring as tailor
from jaa.matching.requirements import RequirementKind, extract_requirements
from jaa.storage import matches
from jaa.storage.profile import active_version_id, version_fact_ids
from jaa.storage.repository import cv_tailorings, jobs, profile_facts

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class Evaluated:
    job: Job
    row: CvTailoring
    plan: tailor.Plan
    result: tailor.TailoringResult


def _version(conn: sqlite3.Connection) -> int:
    version = active_version_id(conn)
    if version is None:
        raise TailoringError(
            "no confirmed profile yet: confirm facts with `jaa profile confirm` before tailoring"
        )
    return version


def _facts(conn: sqlite3.Connection, version: int) -> tuple[list[ProfileFact], list[ProfileFact]]:
    """The confirmed facts of `version`, and the facts that are unreviewed now."""
    wanted = set(version_fact_ids(conn, version))
    everything = profile_facts.list(conn)
    confirmed = [f for f in everything if f.id in wanted]
    unreviewed = [f for f in everything if f.state is FactState.UNREVIEWED]
    return confirmed, unreviewed


def job_terms(job: Job) -> frozenset[str]:
    requirements = extract_requirements(job).requirements
    return tailor.job_terms(
        r.text for r in requirements if r.kind in (RequirementKind.SKILL, RequirementKind.LANGUAGE)
    )


def _approvals(row: CvTailoring) -> frozenset[str]:
    return frozenset(a for a in row.approvals if isinstance(a, str))


def _evaluate(
    conn: sqlite3.Connection,
    job: Job,
    plan: tailor.Plan,
    version: int,
    approvals: frozenset[str],
) -> tailor.TailoringResult:
    confirmed, unreviewed = _facts(conn, version)
    return tailor.tailor(
        plan,
        confirmed,
        unreviewed=unreviewed,
        job_terms=job_terms(job),
        approvals=approvals,
    )


def latest(conn: sqlite3.Connection, job_id: int) -> CvTailoring | None:
    found = cv_tailorings.list(conn, job_id=job_id)
    return found[-1] if found else None


def revisions(conn: sqlite3.Connection, job_id: int) -> list[CvTailoring]:
    return cv_tailorings.list(conn, job_id=job_id)


def get_revision(conn: sqlite3.Connection, job_id: int, revision: int | None) -> CvTailoring:
    """A revision of a job's plan; the latest one when `revision` is None."""
    found = cv_tailorings.list(conn, job_id=job_id)
    if not found:
        raise TailoringError(
            f"job {job_id} has no tailored CV yet; run `jaa cv tailor propose {job_id}`"
        )
    if revision is None:
        return found[-1]
    for row in found:
        if row.revision == revision:
            return row
    raise TailoringError(f"job {job_id} has no tailoring revision {revision}")


def _save(
    conn: sqlite3.Connection,
    job: Job,
    plan: tailor.Plan,
    origin: TailoringOrigin,
    *,
    approvals: frozenset[str],
    job_match_id: int | None,
    at: datetime,
) -> Evaluated:
    version = _version(conn)
    result = _evaluate(conn, job, plan, version, approvals)
    # Keep only approvals that still match an edit or paragraph of this plan.
    current = {c.fingerprint for c in (*result.edit_checks, *result.paragraph_checks)}
    kept = sorted(approvals & current)
    with conn:
        previous = latest(conn, job.id or 0)
        row = cv_tailorings.insert(
            conn,
            CvTailoring(
                job_id=job.id or 0,
                revision=(previous.revision + 1) if previous else 1,
                profile_version_id=version,
                job_match_id=job_match_id,
                origin=origin,
                plan=plan.to_json(),
                checks=[
                    *(c.to_json() for c in result.edit_checks),
                    *({"paragraph": True, **c.to_json()} for c in result.paragraph_checks),
                ],
                approvals=list[JsonValue](kept),
                blocked=result.blocked,
                method=tailor.METHOD,
                created_at=at,
            ),
        )
    log.info(
        "saved cv tailoring job=%s revision=%s origin=%s edits=%d blocked=%s",
        job.id,
        row.revision,
        origin.value,
        len(plan.edits),
        result.blocked,
    )
    return Evaluated(job=job, row=row, plan=plan, result=result)


def propose(conn: sqlite3.Connection, job_id: int, *, at: datetime | None = None) -> Evaluated:
    """Propose a plan for a job from its match, matching it again first if the match is outdated."""
    at = at or utcnow()
    job = jobs.get(conn, job_id)
    version = _version(conn)
    found = matches.latest_match(conn, job_id)
    if found is None or matches.outdated_reasons(found, job, version):
        [stored] = matches.match_jobs(conn, [job_id], at=at)
        found = stored.match
    confirmed, _ = _facts(conn, version)
    plan = tailor.propose(confirmed, found.evidence)
    return _save(
        conn,
        job,
        plan,
        TailoringOrigin.PROPOSED,
        approvals=frozenset(),
        job_match_id=found.id,
        at=at,
    )


def save_edited(
    conn: sqlite3.Connection, job_id: int, plan: tailor.Plan, *, at: datetime | None = None
) -> Evaluated:
    """Save the user's edited plan as a new revision.

    Approvals carry over only for edits that did not change.
    """
    job = jobs.get(conn, job_id)
    previous = latest(conn, job_id)
    return _save(
        conn,
        job,
        plan,
        TailoringOrigin.EDITED,
        approvals=_approvals(previous) if previous else frozenset(),
        job_match_id=previous.job_match_id if previous else None,
        at=at or utcnow(),
    )


def approve(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    edits: Sequence[int] = (),
    paragraphs: Sequence[int] = (),
    at: datetime | None = None,
) -> Evaluated:
    """Approve blocked edits or cover letter paragraphs (numbers from 1) despite their issues.

    Only issues the user may approve (unsupported wording, no evidence, unreviewed facts) can
    be approved; an edit on an unknown target stays blocked.
    """
    current = evaluate(conn, job_id)
    checks = {("edit", c.number): c for c in current.result.edit_checks} | {
        ("paragraph", c.number): c for c in current.result.paragraph_checks
    }
    approved = set(_approvals(current.row))
    for kind, numbers in (("edit", edits), ("paragraph", paragraphs)):
        for number in numbers:
            check = checks.get((kind, number))
            if check is None:
                raise TailoringError(f"there is no {kind} {number} in the latest plan")
            if not check.issues:
                raise TailoringError(f"{kind} {number} has no issues to approve")
            if not check.approvable:
                raise TailoringError(f"{kind} {number} cannot be approved; change or remove it")
            approved.add(check.fingerprint)
    return _save(
        conn,
        current.job,
        current.plan,
        TailoringOrigin.APPROVED,
        approvals=frozenset(approved),
        job_match_id=current.row.job_match_id,
        at=at or utcnow(),
    )


def evaluate(conn: sqlite3.Connection, job_id: int, revision: int | None = None) -> Evaluated:
    """A stored revision checked and applied against the profile version it was saved with."""
    job = jobs.get(conn, job_id)
    row = get_revision(conn, job_id, revision)
    plan = tailor.plan_from_json(row.plan)
    result = _evaluate(conn, job, plan, row.profile_version_id, _approvals(row))
    return Evaluated(job=job, row=row, plan=plan, result=result)
