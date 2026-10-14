"""Template candidates: LaTeX CVs the user chose to keep as a starting template (ADR 0009).

Logging records ids only; the stored files hold the CV's own text.
"""

import logging
import sqlite3
from dataclasses import replace

from jaa.core.models import TemplateCandidate
from jaa.storage.repository import template_candidates

log = logging.getLogger(__name__)


def register_template_candidate(
    conn: sqlite3.Connection, candidate: TemplateCandidate, *, import_run_id: int | None
) -> tuple[TemplateCandidate, bool]:
    """Store a candidate unless identical files are already kept. Returns (candidate, created)."""
    existing = template_candidates.list(conn, content_sha256=candidate.content_sha256)
    if existing:
        return existing[0], False
    with conn:
        stored = template_candidates.insert(
            conn, replace(candidate, id=None, import_run_id=import_run_id)
        )
    log.info("registered template candidate id=%s import_run=%s", stored.id, import_run_id)
    return stored, True


def list_template_candidates(conn: sqlite3.Connection) -> list[TemplateCandidate]:
    return template_candidates.list(conn)
