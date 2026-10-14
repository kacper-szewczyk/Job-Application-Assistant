import logging
import shutil
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
from jaa.storage.repository import import_runs, profile_facts, template_candidates
from tests.fixtures.cv_builders import sample_pdf

runner = CliRunner()
FIXTURES = Path(__file__).parents[1] / "fixtures" / "latex"
VALUES = ("Avery", "avery.synthetic@example.test", "7946", "Example Widgets", "Senior Widget")


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
def sources(tmp_path: Path) -> Path:
    target = tmp_path / "cvs"
    shutil.copytree(FIXTURES, target)
    return target


def test_import_tex_yields_reviewable_facts_and_sections(
    env: dict[str, str], db: sqlite3.Connection, sources: Path
) -> None:
    path = sources / "moderncv.tex"
    result = runner.invoke(app, ["import", "cv", str(path)], env=env)
    assert result.exit_code == 0, result.output
    assert "latex" in result.output and "partial" in result.output
    assert (
        "Sections:  Summary, Experience, Education, Skills, Languages; not imported: Projects"
        in result.output
    )
    assert "\\mystery" in result.output  # the unknown command is reported

    facts = profile_facts.list(db)
    assert facts and all(f.state is m.FactState.UNREVIEWED for f in facts)
    assert all(f.origin is m.FactOrigin.IMPORT and f.source_location for f in facts)
    keys = {f.field_key for f in facts}
    assert {
        "experience.example-widgets-ltd.title",
        "education.university-of-exampleton.degree",
    } <= (keys)

    run = import_runs.get(db, 1)
    assert run.source_type is m.ImportSourceType.LATEX
    assert run.summary["document_class"] == "moderncv"
    headings = run.summary["headings"]
    assert isinstance(headings, list)
    assert {"title": "Projects", "section": "unsupported"} in headings
    # Without --as-template nothing but the facts is kept.
    assert template_candidates.list(db) == []

    show = runner.invoke(app, ["import", "show", "1"], env=env)
    assert "not imported: Projects" in show.output


def test_as_template_registers_the_whole_project_once(
    env: dict[str, str], db: sqlite3.Connection, sources: Path
) -> None:
    path = sources / "awesome" / "resume.tex"
    first = runner.invoke(app, ["import", "cv", str(path), "--as-template"], env=env)
    assert first.exit_code == 0, first.output
    assert "Kept as template candidate #1 (awesome-cv, 3 files)" in first.output

    (candidate,) = template_candidates.list(db)
    assert candidate.import_run_id == 1
    assert candidate.main_file == "resume.tex"
    assert sorted(candidate.files) == ["resume.tex", "resume/experience.tex", "resume/skills.tex"]
    assert candidate.files["resume/skills.tex"] == (path.parent / "resume/skills.tex").read_text()

    again = runner.invoke(app, ["import", "cv", str(path), "--as-template"], env=env)
    assert again.exit_code == 0
    assert "Already kept as template candidate #1" in again.output
    assert len(template_candidates.list(db)) == 1

    listing = runner.invoke(app, ["import", "templates"], env=env)
    assert listing.exit_code == 0
    assert "awesome-cv" in listing.output and "resume.tex" in listing.output


def test_editing_an_included_file_makes_a_new_candidate(
    env: dict[str, str], db: sqlite3.Connection, sources: Path
) -> None:
    path = sources / "awesome" / "resume.tex"
    runner.invoke(app, ["import", "cv", str(path), "--as-template"], env=env)
    skills = path.parent / "resume" / "skills.tex"
    skills.write_text(skills.read_text().replace("Rust", "Zig"))
    result = runner.invoke(app, ["import", "cv", str(path), "--as-template"], env=env)
    assert "Kept as template candidate #2" in result.output
    assert len(template_candidates.list(db)) == 2


def test_as_template_needs_latex(
    env: dict[str, str], db: sqlite3.Connection, tmp_path: Path
) -> None:
    path = tmp_path / "cv.pdf"
    path.write_bytes(sample_pdf())
    result = runner.invoke(app, ["import", "cv", str(path), "--as-template"], env=env)
    assert result.exit_code == 2
    assert "needs a LaTeX source" in result.output
    assert import_runs.list(db) == []


def test_templates_empty(env: dict[str, str]) -> None:
    result = runner.invoke(app, ["import", "templates"], env=env)
    assert result.output.startswith("No template candidates")


def test_fragment_is_refused_with_advice(
    env: dict[str, str], db: sqlite3.Connection, sources: Path
) -> None:
    result = runner.invoke(
        app, ["import", "cv", str(sources / "awesome" / "resume" / "experience.tex")], env=env
    )
    assert result.exit_code == 2
    assert "import the main .tex file" in result.output
    assert import_runs.list(db) == []


def test_reimport_never_touches_confirmed_facts(
    env: dict[str, str], db: sqlite3.Connection, sources: Path
) -> None:
    path = sources / "moderncv.tex"
    runner.invoke(app, ["import", "cv", str(path)], env=env)
    key = "experience.example-widgets-ltd.title"
    (title,) = [f for f in profile_facts.list(db) if f.field_key == key]
    assert runner.invoke(app, ["profile", "confirm", str(title.id)], env=env).exit_code == 0

    path.write_text(path.read_text().replace("Senior Widget Engineer", "Staff Widget Engineer"))
    result = runner.invoke(app, ["import", "cv", str(path)], env=env)
    assert result.exit_code == 0
    assert "1 new fact to review" in result.output
    assert "competing values" in result.output
    versions = {f.value_raw: f.state for f in profile_facts.list(db) if f.field_key == key}
    assert versions == {
        "Senior Widget Engineer": m.FactState.CONFIRMED,
        "Staff Widget Engineer": m.FactState.UNREVIEWED,
    }


def test_import_never_prints_cv_values(env: dict[str, str], sources: Path) -> None:
    result = runner.invoke(
        app, ["import", "cv", str(sources / "moderncv.tex"), "--as-template"], env=env
    )
    assert result.exit_code == 0
    for value in VALUES:
        assert value not in result.output
        assert value not in result.stderr


def test_prepare_logs_counts_only(
    sources: Path, conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="jaa"):
        prepared = prepare_cv_import(sources / "moderncv.tex")
        add_imported_facts(conn, prepared.run, prepared.candidates)
    assert "prepared latex import" in caplog.text
    for value in (*VALUES, str(sources)):
        assert value not in caplog.text
