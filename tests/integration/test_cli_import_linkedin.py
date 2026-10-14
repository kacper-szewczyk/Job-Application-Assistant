import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.core import models as m
from jaa.storage.db import open_database
from jaa.storage.repository import import_runs, profile_facts
from tests.fixtures.cv_builders import SAMPLE_CV
from tests.fixtures.linkedin_builders import (
    LI_ADDRESS,
    LI_BIRTH_DATE,
    LI_EMAIL,
    LI_MESSAGE,
    LI_PHONE,
    SAMPLE_FILES,
    build_zip,
    sample_archive,
)

runner = CliRunner()


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


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    path = tmp_path / "Basic_LinkedInDataExport.zip"
    path.write_bytes(sample_archive())
    return path


def test_without_a_file_it_explains_how_to_get_the_archive(env: dict[str, str]) -> None:
    result = runner.invoke(app, ["import", "linkedin"], env=env)
    assert result.exit_code == 0
    assert "Get a copy of your data" in result.output
    assert "never asks for your" in result.output
    assert "jaa import text" in result.output


def test_archive_imports_into_reviewable_facts_and_lists_skipped_files(
    env: dict[str, str], db: sqlite3.Connection, archive: Path
) -> None:
    result = runner.invoke(app, ["import", "linkedin", str(archive), "--keep-source"], env=env)
    assert result.exit_code == 0, result.output
    assert "linkedin_archive" in result.output
    assert "Skipped 2 files that are not profile data: Connections.csv, messages.csv" in (
        result.output
    )
    assert "Projects.csv: This section is not imported yet" in result.output
    assert "Kept the imported file(s)" in result.output
    assert archive.exists()

    facts = profile_facts.list(db)
    assert facts
    assert all(f.state is m.FactState.UNREVIEWED for f in facts)
    assert all(f.origin is m.FactOrigin.IMPORT and f.import_run_id == 1 for f in facts)
    by_key = {f.field_key: f for f in facts}
    assert by_key["contact.email"].is_sensitive
    assert by_key["contact.location"].is_sensitive
    assert by_key["experience.example-gadgets-2016-06.title"].source_location == (
        "Positions.csv row 3"
    )

    run = import_runs.get(db, 1)
    assert run.source_type is m.ImportSourceType.LINKEDIN_ARCHIVE
    assert run.retention is m.RetentionChoice.KEEP
    assert run.source_deleted_at is None

    shown = runner.invoke(app, ["import", "show", "1"], env=env)
    assert "Retention: keep" in shown.output
    assert "Skipped 2 files" in shown.output

    listed = runner.invoke(app, ["profile", "facts", "--state", "unreviewed"], env=env)
    assert "skills.c-plus-plus" in listed.output


def test_delete_source_removes_the_archive_and_records_it(
    env: dict[str, str], db: sqlite3.Connection, archive: Path
) -> None:
    result = runner.invoke(app, ["import", "linkedin", str(archive), "--delete-source"], env=env)
    assert result.exit_code == 0, result.output
    assert "Deleted the imported file(s)" in result.output
    assert not archive.exists()
    run = import_runs.get(db, 1)
    assert run.retention is m.RetentionChoice.DELETE_AFTER_IMPORT
    assert run.source_deleted_at is not None
    assert profile_facts.list(db)  # the facts stay

    shown = runner.invoke(app, ["import", "show", "1"], env=env)
    assert "Retention: delete after import" in shown.output
    assert "Source deleted:" in shown.output


@pytest.mark.parametrize(("answer", "deleted"), [("y\n", True), ("n\n", False), ("\n", False)])
def test_retention_is_asked_when_not_given(
    env: dict[str, str], db: sqlite3.Connection, archive: Path, answer: str, deleted: bool
) -> None:
    result = runner.invoke(app, ["import", "linkedin", str(archive)], env=env, input=answer)
    assert result.exit_code == 0, result.output
    assert "after a successful import?" in result.output
    assert archive.exists() is not deleted
    run = import_runs.get(db, 1)
    expected = m.RetentionChoice.DELETE_AFTER_IMPORT if deleted else m.RetentionChoice.KEEP
    assert run.retention is expected
    assert (run.source_deleted_at is not None) is deleted


def test_failed_import_keeps_the_archive_even_when_deletion_was_chosen(
    env: dict[str, str], db: sqlite3.Connection, tmp_path: Path
) -> None:
    other = tmp_path / "photos.zip"
    other.write_bytes(build_zip({"photo.jpg": b"\xff\xd8synthetic"}))
    result = runner.invoke(app, ["import", "linkedin", str(other), "--delete-source"], env=env)
    assert result.exit_code == 1
    assert "Is this a LinkedIn data archive?" in result.output
    assert "Kept the imported file(s): the import failed" in result.output
    assert other.exists()
    (run,) = import_runs.list(db)
    assert run.status is m.ImportStatus.FAILED
    assert run.source_deleted_at is None


def test_malicious_archive_is_rejected_and_nothing_is_recorded(
    env: dict[str, str], db: sqlite3.Connection, tmp_path: Path
) -> None:
    evil = tmp_path / "evil.zip"
    evil.write_bytes(build_zip({**SAMPLE_FILES, "../../escape.csv": b"Name\nx\n"}))
    result = runner.invoke(app, ["import", "linkedin", str(evil), "--delete-source"], env=env)
    assert result.exit_code == 2
    assert "points outside the archive" in result.stderr
    assert evil.exists()
    assert not (tmp_path.parent / "escape.csv").exists()
    assert import_runs.list(db) == []
    assert profile_facts.list(db) == []


def test_reimport_skips_known_values(
    env: dict[str, str], db: sqlite3.Connection, archive: Path
) -> None:
    first = runner.invoke(app, ["import", "linkedin", str(archive), "--keep-source"], env=env)
    assert first.exit_code == 0
    count = len(profile_facts.list(db))
    again = runner.invoke(app, ["import", "linkedin", str(archive), "--keep-source"], env=env)
    assert again.exit_code == 0, again.output
    assert "imported before (#1)" in again.output
    assert f"0 new facts to review, {count} already known" in again.output


def test_csv_files_from_the_archive_can_be_imported(
    env: dict[str, str], db: sqlite3.Connection, tmp_path: Path
) -> None:
    paths: list[str] = []
    for name in ("Skills.csv", "Positions.csv"):
        (tmp_path / name).write_bytes(SAMPLE_FILES[name])
        paths.append(str(tmp_path / name))
    result = runner.invoke(app, ["import", "linkedin", *paths, "--delete-source"], env=env)
    assert result.exit_code == 0, result.output
    assert not any(Path(p).exists() for p in paths)
    assert {f.field_key for f in profile_facts.list(db)} >= {
        "skills.go",
        "experience.example-widgets-ltd.title",
    }


def test_linkedin_and_cv_facts_meet_on_the_same_keys(
    env: dict[str, str], db: sqlite3.Connection, archive: Path
) -> None:
    pasted = "\n".join(f"• {text}" if bullet else text for text, _, bullet in SAMPLE_CV)
    assert runner.invoke(app, ["import", "text"], env=env, input=pasted).exit_code == 0
    result = runner.invoke(app, ["import", "linkedin", str(archive), "--keep-source"], env=env)
    assert result.exit_code == 0, result.output
    # Identical values (e.g. skills, the current role's dates) are not added twice.
    assert len(profile_facts.list(db, field_key="skills.python")) == 1
    assert len(profile_facts.list(db, field_key="experience.example-widgets-ltd.dates")) == 1


def test_import_text_from_stdin_and_file(
    env: dict[str, str], db: sqlite3.Connection, tmp_path: Path
) -> None:
    pasted = "Experience\nSenior Widget Engineer at Example Widgets Ltd\nJan 2021 – Present\n"
    result = runner.invoke(app, ["import", "text"], env=env, input=pasted)
    assert result.exit_code == 0, result.output
    assert "(pasted text)" in result.output
    run = import_runs.get(db, 1)
    assert run.source_type is m.ImportSourceType.TEXT
    assert run.source_path is None
    title = profile_facts.list(db, field_key="experience.example-widgets-ltd.title")[0]
    assert title.source_location == "line 2 · Experience"

    file = tmp_path / "skills.txt"
    file.write_text("Skills\nPython, Rust\n", encoding="utf-8")
    from_file = runner.invoke(app, ["import", "text", str(file)], env=env)
    assert from_file.exit_code == 0, from_file.output
    assert import_runs.get(db, 2).source_path == str(file.resolve())
    assert profile_facts.list(db, field_key="skills.rust")


def test_import_text_refuses_empty_input(env: dict[str, str], db: sqlite3.Connection) -> None:
    result = runner.invoke(app, ["import", "text"], env=env, input="  \n")
    assert result.exit_code == 2
    assert "no text to import" in result.stderr
    assert import_runs.list(db) == []


def test_linkedin_import_never_logs_or_prints_values(
    env: dict[str, str], archive: Path, caplog: pytest.LogCaptureFixture
) -> None:
    result = runner.invoke(app, ["import", "linkedin", str(archive), "--delete-source"], env=env)
    assert result.exit_code == 0
    # Logging goes to stderr at DEBUG; the summary names files and fields, not values.
    for value in (LI_EMAIL, LI_PHONE, LI_ADDRESS, LI_BIRTH_DATE, LI_MESSAGE, "Avery"):
        assert value not in result.output
        assert value not in result.stderr
