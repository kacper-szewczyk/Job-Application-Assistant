import logging
import stat
from pathlib import Path

import pytest

from jaa.core.errors import DocumentError, LatexCompileError
from jaa.documents.content import CvLanguage, build_content
from jaa.documents.engine import CompileResult, Engine
from jaa.documents.latex import DEFAULT_TEMPLATE, get_template
from jaa.documents.pipeline import BuildOptions, build_cv, expected_text, missing_from
from tests.fixtures.cv_profile import confirmed_facts

FAKE_ENGINE = Engine(name="tectonic", path=Path("/nonexistent/tectonic"), version="fake")


def _failing_compiler(engine: Engine, tex: Path) -> CompileResult:
    tex.with_suffix(".log").write_text("! Undefined control sequence.\nl.3 \\oops\n")
    raise LatexCompileError(
        "tectonic could not build the PDF", ("line 3: Undefined control sequence.",)
    )


def _build(tmp_path: Path, options: BuildOptions | None = None) -> None:
    build_cv(
        build_content(confirmed_facts()).content,
        template=get_template(DEFAULT_TEMPLATE),
        engine=FAKE_ENGINE,
        output_dir=tmp_path / "out",
        options=options or BuildOptions(),
        profile_version_id=1,
        compiler=_failing_compiler,
    )


def test_failed_build_keeps_private_log_and_cleans_up(tmp_path: Path) -> None:
    with pytest.raises(LatexCompileError) as exc_info:
        _build(tmp_path)
    assert exc_info.value.problems == ("line 3: Undefined control sequence.",)
    files = list((tmp_path / "out").iterdir())
    assert [f.suffix for f in files] == [".log"]  # no PDF, no leftover build folder
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("content", "message"),
    [(b"GIF89a....", "JPEG or PNG"), (b"\xff\xd8\xff" + b"0" * (11 * 1024 * 1024), "larger")],
)
def test_photo_is_checked(tmp_path: Path, content: bytes, message: str) -> None:
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(content)
    with pytest.raises(DocumentError, match=message):
        _build(tmp_path, BuildOptions(photo=photo))
    with pytest.raises(DocumentError, match="not found"):
        _build(tmp_path, BuildOptions(photo=tmp_path / "missing.jpg"))


def test_text_check_ignores_layout_but_not_content() -> None:
    content = build_content(confirmed_facts()).content
    expected = expected_text(content, CvLanguage.EN)
    assert "Example Co" in expected
    assert "01.2021-Present" in expected
    extracted = "\n".join(expected).replace("Led the migration", "Led the\nmigra-\ntion")
    extracted = extracted.replace("01.2021-Present", "01.2021\u2013\nPresent")
    missing = missing_from(extracted, expected)
    assert missing == ["Led the migration of widget pipelines to event streaming."]
    assert missing_from("ﬁle", ["file"]) == []  # ligatures are normalized


def test_logs_hold_no_cv_text(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="jaa"), pytest.raises(LatexCompileError):
        _build(tmp_path)
    for value in ("Avery", "Example Co", "Undefined control"):
        assert value not in caplog.text
