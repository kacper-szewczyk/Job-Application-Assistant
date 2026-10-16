"""`jaa templates …` end to end (#40, ADR 0029)."""

import json
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.documents.engine import Engine
from jaa.storage.db import open_database
from tests.fixtures.cv_profile import store_profile

runner = CliRunner()

CLASSIC = (
    Path(__file__).resolve().parents[2]
    / "src/jaa/documents/templates/moderncv-classic/template.tex"
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    env = {"JAA_DATA_DIR": str(tmp_path / "data"), "COLUMNS": "200"}
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    conn = open_database(tmp_path / "data" / "jaa.sqlite3")
    try:
        store_profile(conn)
    finally:
        conn.close()
    return env


def _run(env: dict[str, str], *args: str, input: str | None = None, code: int = 0) -> str:
    result = runner.invoke(app, list(args), env=env, input=input)
    assert result.exit_code == code, result.output
    return result.output


def _zip(tmp_path: Path, main: str) -> Path:
    archive = tmp_path / "upload.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("mine/cv.tex", main)
        zf.writestr("mine/README.md", "not a template file")
    return archive


def test_list_without_a_database(tmp_path: Path) -> None:
    out = _run({"JAA_DATA_DIR": str(tmp_path / "none"), "COLUMNS": "200"}, "templates", "list")
    shipped = (
        "moderncv-classic (default)",
        "moderncv-banking",
        "moderncv-casual",
        "moderncv-fancy",
    )
    for template_id in shipped:
        assert template_id in out
    assert "moderncv-letter (default)" in out


def test_check_reports_problems_without_adding(env: dict[str, str], tmp_path: Path) -> None:
    broken = CLASSIC.read_text().replace("%%jaa:consent%%", "\\input{/etc/hosts}")
    out = _run(env, "templates", "check", str(_zip(tmp_path, broken)), code=1)
    assert "Problem: missing slot line %%jaa:consent%%" in out
    assert "outside the template's folder" in out
    assert "Skipped: mine/README.md" in out

    out = _run(env, "templates", "add", str(_zip(tmp_path, broken)), "--id", "mine", code=1)
    assert "Not added." in out
    assert "user-mine" not in _run(env, "templates", "list")


def test_add_choose_show_and_delete(env: dict[str, str], tmp_path: Path) -> None:
    archive = _zip(tmp_path, CLASSIC.read_text().replace("{blue}", "{green}"))
    out = _run(env, "templates", "check", str(archive))
    assert "The template meets the template contract." in out

    out = _run(env, "templates", "add", str(archive), "--id", "mine", "--name", "My green CV")
    assert "Added user-mine@1." in out
    assert "already stored as user-mine@1" in _run(
        env, "templates", "add", str(archive), "--id", "mine"
    )
    listed = _run(env, "templates", "list")
    assert "user-mine" in listed and "My green CV" in listed and "added (zip)" in listed

    out = _run(env, "templates", "show", "user-mine")
    assert "user-mine@1: My green CV" in out
    assert "Family: moderncv" in out
    assert "License: Added by you" in out

    assert "Default cv template: user-mine." in _run(env, "templates", "default", "user-mine")
    assert "user-mine (default)" in _run(env, "templates", "list")
    _run(env, "templates", "default", "nope", code=1)

    _run(env, "templates", "delete", "user-mine", input="n\n", code=1)
    assert "Deleted 1 version(s) of user-mine." in _run(
        env, "templates", "delete", "user-mine", "--yes"
    )
    assert "moderncv-classic (default)" in _run(env, "templates", "list")


def test_add_from_a_template_candidate(env: dict[str, str], tmp_path: Path) -> None:
    cv = tmp_path / "cv.tex"
    cv.write_text(CLASSIC.read_text())
    # The template has slot lines but no facts, so the import fails; the candidate is kept.
    _run(env, "import", "cv", str(cv), "--as-template", code=1)
    out = _run(env, "templates", "add", "--candidate", "1", "--id", "from-import")
    assert "Added user-from-import@1." in out
    _run(env, "templates", "add", str(cv), "--candidate", "1", "--id", "x", code=2)


@pytest.mark.latex
@pytest.mark.parametrize("language", ["en", "pl"])
def test_preview_a_shipped_template(
    env: dict[str, str], tmp_path: Path, latex_engine: Engine, language: str
) -> None:
    out = _run(env, "templates", "show", "moderncv-fancy", "--preview", "--language", language)
    assert "Preview written:" in out
    previews = (tmp_path / "data" / "previews").glob(f"cv-*-moderncv-fancy-{language}.pdf")
    assert len(list(previews)) == 1


@pytest.mark.latex
def test_preview_and_build_with_an_added_template(
    env: dict[str, str], tmp_path: Path, latex_engine: Engine
) -> None:
    folder = tmp_path / "upload"
    (folder / "img").mkdir(parents=True)
    main = CLASSIC.read_text().replace("%%jaa:header%%", "\\usepackage{mystyle}\n%%jaa:header%%")
    (folder / "cv.tex").write_text(main)
    (folder / "mystyle.sty").write_text("\\ProvidesPackage{mystyle}\n\\moderncvcolor{green}\n")
    _run(env, "templates", "add", str(folder), "--id", "green")

    out = _run(env, "templates", "show", "user-green", "--preview", "--language", "pl")
    assert "Preview written:" in out
    previews = list((tmp_path / "data" / "previews").glob("cv-*-user-green-pl.pdf"))
    assert len(previews) == 1

    _run(env, "templates", "default", "user-green")
    out = _run(env, "cv", "build", "--confirmed-only")
    assert "Building with user-green@1" in out
    conn = open_database(tmp_path / "data" / "jaa.sqlite3")
    try:
        row = conn.execute(
            "SELECT template_version, manifest FROM document_artifacts WHERE kind = 'cv_pdf'"
        ).fetchone()
    finally:
        conn.close()
    assert row["template_version"] == "user-green@1"
    template = json.loads(row["manifest"])["template"]
    assert template["id"] == "user-green" and template["uploaded"] is True

    out = _run(env, "cv", "build", "--confirmed-only", "--template", "moderncv-banking")
    assert "Building with moderncv-banking@1" in out
