import json
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.core.models import FactState
from jaa.documents.engine import Engine
from jaa.storage import documents
from jaa.storage.db import open_database
from jaa.storage.repository import profile_facts
from tests.fixtures.cv_profile import store_profile

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    env = {"JAA_DATA_DIR": str(tmp_path / "data"), "COLUMNS": "200"}
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    conn = open_database(tmp_path / "data" / "jaa.sqlite3")
    try:
        store_profile(
            conn,
            unreviewed={
                "headline": "Unreviewed Headline",
                "skills.go": "Go",
                "skills.rust": "Rust",
            },
        )
    finally:
        conn.close()
    return env


def _run(env: dict[str, str], *args: str, input: str | None = None, code: int = 0) -> str:
    result = runner.invoke(app, list(args), env=env, input=input)
    assert result.exit_code == code, result.output
    return result.output


def test_templates_and_preview(env: dict[str, str]) -> None:
    assert "moderncv-classic" in _run(env, "cv", "templates")
    out = _run(env, "cv", "preview", "--language", "pl")
    assert "Avery Żółkiewska-Synthetic" in out
    assert "01.2021–obecnie  Senior Widget Engineer, Example Co, Wrocław" in out
    assert "Not shown by this template: hobbies.skiing" in out
    assert "3 unreviewed fact(s) are left out" in out
    assert "Unreviewed Headline" not in out


def test_no_engine_is_explained(env: dict[str, str]) -> None:
    out = _run(env | {"PATH": "/nonexistent"}, "cv", "build", "--confirmed-only", code=1)
    assert "No LaTeX engine found" in out


@pytest.mark.latex
def test_build_offers_unreviewed_facts_and_records_manifest(
    env: dict[str, str], latex_engine: Engine, tmp_path: Path
) -> None:
    # Pick two of the three unreviewed facts; those not confirmed are left out and recorded.
    conn = open_database(Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3")
    pending = {f.field_key: f.id for f in profile_facts.list(conn, state=FactState.UNREVIEWED)}
    conn.close()
    answer = f"{pending['headline']} {pending['skills.go']}\n"
    out = _run(env, "cv", "build", "--output", str(tmp_path / "cvs"), input=answer)
    assert "3 unreviewed fact(s) are not in your confirmed profile" in out
    # The unreviewed headline conflicts with the confirmed one, so review leaves it out too.
    assert "conflicts with a confirmed value" in out
    assert "CV written:" in out
    assert "2 unreviewed fact(s) were left out." in out

    conn = open_database(Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3")
    try:
        [cv] = documents.list_cvs(conn)
        assert profile_facts.get(conn, pending["skills.go"] or 0).state is FactState.CONFIRMED
        assert profile_facts.get(conn, pending["headline"] or 0).state is FactState.UNREVIEWED
    finally:
        conn.close()
    assert cv.template_version == "moderncv-classic@1"
    assert Path(cv.path).is_file()
    excluded = cast(list[int], cv.manifest["excluded_unreviewed_fact_ids"])
    assert set(excluded) == {pending["headline"], pending["skills.rust"]}
    assert json.dumps(cv.manifest)  # manifest is plain JSON
    assert "moderncv-classic@1" in _run(env, "cv", "list")


@pytest.mark.latex
def test_paper_follows_the_preference_unless_overridden(
    env: dict[str, str], latex_engine: Engine, tmp_path: Path
) -> None:
    folder = str(tmp_path / "cvs")
    _run(env, "preferences", "set", "documents.paper_size", "letter")
    _run(env, "cv", "build", "--confirmed-only", "--output", folder)
    _run(env, "cv", "build", "--confirmed-only", "--output", folder, "--paper", "a4")
    conn = open_database(Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3")
    try:
        papers = [str(cv.manifest["paper"]) for cv in documents.list_cvs(conn)]
    finally:
        conn.close()
    assert sorted(papers) == ["a4", "letter"]
