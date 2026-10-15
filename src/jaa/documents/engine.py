"""Finding a LaTeX engine, compiling safely, and explaining failures (ADR 0012).

Tectonic is preferred: one binary, no TeX installation, and it fetches the exact packages it
needs on first use. XeLaTeX and LuaLaTeX from an installed TeX distribution also work. Shell
escape is always off and file access is limited to the build directory. Engine output quotes
the CV source, so it is returned to the caller for display and never logged.
"""

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from jaa.core.errors import LatexCompileError, LatexEngineNotFoundError

log = logging.getLogger(__name__)

ENGINE_ENV: Final = "JAA_LATEX_ENGINE"
# Preference order when JAA_LATEX_ENGINE is unset. pdflatex is left out: it cannot use the
# system fonts moderncv needs for full Unicode (Polish diacritics) without extra setup.
SUPPORTED: Final = ("tectonic", "xelatex", "lualatex")
# The first Tectonic run downloads packages, which can take minutes on a slow connection.
DEFAULT_TIMEOUT_SECONDS: Final = 600.0

INSTALL_HINT: Final = (
    "Install Tectonic (macOS: `brew install tectonic`; Linux: see "
    "https://tectonic-typesetting.github.io/), or a TeX distribution with xelatex, "
    f"or point {ENGINE_ENV} at an engine binary."
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Engine:
    name: str  # one of SUPPORTED
    path: Path
    version: str


def _version(path: Path) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - fixed arguments, engine path from PATH or config
            [str(path), "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    first = (result.stdout or result.stderr).strip().splitlines()
    return first[0].strip() if first else "unknown"


def find_engine(env: Mapping[str, str] | None = None) -> Engine:
    """The configured engine (`JAA_LATEX_ENGINE`: a name or a path), else the first on PATH."""
    env = os.environ if env is None else env
    configured = env.get(ENGINE_ENV, "").strip()
    candidates = (configured,) if configured else SUPPORTED
    for candidate in candidates:
        found = shutil.which(candidate, path=env.get("PATH"))
        if found is None:
            continue
        path = Path(found).resolve()
        name = next((s for s in SUPPORTED if path.name.startswith(s)), None)
        if name is None:
            raise LatexEngineNotFoundError(
                f"{ENGINE_ENV}={configured} is not one of {', '.join(SUPPORTED)}"
            )
        return Engine(name=name, path=path, version=_version(path))
    if configured:
        raise LatexEngineNotFoundError(f"{ENGINE_ENV}={configured} was not found. {INSTALL_HINT}")
    raise LatexEngineNotFoundError(f"No LaTeX engine found. {INSTALL_HINT}")


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True, kw_only=True)
class LatexProblem:
    severity: Severity
    message: str
    line: int | None = None
    hint: str | None = None

    def describe(self) -> str:
        where = f"line {self.line}: " if self.line else ""
        return f"{where}{self.message}" + (f" ({self.hint})" if self.hint else "")


_HINTS: Final = (
    (
        re.compile(r"File `([^']+)' not found"),
        "a package or file the template needs is missing; with Tectonic check the network "
        "connection on the first build, with a TeX distribution install the package",
    ),
    (
        re.compile(r"Undefined control sequence"),
        "the template uses a command that is not defined; check the template and its packages",
    ),
    (
        re.compile(r"Missing \$ inserted|Missing \{ inserted|Extra \}"),
        "the generated source is malformed; please report this as a bug",
    ),
    (
        re.compile(r"Unable to load picture|Cannot determine size of graphic|not loadable"),
        "the photo could not be read; use a JPEG or PNG file",
    ),
    (
        re.compile(r"Emergency stop|Fatal error"),
        "LaTeX stopped early; the messages above show why",
    ),
)
_ERROR_LINE: Final = re.compile(r"^! (.+)$", re.MULTILINE)
_LINE_NO: Final = re.compile(r"^l\.(\d+)", re.MULTILINE)
# Tectonic's own messages: "error: cv.tex:12: Undefined control sequence".
_TECTONIC_ERROR: Final = re.compile(r"^error: (?:[^:\n]+\.tex:(\d+): )?(.+)$", re.MULTILINE)
_OVERFULL: Final = re.compile(
    r"^Overfull \\hbox \((\d+(?:\.\d+)?)pt too wide\).*?lines? (\d+)", re.MULTILINE
)
# Narrow overflows are invisible in print; wider ones run into the margin.
OVERFULL_THRESHOLD_PT: Final = 5.0


def _hint(message: str) -> str | None:
    return next((hint for pattern, hint in _HINTS if pattern.search(message)), None)


def parse_log(text: str) -> list[LatexProblem]:
    """Errors and layout warnings from a TeX log or Tectonic's output, in order."""
    problems: list[LatexProblem] = []
    seen: set[tuple[str, int | None]] = set()

    def add(problem: LatexProblem) -> None:
        key = (problem.message, problem.line)
        if key not in seen:
            seen.add(key)
            problems.append(problem)

    for match in _ERROR_LINE.finditer(text):
        message = match.group(1).strip()
        line_match = _LINE_NO.search(text, match.end())
        line = int(line_match.group(1)) if line_match else None
        add(LatexProblem(severity=Severity.ERROR, message=message, line=line, hint=_hint(message)))
    for match in _TECTONIC_ERROR.finditer(text):
        message = match.group(2).strip()
        if message.startswith("halted on potentially-recoverable error"):
            continue
        line = int(match.group(1)) if match.group(1) else None
        add(LatexProblem(severity=Severity.ERROR, message=message, line=line, hint=_hint(message)))
    for match in _OVERFULL.finditer(text):
        width = float(match.group(1))
        if width >= OVERFULL_THRESHOLD_PT:
            add(
                LatexProblem(
                    severity=Severity.WARNING,
                    message=f"text runs {width:.0f}pt into the margin",
                    line=int(match.group(2)),
                    hint="a long word, URL or skill list does not fit the line",
                )
            )
    return problems


@dataclass(frozen=True, slots=True, kw_only=True)
class CompileResult:
    pdf: Path
    warnings: tuple[LatexProblem, ...]
    # Absolute paths Tectonic reports reading. Fonts and packages come from its bundle, so any
    # path here was asked for by the document itself (ADR 0029).
    absolute_reads: tuple[str, ...] = ()


_ABSOLUTE_READ: Final = re.compile(r"warning: accessing absolute path `([^`]+)`")


def _command(engine: Engine, tex: Path) -> list[str]:
    if engine.name == "tectonic":
        return [str(engine.path), "-X", "compile", "--untrusted", "--keep-logs", tex.name]
    return [
        str(engine.path),
        "-no-shell-escape",
        "-interaction=nonstopmode",
        "-halt-on-error",
        tex.name,
    ]


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    # kpathsea "paranoid" mode: read and write only below the build directory.
    env["openin_any"] = "p"
    env["openout_any"] = "p"
    env.pop("TEXINPUTS", None)
    return env


def compile_tex(
    engine: Engine, tex: Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> CompileResult:
    """Compile `tex` in its own directory. Raises `LatexCompileError` with explained problems."""
    workdir = tex.parent
    runs = 1 if engine.name == "tectonic" else 2  # TeX engines need a second pass for layout
    output = ""
    for _ in range(runs):
        try:
            result = subprocess.run(  # noqa: S603 - argument list, no shell
                _command(engine, tex),
                cwd=workdir,
                env=_environment(),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LatexCompileError(
                f"{engine.name} did not finish within {timeout:.0f}s",
                ("the first Tectonic build downloads packages; try again with a working network",),
            ) from exc
        except OSError as exc:
            raise LatexEngineNotFoundError(f"cannot run {engine.path}: {exc}") from exc
        output = result.stdout + "\n" + result.stderr
        log_file = tex.with_suffix(".log")
        full_log = log_file.read_text(errors="replace") if log_file.is_file() else ""
        if result.returncode != 0:
            problems = parse_log(full_log + "\n" + output)
            errors = [p for p in problems if p.severity is Severity.ERROR]
            log.warning(
                "latex engine=%s failed: exit=%s errors=%s",
                engine.name,
                result.returncode,
                len(errors),
            )
            raise LatexCompileError(
                f"{engine.name} could not build the PDF",
                tuple(p.describe() for p in (errors or problems))
                or ("the engine failed without an explanation; see the log file",),
            )
        output = full_log + "\n" + output
    pdf = tex.with_suffix(".pdf")
    if not pdf.is_file():
        raise LatexCompileError(f"{engine.name} finished but wrote no PDF")
    warnings = tuple(p for p in parse_log(output) if p.severity is Severity.WARNING)
    absolute = tuple(dict.fromkeys(_ABSOLUTE_READ.findall(output)))
    log.info(
        "latex engine=%s built pdf warnings=%s absolute_reads=%s",
        engine.name,
        len(warnings),
        len(absolute),
    )
    return CompileResult(pdf=pdf, warnings=warnings, absolute_reads=absolute)
