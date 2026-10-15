import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.connectors.base import JobListing
from jaa.core.job_details import JobDetails, ListedSkill
from jaa.core.models import JsonValue
from jaa.documents.engine import Engine
from jaa.storage import documents, listings
from jaa.storage.db import open_database
from tests.fixtures.cv_profile import store_profile

runner = CliRunner()
T1 = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
BULLET_1 = "experience.example-co.description[1]"
REWRITE = "Led the migration of widget pipelines to Kubernetes."


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    env = {"JAA_DATA_DIR": str(tmp_path / "data"), "COLUMNS": "250"}
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    conn = open_database(tmp_path / "data" / "jaa.sqlite3")
    try:
        store_profile(conn)
        listing = JobListing(
            source_job_id="job-1",
            url="https://jobs.example.test/1",
            title="Widget Engineer",
            company="Example Widgets",
            details=JobDetails(skills=(ListedSkill(name="Python"), ListedSkill(name="Kubernetes"))),
        )
        listings.store_listings(conn, "test", "all", [listing], at=T1)
    finally:
        conn.close()
    return env


def _run(env: dict[str, str], *args: str, input: str | None = None, code: int = 0) -> str:
    result = runner.invoke(app, list(args), env=env, input=input)
    assert result.exit_code == code, result.output
    return result.output


def _plan(env: dict[str, str]) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], json.loads(_run(env, "cv", "tailor", "show", "1", "--json")))


def test_propose_edit_approve_flow(env: dict[str, str], tmp_path: Path) -> None:
    assert "no tailored CV yet" in _run(env, "cv", "tailor", "show", "1", code=1)
    out = _run(env, "cv", "tailor", "propose", "1")
    assert "Revision 1 (proposed)" in out
    assert "the job asks for Python" in out
    assert "bold: Python" in out
    assert "0 blocked" in out

    plan = _plan(env)
    edits = cast(list[JsonValue], plan["edits"])
    edits.append({"kind": "rewrite", "target": BULLET_1, "text": REWRITE, "fact_ids": []})
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    out = _run(env, "cv", "tailor", "edit", "1", "--file", str(plan_file))
    assert "Revision 2 (edited)" in out
    assert "blocked: not found in the target or the cited facts: Kubernetes" in out
    number = str(len(edits))

    out = _run(env, "cv", "tailor", "approve", "1", "--edit", number, input="n\n", code=1)
    assert "Kubernetes" in out
    out = _run(env, "cv", "tailor", "approve", "1", "--edit", number, input="y\n")
    assert "Revision 3 (approved)" in out
    assert "approved: not found" in out
    out = _run(env, "cv", "tailor", "show", "1", "--full")
    assert f"• {REWRITE}" in out
    out = _run(env, "cv", "tailor", "show", "1", "--revision", "2")
    assert "Revision 2" in out and "blocked" in out
    listed = _run(env, "cv", "tailor", "list", "1")
    assert "approved" in listed and "edited" in listed


def test_invalid_plan_files_are_explained(env: dict[str, str], tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"edits": [{"kind": "invent", "target": "summary"}]}', encoding="utf-8")
    assert "kind must be one of" in _run(env, "cv", "tailor", "edit", "1", "-f", str(bad), code=1)
    bad.write_text("{", encoding="utf-8")
    assert "not valid JSON" in _run(env, "cv", "tailor", "edit", "1", "-f", str(bad), code=1)


def test_edit_in_editor(env: dict[str, str], tmp_path: Path) -> None:
    _run(env, "cv", "tailor", "propose", "1")
    # A fake editor that hides a skill: rewrites the file it is given.
    editor = tmp_path / "editor.py"
    editor.write_text(
        "import json, sys\n"
        "path = sys.argv[1]\n"
        "text = ''.join(l for l in open(path) if not l.lstrip().startswith('//'))\n"
        "plan = json.loads(text)\n"
        "plan['edits'].append({'kind': 'hide', 'target': 'skills.c-plus-plus'})\n"
        "open(path, 'w').write(json.dumps(plan))\n",
        encoding="utf-8",
    )
    out = _run(env | {"VISUAL": f"{sys.executable} {editor}"}, "cv", "tailor", "edit", "1")
    assert "Revision 2 (edited)" in out and "hide: C++" in out


def test_blocked_plan_is_not_built(env: dict[str, str], tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.json"
    edit = {"kind": "rewrite", "target": "headline", "text": "Kubernetes Engineer"}
    plan_file.write_text(json.dumps({"edits": [edit]}), encoding="utf-8")
    _run(env, "cv", "tailor", "edit", "1", "-f", str(plan_file))
    out = _run(env, "cv", "tailor", "build", "1", code=1)
    assert "blocked: edit 1" in out


@pytest.mark.latex
def test_build_links_pdf_to_job_and_records_changes(
    env: dict[str, str], latex_engine: Engine, tmp_path: Path
) -> None:
    _run(env, "cv", "tailor", "propose", "1")
    out = _run(env, "cv", "tailor", "build", "1", "--output", str(tmp_path / "cvs"))
    assert "CV written:" in out
    conn = open_database(Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3")
    try:
        [cv] = documents.list_cvs(conn)
    finally:
        conn.close()
    assert cv.job_id == 1
    assert cv.profile_version_id is not None
    assert "-job1-" in Path(cv.path).name
    tailoring = cast(dict[str, JsonValue], cv.manifest["tailoring"])
    assert tailoring["revision"] == 1
    changes = cast(list[dict[str, JsonValue]], tailoring["changes"])
    assert "skills.python" in {str(c["target"]) for c in changes}
    assert cv.manifest["job_id"] == 1
    listed = _run(env, "cv", "list")
    assert " 1 " in listed
