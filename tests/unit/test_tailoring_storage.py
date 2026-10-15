import logging
import sqlite3
from datetime import UTC, datetime

import pytest

from jaa.connectors.base import JobListing
from jaa.core.errors import TailoringError
from jaa.core.job_details import JobDetails, ListedSkill
from jaa.core.models import TailoringOrigin
from jaa.documents.tailoring import Edit, EditKind, Plan
from jaa.storage import listings, matches
from jaa.storage import tailoring as service
from jaa.storage.profile import active_version_id, confirm_facts
from jaa.storage.repository import job_matches, profile_facts
from tests.fixtures.cv_profile import store_profile

T1 = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
BULLET_1 = "experience.example-co.description[1]"
REWORDED = "Led the migration of widget pipelines to Kubernetes clusters."


def _job(conn: sqlite3.Connection) -> int:
    listing = JobListing(
        source_job_id="job-1",
        url="https://jobs.example.test/1",
        title="Widget Engineer",
        company="Example Widgets",
        description="Requirements:\n- React Native\n- Python",
        details=JobDetails(skills=(ListedSkill(name="Kubernetes"), ListedSkill(name="Python"))),
    )
    changes = listings.store_listings(conn, "test", "all", [listing], at=T1)
    [job_id] = changes.new
    return job_id


def _fact_id(conn: sqlite3.Connection, key: str) -> int:
    [fact] = [f for f in profile_facts.list(conn) if f.field_key == key]
    assert fact.id is not None
    return fact.id


def test_tailoring_needs_a_confirmed_profile(conn: sqlite3.Connection) -> None:
    job_id = _job(conn)
    with pytest.raises(TailoringError, match="no confirmed profile"):
        service.propose(conn, job_id)
    with pytest.raises(TailoringError, match="no tailored CV yet"):
        service.evaluate(conn, job_id)


def test_propose_matches_first_and_stores_a_revision(conn: sqlite3.Connection) -> None:
    store_profile(conn)
    job_id = _job(conn)
    proposed = service.propose(conn, job_id, at=T1)
    row = proposed.row
    assert (row.revision, row.origin) == (1, TailoringOrigin.PROPOSED)
    assert row.profile_version_id == active_version_id(conn)
    [match] = job_matches.list(conn, job_id=job_id)
    assert row.job_match_id == match.id
    targets = [e.target for e in proposed.plan.edits]
    assert targets[0] == "skills.python"
    assert "skills.react-native" in targets
    assert not row.blocked
    # A current match is reused rather than recomputed.
    service.propose(conn, job_id, at=T1)
    assert len(job_matches.list(conn, job_id=job_id)) == 1
    assert [r.revision for r in service.revisions(conn, job_id)] == [1, 2]


def test_edit_approve_and_carry_over(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store_profile(conn)
    job_id = _job(conn)
    rewrite = Edit(kind=EditKind.REWRITE, target=BULLET_1, text=REWORDED)
    hide = Edit(kind=EditKind.HIDE, target="skills.c-plus-plus")
    edited = service.save_edited(conn, job_id, Plan(edits=(rewrite, hide)), at=T1)
    assert edited.row.origin is TailoringOrigin.EDITED and edited.row.blocked
    assert edited.row.checks[0] == {
        "number": 1,
        "codes": ["unsupported_terms"],
        "approved": False,
        "blocked": True,
        "fact_ids": [_fact_id(conn, "experience.example-co.description")],
    }

    with pytest.raises(TailoringError, match="no issues to approve"):
        service.approve(conn, job_id, edits=[2])
    with pytest.raises(TailoringError, match="no edit 3"):
        service.approve(conn, job_id, edits=[3])
    approved = service.approve(conn, job_id, edits=[1], at=T1)
    assert approved.row.origin is TailoringOrigin.APPROVED
    assert not approved.row.blocked
    assert approved.result.content.experience[0].items[0] == REWORDED

    # Unchanged edits keep their approval; a changed one needs a new approval.
    kept = service.save_edited(conn, job_id, Plan(edits=(hide, rewrite)), at=T1)
    assert not kept.row.blocked
    changed = Edit(kind=EditKind.REWRITE, target=BULLET_1, text=REWORDED + " Twice.")
    again = service.save_edited(conn, job_id, Plan(edits=(changed,)), at=T1)
    assert again.row.blocked and again.row.approvals == []
    assert [r.revision for r in service.revisions(conn, job_id)] == [1, 2, 3, 4]
    assert REWORDED not in caplog.text and "Kubernetes" not in caplog.text


def test_unknown_targets_cannot_be_approved(conn: sqlite3.Connection) -> None:
    store_profile(conn)
    job_id = _job(conn)
    bad = Edit(kind=EditKind.REWRITE, target="experience.example-co.dates", text="2010")
    service.save_edited(conn, job_id, Plan(edits=(bad,)))
    with pytest.raises(TailoringError, match="cannot be approved"):
        service.approve(conn, job_id, edits=[1])


def test_revisions_are_append_only(conn: sqlite3.Connection) -> None:
    store_profile(conn)
    job_id = _job(conn)
    service.propose(conn, job_id)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE cv_tailorings SET blocked = 1")


def test_revision_is_evaluated_against_its_own_profile_version(conn: sqlite3.Connection) -> None:
    store_profile(conn, unreviewed={"skills.go": "Go"})
    job_id = _job(conn)
    saved = service.save_edited(
        conn, job_id, Plan(edits=(Edit(kind=EditKind.EMPHASIZE, target="skills.python"),))
    )
    confirm_facts(conn, [_fact_id(conn, "skills.go")])
    assert active_version_id(conn) != saved.row.profile_version_id
    later = service.evaluate(conn, job_id)
    assert "Go" not in later.result.content.skills
    assert later.result.content == saved.result.content
    # The match it was based on is outdated now, so a new proposal matches again.
    service.propose(conn, job_id)
    assert len(job_matches.list(conn, job_id=job_id)) == 1
    found = matches.latest_match(conn, job_id)
    assert found is not None and found.profile_version_id == active_version_id(conn)
