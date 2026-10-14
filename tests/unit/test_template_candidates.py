import sqlite3
from datetime import UTC, datetime

import pytest

from jaa.core import models as m
from jaa.storage.repository import template_candidates
from jaa.storage.templates import list_template_candidates, register_template_candidate

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def candidate(text: str = "\\documentclass{article}") -> m.TemplateCandidate:
    return m.TemplateCandidate(
        main_file="cv.tex",
        files={"cv.tex": text},
        document_class="article",
        content_sha256=str(hash(text) % 10**12).rjust(64, "0"),
        registered_at=T0,
    )


def test_register_is_idempotent_per_content(conn: sqlite3.Connection) -> None:
    first, created = register_template_candidate(conn, candidate(), import_run_id=None)
    again, created_again = register_template_candidate(conn, candidate(), import_run_id=None)
    other, _ = register_template_candidate(conn, candidate("other"), import_run_id=None)
    assert created and not created_again
    assert again.id == first.id
    assert other.id != first.id
    assert [c.id for c in list_template_candidates(conn)] == [first.id, other.id]


def test_candidates_are_immutable(conn: sqlite3.Connection) -> None:
    stored, _ = register_template_candidate(conn, candidate(), import_run_id=None)
    assert stored.id is not None
    with pytest.raises(Exception, match="immutable"):
        template_candidates.update(conn, stored.id, files={"cv.tex": "changed"})


def test_repr_hides_the_source() -> None:
    assert "documentclass" not in repr(candidate())
