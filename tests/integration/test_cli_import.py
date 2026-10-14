import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.core import models as m
from jaa.importers.cv_file import prepare_cv_import
from jaa.storage.db import open_database
from jaa.storage.profile import add_imported_facts
from jaa.storage.repository import import_runs, profile_facts
from tests.fixtures.cv_builders import (
    SAMPLE_CV,
    SAMPLE_EMAIL,
    SAMPLE_NAME,
    SAMPLE_PHONE,
    build_pdf,
    sample_docx,
    sample_pdf,
)

runner = CliRunner()
TITLE_KEY = "experience.example-widgets-ltd.title"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    env = {"JAA_DATA_DIR": str(tmp_path / "data"), "JAA_LOG_LEVEL": "DEBUG", "COLUMNS": "200"}
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    return env


@pytest.fixture
def db(env: dict[str, str]) -> Iterator[sqlite3.Connection]:
    conn = open_database(Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3")
    yield conn
    conn.close()


@pytest.fixture(params=["pdf", "docx"])
def cv_file(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    kind = str(request.param)
    path = tmp_path / f"synthetic-cv.{kind}"
    path.write_bytes(sample_pdf() if kind == "pdf" else sample_docx())
    return path


def test_import_cv_yields_reviewable_facts_with_excerpts(
    env: dict[str, str], db: sqlite3.Connection, cv_file: Path
) -> None:
    result = runner.invoke(app, ["import", "cv", str(cv_file)], env=env)
    assert result.exit_code == 0, result.output
    assert "Import #1" in result.output
    assert "partial" in result.output  # the Volunteering section is not imported
    assert "Volunteering" in result.output
    assert "23 new facts to review" in result.output

    facts = profile_facts.list(db)
    assert len(facts) == 23
    assert all(f.state is m.FactState.UNREVIEWED for f in facts)
    assert all(f.origin is m.FactOrigin.IMPORT and f.import_run_id == 1 for f in facts)
    assert all(f.source_excerpt and f.source_location for f in facts)
    by_key = {f.field_key: f for f in facts}
    assert by_key[TITLE_KEY].value_raw == "Senior Widget Engineer"
    assert by_key["contact.email"].is_sensitive
    assert by_key["identity.name"].is_sensitive
    assert not by_key["skills.python"].is_sensitive

    run = import_runs.get(db, 1)
    assert run.status is m.ImportStatus.PARTIAL
    assert run.source_type.value == cv_file.suffix[1:]
    assert run.source_path == str(cv_file.resolve())
    assert run.source_sha256 and len(run.source_sha256) == 64
    assert run.retention is m.RetentionChoice.KEEP
    assert run.summary["candidates"] == 23
    assert [e["code"] for e in run.errors if isinstance(e, dict)] == ["section_not_imported"]

    # The facts are visible in the T03 review workflow.
    listed = runner.invoke(app, ["profile", "facts", "--state", "unreviewed"], env=env)
    assert "skills.c-plus-plus" in listed.output


def test_reimport_skips_known_values_and_never_touches_confirmed_facts(
    env: dict[str, str], db: sqlite3.Connection, cv_file: Path
) -> None:
    assert runner.invoke(app, ["import", "cv", str(cv_file)], env=env).exit_code == 0
    title = profile_facts.list(db, field_key=TITLE_KEY)[0]
    assert runner.invoke(app, ["profile", "confirm", str(title.id)], env=env).exit_code == 0

    again = runner.invoke(app, ["import", "cv", str(cv_file)], env=env)
    assert again.exit_code == 0, again.output
    assert "imported before (#1)" in again.output
    assert "0 new facts to review, 23 already known" in again.output
    assert len(profile_facts.list(db)) == 23

    # An edited CV with a different title: the confirmed fact stays, the new value is a conflict.
    edited_cv = [
        ("Staff Widget Engineer at Example Widgets Ltd", style, bullet)
        if text.startswith("Senior")
        else (text, style, bullet)
        for text, style, bullet in SAMPLE_CV
    ]
    edited = cv_file.with_name(f"edited{cv_file.suffix}")
    edited.write_bytes(
        sample_pdf(edited_cv) if cv_file.suffix == ".pdf" else sample_docx(edited_cv)
    )
    third = runner.invoke(app, ["import", "cv", str(edited)], env=env)
    assert third.exit_code == 0, third.output
    assert "1 new fact to review" in third.output
    assert "competing values" in third.output

    confirmed = profile_facts.get(db, title.id or 0)
    assert confirmed.state is m.FactState.CONFIRMED
    assert confirmed.value_raw == "Senior Widget Engineer"
    incoming = [f for f in profile_facts.list(db, field_key=TITLE_KEY) if f.id != title.id]
    assert [(f.value_raw, f.state) for f in incoming] == [
        ("Staff Widget Engineer", m.FactState.UNREVIEWED)
    ]
    conflicts = runner.invoke(app, ["profile", "conflicts"], env=env)
    assert "Staff Widget Engineer" in conflicts.output


def test_unreadable_file_is_recorded_as_a_failed_run(
    env: dict[str, str], db: sqlite3.Connection, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nthis is not a pdf body")
    result = runner.invoke(app, ["import", "cv", str(broken)], env=env)
    assert result.exit_code == 1
    assert "failed" in result.output
    assert "could not be read" in result.output

    (run,) = import_runs.list(db)
    assert run.status is m.ImportStatus.FAILED
    assert [e["code"] for e in run.errors if isinstance(e, dict)] == ["unreadable"]
    assert profile_facts.list(db) == []


def test_scanned_pdf_fails_with_a_clear_reason(env: dict[str, str], tmp_path: Path) -> None:
    scanned = tmp_path / "scanned.pdf"
    scanned.write_bytes(build_pdf([[], []]))
    result = runner.invoke(app, ["import", "cv", str(scanned)], env=env)
    assert result.exit_code == 1
    assert "no extractable text" in result.output
    assert "No profile facts could be extracted" in result.output


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", "legacy Word .doc"),
        (b"just some text", "unsupported file type"),
    ],
)
def test_unsupported_files_are_refused_without_a_run(
    env: dict[str, str], db: sqlite3.Connection, tmp_path: Path, content: bytes, message: str
) -> None:
    path = tmp_path / "cv.pdf"  # the extension is not trusted
    path.write_bytes(content)
    result = runner.invoke(app, ["import", "cv", str(path)], env=env)
    assert result.exit_code == 2
    assert message in result.output
    assert import_runs.list(db) == []


def test_missing_file(env: dict[str, str], tmp_path: Path) -> None:
    result = runner.invoke(app, ["import", "cv", str(tmp_path / "nope.pdf")], env=env)
    assert result.exit_code == 2
    assert "file not found" in result.output


def test_runs_and_show(env: dict[str, str], cv_file: Path) -> None:
    assert runner.invoke(app, ["import", "runs"], env=env).output.startswith("No imports yet")
    runner.invoke(app, ["import", "cv", str(cv_file)], env=env)

    runs = runner.invoke(app, ["import", "runs"], env=env)
    assert runs.exit_code == 0
    assert "partial" in runs.output
    assert " 23 " in runs.output

    show = runner.invoke(app, ["import", "show", "1"], env=env)
    assert show.exit_code == 0
    assert "23 stored from this import" in show.output
    assert "experience 8" in show.output
    assert "Volunteering" in show.output

    missing = runner.invoke(app, ["import", "show", "99"], env=env)
    assert missing.exit_code == 1


def test_import_never_logs_or_prints_cv_values(
    env: dict[str, str], cv_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    result = runner.invoke(app, ["import", "cv", str(cv_file)], env=env)
    assert result.exit_code == 0
    # Logging goes to stderr at DEBUG (setup_logging); the summary output lists no values.
    for value in (SAMPLE_NAME, SAMPLE_EMAIL, SAMPLE_PHONE, "Example Widgets", "Senior Widget"):
        assert value not in result.output
        assert value not in result.stderr


def test_prepare_logs_counts_only(
    tmp_path: Path, conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "cv.docx"
    path.write_bytes(sample_docx())
    with caplog.at_level(logging.DEBUG, logger="jaa"):
        prepared = prepare_cv_import(path)
        add_imported_facts(conn, prepared.run, prepared.candidates)
    assert "candidates=23" in caplog.text
    for value in (SAMPLE_NAME, SAMPLE_EMAIL, "Example Widgets", "Python", str(tmp_path)):
        assert value not in caplog.text
