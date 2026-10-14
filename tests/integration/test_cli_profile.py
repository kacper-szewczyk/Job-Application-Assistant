from pathlib import Path

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.core import models as m
from jaa.storage.db import open_database
from jaa.storage.profile import add_imported_facts
from tests.conftest import SYNTHETIC_EMAIL, SYNTHETIC_TITLE, T0

runner = CliRunner()
TITLE_KEY = "experience.example-co.title"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    env = {"JAA_DATA_DIR": str(tmp_path / "data"), "JAA_LOG_LEVEL": "DEBUG", "COLUMNS": "200"}
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    return env


def _seed(env: dict[str, str], *candidates: tuple[str, str]) -> None:
    conn = open_database(Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3")
    try:
        add_imported_facts(
            conn,
            m.ImportRun(
                source_type=m.ImportSourceType.PDF,
                source_path="/synthetic/cv.pdf",
                imported_at=T0,
                status=m.ImportStatus.SUCCEEDED,
                retention=m.RetentionChoice.KEEP,
            ),
            [
                m.FactCandidate(
                    field_key=key,
                    value_raw=value,
                    value_normalized=value,
                    confidence=0.9,
                    source_location="page 1",
                    source_excerpt=f"... {value} ...",
                )
                for key, value in candidates
            ],
        )
    finally:
        conn.close()


def _run(env: dict[str, str], *args: str) -> str:
    result = runner.invoke(app, ["profile", *args], env=env)
    assert result.exit_code == 0, result.output
    return result.output


def test_list_masks_sensitive_values(env: dict[str, str]) -> None:
    _seed(env, (TITLE_KEY, SYNTHETIC_TITLE), ("contact.email", SYNTHETIC_EMAIL))
    out = _run(env, "facts")
    assert SYNTHETIC_TITLE in out
    assert SYNTHETIC_EMAIL not in out
    assert "sensitive" in out
    assert "90%" in out
    assert "import #1, page 1" in out
    assert SYNTHETIC_EMAIL in _run(env, "facts", "--reveal")
    assert "No confirmed facts." in _run(env, "facts", "--state", "confirmed")


def test_show_displays_provenance(env: dict[str, str]) -> None:
    _seed(env, (TITLE_KEY, SYNTHETIC_TITLE))
    out = _run(env, "show", "1")
    assert SYNTHETIC_TITLE in out
    assert "pdf /synthetic/cv.pdf, 2026-10-14 09:00 UTC" in out
    assert "location: page 1" in out
    assert f"excerpt: “... {SYNTHETIC_TITLE} ...”" in out
    assert "Confidence:  90%" in out
    assert "Not reviewed yet." in out


def test_confirm_edit_reject_and_versions(env: dict[str, str]) -> None:
    _seed(env, (TITLE_KEY, "Principal Widget Enginer"), ("skills.go", "Go"))
    assert "Profile version 1 created" in _run(env, "confirm", "1", "2")

    out = _run(env, "edit", "1", SYNTHETIC_TITLE)
    assert "Fact #3" in out and "Profile version 2" in out
    shown = _run(env, "show", "3")
    assert "↳ corrects #1" in shown
    assert "unreviewed → confirmed (profile version 1)" in _run(env, "show", "1")

    assert "Profile version 3" in _run(env, "reject", "2")
    versions = _run(env, "versions")
    assert "active" in versions
    assert [line.split()[1] for line in versions.splitlines() if "active" in line] == ["3"]


def test_conflicts_shown_side_by_side(env: dict[str, str]) -> None:
    _seed(env, (TITLE_KEY, SYNTHETIC_TITLE))
    _run(env, "confirm", "1")
    _seed(env, (TITLE_KEY, "Chief Widget Officer"))

    out = _run(env, "conflicts")
    assert TITLE_KEY in out
    confirmed = next(line for line in out.splitlines() if "confirmed" in line)
    incoming = next(line for line in out.splitlines() if "incoming" in line)
    assert SYNTHETIC_TITLE in confirmed
    assert "Chief Widget Officer" in incoming

    result = runner.invoke(app, ["profile", "confirm", "2"], env=env)
    assert result.exit_code == 1
    assert "Conflict" in result.output and "jaa profile conflicts" in result.output

    _run(env, "confirm", "2", "--replace")
    assert "No conflicts." in _run(env, "conflicts")


def test_unknown_fact_is_an_error(env: dict[str, str]) -> None:
    result = runner.invoke(app, ["profile", "confirm", "42"], env=env)
    assert result.exit_code == 1
    assert "not found" in result.output


def test_edit_json_value(env: dict[str, str]) -> None:
    _seed(env, ("education.0.year", "2O20"))
    assert runner.invoke(app, ["profile", "edit", "1", "{", "--json"], env=env).exit_code == 1
    _run(env, "edit", "1", "2020", "--json")
    assert "Normalized:  2020" in _run(env, "show", "2")


def test_commands_need_database(tmp_path: Path) -> None:
    result = runner.invoke(app, ["profile", "facts"], env={"JAA_DATA_DIR": str(tmp_path / "x")})
    assert result.exit_code == 1
    assert "jaa init" in result.output
