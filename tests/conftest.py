import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jaa.core import models as m
from jaa.storage import repository as repo
from jaa.storage.db import open_database
from jaa.storage.jobs import add_job

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)

# Synthetic values only. Distinctive strings let tests check they never reach logs.
SYNTHETIC_EMAIL = "synthetic.candidate@example.test"
SYNTHETIC_TITLE = "Principal Widget Engineer"
SYNTHETIC_ANSWER = "Requires visa sponsorship: synthetic-answer"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = open_database(tmp_path / "jaa.sqlite3")
    yield db
    db.close()


@dataclass(frozen=True)
class Sample:
    run: m.ImportRun
    fact: m.ProfileFact
    email: m.ProfileFact
    version: m.ProfileVersion
    answer: m.Answer
    job: m.Job
    application: m.Application
    artifact: m.DocumentArtifact


def ident(x: int | None) -> int:
    assert x is not None
    return x


def populate(conn: sqlite3.Connection) -> Sample:
    """One row in every table, including link tables."""
    run = repo.import_runs.insert(
        conn,
        m.ImportRun(
            source_type=m.ImportSourceType.PDF,
            source_path="/synthetic/cv.pdf",
            source_sha256="0" * 64,
            imported_at=T0,
            status=m.ImportStatus.PARTIAL,
            summary={"facts": 2, "pages": 1},
            errors=["page 2: unreadable table"],
            retention=m.RetentionChoice.KEEP,
        ),
    )
    fact = repo.profile_facts.insert(
        conn,
        m.ProfileFact(
            field_key="experience.0.title",
            value_raw=SYNTHETIC_TITLE,
            value_normalized={"title": SYNTHETIC_TITLE, "seniority": "principal"},
            origin=m.FactOrigin.IMPORT,
            confidence=0.8,
            import_run_id=run.id,
            source_location="page 1",
            source_excerpt=f"... {SYNTHETIC_TITLE} ...",
            created_at=T0,
            updated_at=T0,
        ),
    )
    email = repo.profile_facts.insert(
        conn,
        m.ProfileFact(
            field_key="contact.email",
            value_raw=SYNTHETIC_EMAIL,
            value_normalized=SYNTHETIC_EMAIL,
            state=m.FactState.CONFIRMED,
            origin=m.FactOrigin.USER,
            is_sensitive=True,
            created_at=T0,
            updated_at=T0,
            confirmed_at=T0,
        ),
    )
    version = repo.profile_versions.insert(conn, m.ProfileVersion(created_at=T0, note="initial"))
    conn.execute(
        "INSERT INTO profile_version_facts VALUES (?, ?)", (ident(version.id), ident(email.id))
    )
    repo.profile_fact_events.insert(
        conn,
        m.ProfileFactEvent(
            fact_id=ident(email.id),
            from_state=None,
            to_state=m.FactState.CONFIRMED,
            occurred_at=T0,
            profile_version_id=version.id,
        ),
    )
    repo.profiles.insert(
        conn, m.Profile(active_version_id=version.id, created_at=T0, updated_at=T0)
    )
    repo.preferences.insert(
        conn,
        m.Preference(
            key="salary.min",
            value={"amount": 1, "currency": "EUR"},
            is_sensitive=True,
            updated_at=T0,
        ),
    )
    repo.preference_events.insert(
        conn,
        m.PreferenceEvent(
            preference_key="salary.min",
            status=m.PreferenceStatus.ANSWERED,
            value={"amount": 1, "currency": "EUR"},
            is_sensitive=True,
            occurred_at=T0,
        ),
    )
    answer = repo.answers.insert(
        conn,
        m.Answer(
            question_key="sponsorship.required",
            question_text="Do you require sponsorship?",
            value=SYNTHETIC_ANSWER,
            reuse_policy=m.ReusePolicy.REUSE_WITH_REVIEW,
            is_sensitive=True,
            created_at=T0,
            confirmed_at=T0,
            expires_at=datetime(2027, 4, 14, tzinfo=UTC),
        ),
    )
    conn.execute("INSERT INTO answer_sources VALUES (?, ?)", (ident(answer.id), ident(email.id)))
    job = add_job(
        conn,
        m.Job(
            source="greenhouse:example",
            source_job_id="123",
            canonical_url="https://boards.example.test/jobs/123",
            employer="Example Co",
            title="Widget Engineer",
            location="Remote, EU",
            description="Build widgets.",
            posted_at=T0,
            first_seen_at=T0,
            last_seen_at=T0,
            requirements={"required": ["widgets"], "preferred": []},
            status_changed_at=T0,
        ),
    )
    repo.job_matches.insert(
        conn,
        m.JobMatch(
            job_id=ident(job.id),
            profile_version_id=ident(version.id),
            score=0.75,
            evidence=[{"fact_id": fact.id, "requirement": "widgets"}],
            gaps=["kubernetes"],
            created_at=T0,
        ),
    )
    application = repo.applications.insert(
        conn,
        m.Application(
            job_id=ident(job.id),
            created_at=T0,
            submitted_at=T0,
            submission_confirmed_by=m.SubmissionConfirmation.USER,
            notes="applied via employer page",
        ),
    )
    conn.execute(
        "INSERT INTO application_answers VALUES (?, ?)", (ident(application.id), ident(answer.id))
    )
    artifact = repo.document_artifacts.insert(
        conn,
        m.DocumentArtifact(
            kind=m.ArtifactKind.CV_PDF,
            path="/synthetic/out/cv.pdf",
            job_id=job.id,
            application_id=application.id,
            profile_version_id=version.id,
            template_version="basic@1",
            content_sha256="f" * 64,
            manifest={"template": "basic", "profile_version": version.id},
            created_at=T0,
        ),
    )
    repo.template_candidates.insert(
        conn,
        m.TemplateCandidate(
            import_run_id=run.id,
            main_file="cv.tex",
            files={
                "cv.tex": "\\documentclass{article}\n\\begin{document}Synthetic\\end{document}\n"
            },
            document_class="article",
            packages=["geometry"],
            content_sha256="e" * 64,
            registered_at=T0,
        ),
    )
    conn.commit()
    return Sample(run, fact, email, version, answer, job, application, artifact)


@pytest.fixture
def sample(conn: sqlite3.Connection) -> Sample:
    return populate(conn)
