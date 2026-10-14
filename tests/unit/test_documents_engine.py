import stat
from pathlib import Path

import pytest

from jaa.core.errors import LatexEngineNotFoundError
from jaa.documents.engine import Severity, find_engine, parse_log

TEX_LOG = r"""
This is XeTeX, Version 3.141592653
(./cv.tex
! Undefined control sequence.
l.23 \cvnosuchmacro
                   {Example}
! LaTeX Error: File `moderncv.cls' not found.

Type X to quit or <RETURN> to proceed,
l.4 \moderncvstyle
Overfull \hbox (12.5pt too wide) in paragraph at lines 31--31
Overfull \hbox (0.8pt too wide) in paragraph at lines 40--40
"""

TECTONIC_OUTPUT = """
error: cv.tex:12: Undefined control sequence
error: halted on potentially-recoverable error as specified
"""


def test_parse_tex_log() -> None:
    problems = parse_log(TEX_LOG)
    errors = [p for p in problems if p.severity is Severity.ERROR]
    assert [(p.message, p.line) for p in errors] == [
        ("Undefined control sequence.", 23),
        ("LaTeX Error: File `moderncv.cls' not found.", 4),
    ]
    assert errors[0].hint is not None and "not defined" in errors[0].hint
    assert errors[1].hint is not None and "missing" in errors[1].hint
    warnings = [p for p in problems if p.severity is Severity.WARNING]
    # The 0.8pt overflow is below the threshold.
    assert [(p.message, p.line) for p in warnings] == [("text runs 12pt into the margin", 31)]
    assert errors[0].describe().startswith("line 23: Undefined control sequence.")


def test_parse_tectonic_output() -> None:
    problems = parse_log(TECTONIC_OUTPUT)
    assert [(p.message, p.line) for p in problems] == [("Undefined control sequence", 12)]


def _fake_binary(folder: Path, name: str) -> Path:
    path = folder / name
    path.write_text("#!/bin/sh\necho 'Fake 1.0'\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_find_engine_prefers_tectonic(tmp_path: Path) -> None:
    _fake_binary(tmp_path, "xelatex")
    _fake_binary(tmp_path, "tectonic")
    engine = find_engine({"PATH": str(tmp_path)})
    assert (engine.name, engine.version) == ("tectonic", "Fake 1.0")
    configured = find_engine({"PATH": str(tmp_path), "JAA_LATEX_ENGINE": "xelatex"})
    assert configured.name == "xelatex"


def test_find_engine_errors(tmp_path: Path) -> None:
    with pytest.raises(LatexEngineNotFoundError, match="brew install tectonic"):
        find_engine({"PATH": str(tmp_path)})
    with pytest.raises(LatexEngineNotFoundError, match="was not found"):
        find_engine({"PATH": str(tmp_path), "JAA_LATEX_ENGINE": "lualatex"})
    _fake_binary(tmp_path, "pdflatex")
    with pytest.raises(LatexEngineNotFoundError, match="is not one of"):
        find_engine({"PATH": str(tmp_path), "JAA_LATEX_ENGINE": "pdflatex"})
