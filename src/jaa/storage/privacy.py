"""Deleting what the user asks to delete (T19, ADR 0021).

Nothing here runs on its own: jaa never deletes records automatically. Each function deletes
what the user named, in one transaction, and files only when they are still exactly what jaa
recorded, so a file the user changed or replaced since is never lost.
"""

import hashlib
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jaa.core.clock import utcnow
from jaa.core.errors import DeletionError, NotFoundError
from jaa.core.models import ImportRun
from jaa.storage.repository import import_runs, jobs

log = logging.getLogger(__name__)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _combined_sha256(hashes: Sequence[str]) -> str:
    """The hash an import run records: the file's own, or one over several files' hashes."""
    if len(hashes) == 1:
        return hashes[0]
    return hashlib.sha256("\n".join(sorted(hashes)).encode()).hexdigest()


def import_source_paths(run: ImportRun) -> list[Path]:
    """The user's files an import read: every file of a multi-file import, else the one path."""
    listed = run.summary.get("source_files")
    if isinstance(listed, list) and listed:
        return [Path(str(p)) for p in listed]
    return [Path(run.source_path)] if run.source_path else []


def delete_import_source(
    conn: sqlite3.Connection, run_id: int, *, at: datetime | None = None
) -> list[Path]:
    """Delete the file(s) an import read, if they are unchanged since. Returns the paths.

    Every file is checked before any is deleted, so a mismatch deletes nothing. The facts
    imported from them stay; `profile.delete_import` deletes those.
    """
    run = import_runs.get(conn, run_id)
    if run.source_deleted_at is not None:
        raise DeletionError(f"the source of import #{run_id} was already deleted")
    paths = import_source_paths(run)
    if not paths or run.source_sha256 is None:
        raise DeletionError(f"import #{run_id} did not read a file")
    hashes: list[str] = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise DeletionError(f"{path} is no longer a file there; nothing was deleted")
        try:
            hashes.append(_file_sha256(path))
        except OSError as exc:
            raise DeletionError(f"cannot read {path} to check it: {exc}") from exc
    if _combined_sha256(hashes) != run.source_sha256:
        raise DeletionError(
            f"the file(s) of import #{run_id} changed since they were imported; nothing deleted"
        )
    for path in paths:
        try:
            path.unlink()
        except OSError as exc:
            raise DeletionError(f"cannot delete {path}: {exc}") from exc
    with conn:
        import_runs.update(conn, run_id, source_deleted_at=at or utcnow())
    log.info("deleted import source run=%s files=%d", run_id, len(paths))
    return paths


@dataclass(frozen=True, slots=True)
class DeletedJobs:
    jobs: int
    files_deleted: tuple[Path, ...]
    # Generated files outside the data directory, or changed since jaa wrote them.
    files_kept: tuple[Path, ...]


def _job_documents(conn: sqlite3.Connection, job_ids: Sequence[int]) -> list[sqlite3.Row]:
    marks = ", ".join("?" for _ in job_ids)
    return conn.execute(
        "SELECT id, path, content_sha256 FROM document_artifacts "  # noqa: S608 - placeholders only
        f"WHERE job_id IN ({marks}) OR application_id IN "
        f"(SELECT id FROM applications WHERE job_id IN ({marks}))",
        [*job_ids, *job_ids],
    ).fetchall()


def _remove_generated(path: Path, sha256: str, data_dir: Path) -> bool | None:
    """Delete a file jaa generated if it is inside the data directory and unchanged.

    Returns True when deleted, False when kept, None when it is already gone.
    """
    try:
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file():
            return False
        if not path.resolve().is_relative_to(data_dir.resolve()):
            return False
        if _file_sha256(path) != sha256:
            return False
        path.unlink()
    except OSError:
        return False
    return True


def delete_jobs(conn: sqlite3.Connection, job_ids: Sequence[int], data_dir: Path) -> DeletedJobs:
    """Delete jobs with their application, history, matches, tailored CVs and questions.

    Generated documents for them are deleted when they are inside the data directory and
    unchanged; others are reported as kept. Saved answers stay (they are reusable); the user
    deletes those with `answers.forget_answer`. A job still listed by its source comes back as
    a new job the next time that source is fetched.
    """
    if not job_ids:
        raise NotFoundError("no job ids given")
    ids = list(dict.fromkeys(job_ids))
    with conn:
        for job_id in ids:
            jobs.get(conn, job_id)
        documents = _job_documents(conn, ids)
        conn.executemany(
            "DELETE FROM document_artifacts WHERE id = ?", [(r["id"],) for r in documents]
        )
        # Tailorings first, so no cascade order can clear a tailoring's match link: that is an
        # update, which the append-only trigger refuses.
        conn.executemany("DELETE FROM cv_tailorings WHERE job_id = ?", [(i,) for i in ids])
        conn.executemany("DELETE FROM jobs WHERE id = ?", [(i,) for i in ids])
    deleted: list[Path] = []
    kept: list[Path] = []
    for row in documents:
        path = Path(str(row["path"]))
        outcome = _remove_generated(path, str(row["content_sha256"]), data_dir)
        if outcome is not None:
            (deleted if outcome else kept).append(path)
    log.info("deleted jobs=%d files=%d kept=%d", len(ids), len(deleted), len(kept))
    return DeletedJobs(len(ids), tuple(deleted), tuple(kept))
