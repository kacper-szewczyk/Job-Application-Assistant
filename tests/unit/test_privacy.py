"""Deleting on request and restoring (T19, ADR 0021). Nothing is deleted automatically."""

import hashlib
import logging
import sqlite3
import stat
from pathlib import Path

import pytest

from jaa.core import models as m
from jaa.core.applications import ARCHIVED, is_archived
from jaa.core.errors import (
    DatabaseNotEmptyError,
    DeletionError,
    ExportFormatError,
    MigrationError,
    NotFoundError,
    RestoreError,
    StorageError,
)
from jaa.storage import privacy
from jaa.storage import repository as repo
from jaa.storage.db import connect, open_database
from jaa.storage.export import (
    RestoreKind,
    backup_database,
    export_data,
    export_json,
    has_records,
    restore,
)
from jaa.storage.profile import active_version_id, delete_facts, delete_import, version_fact_ids
from tests.conftest import SYNTHETIC_EMAIL, SYNTHETIC_TITLE, T0, Sample, ident, populate


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608 - test


def _dump(conn: sqlite3.Connection) -> str:
    return "\n".join(conn.iterdump())


# --- archived ----------------------------------------------------------------------------


def test_finished_jobs_are_archived_not_deleted() -> None:
    assert {s for s in m.JobStatus if is_archived(s)} == {
        m.JobStatus.REJECTED,
        m.JobStatus.WITHDRAWN,
        m.JobStatus.CLOSED,
    }
    assert set(m.JobStatus) >= ARCHIVED


# --- facts and imports -------------------------------------------------------------------


def test_deleting_an_unreviewed_fact_removes_its_value(
    conn: sqlite3.Connection, sample: Sample
) -> None:
    before = active_version_id(conn)
    result = delete_facts(conn, [ident(sample.fact.id)], at=T0)
    assert (result.facts, result.version) == (1, None)
    assert active_version_id(conn) == before
    assert SYNTHETIC_TITLE not in _dump(conn)


def test_deleting_a_confirmed_fact_removes_it_from_history_and_versions(
    conn: sqlite3.Connection, sample: Sample
) -> None:
    email = ident(sample.email.id)
    result = delete_facts(conn, [email], at=T0)
    assert result.version is not None
    assert active_version_id(conn) == result.version.id
    assert version_fact_ids(conn, ident(sample.version.id)) == []
    assert _count(conn, "profile_fact_events") == 0
    assert _count(conn, "answer_sources") == 0
    # The answer it backed is the user's own record and stays until they forget it.
    assert _count(conn, "answers") == 1
    assert SYNTHETIC_EMAIL not in _dump(conn)


def test_deleting_unknown_facts_changes_nothing(conn: sqlite3.Connection, sample: Sample) -> None:
    with pytest.raises(NotFoundError):
        delete_facts(conn, [ident(sample.fact.id), 999])
    assert _count(conn, "profile_facts") == 2


def test_deleting_an_import_removes_its_facts_and_templates(
    conn: sqlite3.Connection, sample: Sample
) -> None:
    run_id = ident(sample.run.id)
    result = delete_import(conn, run_id, at=T0)
    assert result.facts == 1
    assert _count(conn, "import_runs") == 0
    assert _count(conn, "template_candidates") == 0
    # Facts the user entered are not part of the import.
    assert [f.id for f in repo.profile_facts.list(conn)] == [sample.email.id]
    with pytest.raises(NotFoundError):
        delete_import(conn, run_id)


def _run_for(conn: sqlite3.Connection, first: Path, *more: Path) -> int:
    files = (first, *more)
    hashes = sorted(hashlib.sha256(f.read_bytes()).hexdigest() for f in files)
    combined = (
        hashes[0] if len(hashes) == 1 else hashlib.sha256("\n".join(hashes).encode()).hexdigest()
    )
    run = repo.import_runs.insert(
        conn,
        m.ImportRun(
            source_type=m.ImportSourceType.LINKEDIN_ARCHIVE
            if len(files) > 1
            else m.ImportSourceType.PDF,
            source_path=str(first),
            source_sha256=combined,
            imported_at=T0,
            status=m.ImportStatus.SUCCEEDED,
            summary={"source_files": [str(f) for f in files]} if len(files) > 1 else {},
            retention=m.RetentionChoice.KEEP,
        ),
    )
    conn.commit()
    return ident(run.id)


def test_import_source_is_deleted_when_unchanged(conn: sqlite3.Connection, tmp_path: Path) -> None:
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF synthetic")
    run_id = _run_for(conn, cv)
    assert privacy.delete_import_source(conn, run_id, at=T0) == [cv]
    assert not cv.exists()
    assert repo.import_runs.get(conn, run_id).source_deleted_at == T0
    with pytest.raises(DeletionError, match="already deleted"):
        privacy.delete_import_source(conn, run_id)


def test_import_sources_are_all_checked_before_any_is_deleted(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    first, second = tmp_path / "Positions.csv", tmp_path / "Skills.csv"
    first.write_text("Title\nsynthetic\n")
    second.write_text("Name\nsynthetic\n")
    run_id = _run_for(conn, first, second)
    second.write_text("Name\nchanged by the user\n")
    with pytest.raises(DeletionError, match="changed since"):
        privacy.delete_import_source(conn, run_id)
    assert first.exists() and second.exists()
    assert repo.import_runs.get(conn, run_id).source_deleted_at is None


def test_import_source_replaced_by_a_link_is_not_followed(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF synthetic")
    run_id = _run_for(conn, cv)
    elsewhere = tmp_path / "elsewhere.pdf"
    cv.rename(elsewhere)
    cv.symlink_to(elsewhere)
    with pytest.raises(DeletionError, match="no longer a file"):
        privacy.delete_import_source(conn, run_id)
    assert elsewhere.exists()


# --- jobs --------------------------------------------------------------------------------


def _artifact(conn: sqlite3.Connection, job_id: int, path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    repo.document_artifacts.insert(
        conn,
        m.DocumentArtifact(
            kind=m.ArtifactKind.CV_PDF,
            path=str(path),
            job_id=job_id,
            content_sha256=hashlib.sha256(content).hexdigest(),
            created_at=T0,
        ),
    )


def test_deleting_a_job_removes_its_records_and_generated_files(
    conn: sqlite3.Connection, sample: Sample, tmp_path: Path
) -> None:
    job_id = ident(sample.job.id)
    # A tailoring that points at a match: the cascade must not trip the append-only trigger.
    match = repo.job_matches.list(conn, job_id=job_id)[0]
    repo.cv_tailorings.insert(
        conn,
        m.CvTailoring(
            job_id=job_id,
            revision=2,
            profile_version_id=ident(sample.version.id),
            job_match_id=match.id,
            origin=m.TailoringOrigin.PROPOSED,
            method="tailoring-v1",
            created_at=T0,
        ),
    )
    data_dir = tmp_path / "data"
    inside = data_dir / "documents" / "job-1" / "cv.pdf"
    changed = data_dir / "documents" / "job-1" / "edited.pdf"
    outside = tmp_path / "Desktop" / "cv.pdf"
    _artifact(conn, job_id, inside, b"%PDF generated")
    _artifact(conn, job_id, changed, b"%PDF generated")
    changed.write_bytes(b"%PDF edited by the user")
    _artifact(conn, job_id, outside, b"%PDF generated")
    conn.commit()

    result = privacy.delete_jobs(conn, [job_id, job_id], data_dir)

    assert result.jobs == 1
    assert result.files_deleted == (inside,)
    assert set(result.files_kept) == {changed, outside}
    assert not inside.exists() and changed.exists() and outside.exists()
    for table in (
        "applications",
        "application_answers",
        f"job_status_events WHERE job_id = {job_id}",
        "job_matches",
        "cv_tailorings",
        "job_questions",
        "job_duplicates",
        "document_artifacts",
    ):
        assert _count(conn, table) == 0, table
    assert job_id not in [j.id for j in repo.jobs.list(conn)]
    assert _count(conn, "answers") == 1


def test_deleting_unknown_jobs_changes_nothing(
    conn: sqlite3.Connection, sample: Sample, tmp_path: Path
) -> None:
    with pytest.raises(NotFoundError):
        privacy.delete_jobs(conn, [ident(sample.job.id), 999], tmp_path)
    assert _count(conn, "jobs") == 2


# --- backup and export guard the live database --------------------------------------------


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_backup_and_export_never_write_over_the_database(
    conn: sqlite3.Connection, sample: Sample, tmp_path: Path, suffix: str
) -> None:
    target = tmp_path / f"jaa.sqlite3{suffix}"
    with pytest.raises(StorageError, match="own database"):
        backup_database(conn, target, overwrite=True)
    with pytest.raises(StorageError, match="own database"):
        export_json(conn, target, overwrite=True)
    assert has_records(conn)


def test_backup_and_export_refuse_directories_and_missing_folders(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    with pytest.raises(StorageError, match="directory"):
        export_json(conn, tmp_path)
    with pytest.raises(StorageError, match="does not exist"):
        backup_database(conn, tmp_path / "missing" / "b.sqlite3")


# --- restore -----------------------------------------------------------------------------


@pytest.fixture
def saved(tmp_path: Path) -> tuple[Path, Path, str]:
    """A populated database's backup and export, and its records, made elsewhere."""
    folder = tmp_path / "source"
    folder.mkdir()
    conn = open_database(folder / "jaa.sqlite3")
    populate(conn)
    backup = backup_database(conn, tmp_path / "backup.sqlite3")
    export = export_json(conn, tmp_path / "export.json")
    records = str(export_data(conn)["tables"])
    conn.close()
    return backup, export, records


def _records(database: Path) -> str:
    conn = connect(database)
    try:
        return str(export_data(conn)["tables"])
    finally:
        conn.close()


@pytest.mark.parametrize("kind", list(RestoreKind))
def test_restore_into_a_new_data_directory(
    tmp_path: Path, saved: tuple[Path, Path, str], kind: RestoreKind
) -> None:
    database = tmp_path / "data" / "jaa.sqlite3"
    database.parent.mkdir()
    result = restore(database, saved[0] if kind is RestoreKind.BACKUP else saved[1])
    assert result.kind is kind
    assert result.rows > 0 and result.previous is None
    assert _records(database) == saved[2]
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert [p.name for p in database.parent.iterdir()] == ["jaa.sqlite3"]


def test_restore_never_replaces_records_without_being_asked(
    tmp_path: Path, saved: tuple[Path, Path, str]
) -> None:
    database = tmp_path / "data" / "jaa.sqlite3"
    database.parent.mkdir()
    conn = open_database(database)
    repo.saved_searches.insert(
        conn, m.SavedSearch(name="mine", query={}, created_at=T0, updated_at=T0)
    )
    conn.commit()
    conn.close()
    mine = _records(database)

    with pytest.raises(DatabaseNotEmptyError):
        restore(database, saved[0])
    assert _records(database) == mine

    result = restore(database, saved[0], replace=True)
    assert _records(database) == saved[2]
    assert result.previous is not None
    assert result.previous.parent == database.parent / "backups"
    assert _records(result.previous) == mine
    assert stat.S_IMODE(result.previous.stat().st_mode) == 0o600
    assert sorted(p.name for p in database.parent.iterdir()) == ["backups", "jaa.sqlite3"]


def test_failed_restore_leaves_the_database_as_it_was(
    tmp_path: Path, saved: tuple[Path, Path, str]
) -> None:
    database = tmp_path / "data" / "jaa.sqlite3"
    database.parent.mkdir()
    conn = open_database(database)
    populate(conn)
    conn.close()
    before = _records(database)

    bad = tmp_path / "bad.json"
    bad.write_text('{"format": "jaa-export", "format_version": 1, "schema_version": 1}')
    with pytest.raises(ExportFormatError):
        restore(database, bad, replace=True)
    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    with pytest.raises(RestoreError):
        restore(database, garbage, replace=True)

    assert _records(database) == before
    assert sorted(p.name for p in database.parent.iterdir()) == ["jaa.sqlite3"]


def test_restore_refuses_a_database_from_a_newer_jaa(
    tmp_path: Path, saved: tuple[Path, Path, str]
) -> None:
    newer = sqlite3.connect(saved[0])
    newer.execute(
        "INSERT INTO schema_migrations VALUES (9999, 'future', 'x', '2030-01-01T00:00:00+00:00')"
    )
    newer.commit()
    newer.close()
    database = tmp_path / "data" / "jaa.sqlite3"
    database.parent.mkdir()
    with pytest.raises(MigrationError, match="does not know"):
        restore(database, saved[0])
    assert not database.exists()


def test_restore_refuses_other_files(tmp_path: Path) -> None:
    database = tmp_path / "data" / "jaa.sqlite3"
    database.parent.mkdir()
    other = tmp_path / "other.sqlite3"
    plain = sqlite3.connect(other)
    plain.execute("CREATE TABLE t (x)")
    plain.close()
    with pytest.raises(RestoreError, match="not a jaa backup"):
        restore(database, other)
    with pytest.raises(RestoreError, match="not found"):
        restore(database, tmp_path / "missing.json")
    with pytest.raises(RestoreError, match="not a regular file"):
        restore(database, tmp_path)
    open_database(database).close()
    with pytest.raises(RestoreError, match="own database"):
        restore(database, database)


def test_restore_does_not_change_the_backup_it_reads(
    tmp_path: Path, saved: tuple[Path, Path, str]
) -> None:
    before = saved[0].read_bytes()
    database = tmp_path / "data" / "jaa.sqlite3"
    database.parent.mkdir()
    restore(database, saved[0])
    assert saved[0].read_bytes() == before


def test_deletion_and_restore_log_counts_not_values(
    tmp_path: Path, saved: tuple[Path, Path, str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="jaa")
    database = tmp_path / "data" / "jaa.sqlite3"
    database.parent.mkdir()
    restore(database, saved[1])
    conn = open_database(database)
    try:
        facts = [ident(f.id) for f in repo.profile_facts.list(conn)]
        delete_facts(conn, facts)
        privacy.delete_jobs(conn, [ident(j.id) for j in repo.jobs.list(conn)], tmp_path)
    finally:
        conn.close()
    assert caplog.records
    for value in (SYNTHETIC_EMAIL, SYNTHETIC_TITLE, "Example Co", "Widget Engineer"):
        assert value not in caplog.text
