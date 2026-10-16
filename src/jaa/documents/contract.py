"""The template contract: what a CV or cover letter template must provide (ADR 0029).

Pure checks on a template's files, run when a template is loaded or added, so problems are
reported before anything is compiled. `docs/templates.md` documents the contract for people
writing templates.

A template has a main `.tex` file with slot lines (`%%jaa:<slot>%%`) that jaa replaces with
generated LaTeX. The generated LaTeX calls moderncv's macros (`\\cventry`, `\\cvitem`, ...), so a
template either uses the moderncv class or defines those macros itself. Uploaded templates are
also checked for file access: TeX engines can read any file the user can read (Tectonic has no
sandbox), so paths that leave the template's folder, Lua code and shell or file-writing
primitives are refused. That check only sees what is written out; low-level TeX that builds
command names at run time is reported as a warning.

The paper size is a `\\documentclass` option that jaa sets for each build (ADR 0033), so a
template must not fix the paper anywhere else in its main file.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Final

from jaa.core.models import PaperSize, TemplateFamily, TemplateKind, TemplateOrigin

SLOTS: Final[dict[TemplateKind, tuple[str, ...]]] = {
    TemplateKind.CV: ("babel", "header", "body", "consent"),
    # A cover letter's `letter` slot sets the recipient, date, opening and closing (ADR 0024).
    TemplateKind.COVER_LETTER: ("babel", "header", "letter", "body"),
}
# Slots that must come before `\begin{document}`; the rest come after it.
PREAMBLE_SLOTS: Final = frozenset({"babel", "header", "letter"})

# Macros the generated LaTeX calls, beyond what every LaTeX class has (\section, \title, ...).
# The moderncv class provides them all; a custom template must define each one.
REQUIRED_MACROS: Final[dict[TemplateKind, tuple[str, ...]]] = {
    TemplateKind.CV: (
        "name",
        "address",
        "mobile",
        "email",
        "extrainfo",
        "photo",
        "quote",
        "makecvtitle",
        "cventry",
        "cvitem",
        "cvlistitem",
        "cvlanguage",
    ),
    TemplateKind.COVER_LETTER: (
        "name",
        "address",
        "mobile",
        "email",
        "extrainfo",
        "recipient",
        "opening",
        "closing",
        "makelettertitle",
        "makeletterclosing",
    ),
}

TEXT_SUFFIXES: Final = frozenset({".tex", ".cls", ".sty", ".cfg", ".def", ".clo", ".fd"})
ASSET_SUFFIXES: Final = frozenset({".png", ".jpg", ".jpeg", ".pdf", ".otf", ".ttf"})
MAX_FILES: Final = 100
MAX_TOTAL_BYTES: Final = 5 * 1024 * 1024
# The build folder holds document.tex, document.pdf, document.log ... and the user's photo.
RESERVED_STEMS: Final = frozenset({"document", "photo"})

SLOT_LINE: Final = re.compile(r"^%%jaa:([a-z_]+)%%[ \t]*$", re.MULTILINE)
_COMMENT: Final = re.compile(r"(?<!\\)%.*$", re.MULTILINE)
_DOCUMENTCLASS: Final = re.compile(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{\s*([^}\s]+)\s*\}")
_BEGIN_DOCUMENT: Final = re.compile(r"\\begin\s*\{document\}")
_DEFINES: Final = re.compile(
    r"\\(?:(?:re)?newcommand|providecommand|DeclareRobustCommand)\*?\s*\{?\s*\\([A-Za-z@]+)"
    r"|\\(?:New|Renew|Provide|Declare)DocumentCommand\s*\{?\s*\\([A-Za-z@]+)"
    r"|\\(?:[egx]?def|let)\s*\\([A-Za-z@]+)"
)
# Lua runs with the engine's file access; \write18 and \openout run commands or write files.
_FORBIDDEN: Final = re.compile(
    r"\\(directlua|latelua|luaexec|luadirect|luacode|write18|ShellEscape|openout)(?![A-Za-z@])"
    r"|\\begin\s*\{(luacode\*?)\}"
    r"|\\input\s*\{?\s*(\|)"
)
# Paths that leave the template's folder: absolute, home-relative or parent paths in an
# argument or after `=`, and `..` path segments anywhere.
_OUTSIDE_PATH: Final = re.compile(
    r"(?:[{=\"]|\\input\s|\\openin\s*\\?[A-Za-z@]*\s*=?)\s*(?:/|~)[\w.~/-]"
    r"|(?:^|[{=\s/\"])\.\.[/\\]"
)
# The class option jaa sets for each paper size; geometry and the classes follow it.
PAPER_OPTIONS: Final[dict[PaperSize, str]] = {
    PaperSize.A4: "a4paper",
    PaperSize.LETTER: "letterpaper",
}
# Width and height in PostScript points, to check the built PDF.
PAPER_POINTS: Final[dict[PaperSize, tuple[float, float]]] = {
    PaperSize.A4: (595.28, 841.89),
    PaperSize.LETTER: (612.0, 792.0),
}
_PAPER_NAME: Final = r"(?:[abc][0-9]|letter|legal|executive)paper"
_PAPER_OPTION: Final = re.compile(rf"^{_PAPER_NAME}$")
_CLASS_OPTIONS: Final = re.compile(r"\\documentclass\s*(?:\[([^\]]*)\])?\s*\{")
# Paper set anywhere but the class options would override the size jaa chose: a paper name
# (geometry or class options), key=value sizes, or assignments to the page dimensions.
_FIXED_PAPER: Final = re.compile(
    rf"(?<![A-Za-z]){_PAPER_NAME}(?![A-Za-z])"
    r"|(?<![A-Za-z])paper(?:size|width|height)?\s*="
    r"|\\(?:pdf)?(?:paper|page)(?:width|height)\s*=?\s*-?[\d.]"
    r"|\\setlength\s*\{?\s*\\(?:pdf)?(?:paper|page)(?:width|height)"
    r"|\\special\s*\{\s*papersize"
)
# Primitives that build command names or re-read text at run time, which hides what is read.
_OPAQUE: Final = re.compile(r"\\(csname|catcode|scantokens)(?![A-Za-z@])")


@dataclass(frozen=True, slots=True, kw_only=True)
class TemplateCheck:
    """The result of checking a template. Problems prevent its use; warnings do not."""

    kind: TemplateKind
    family: TemplateFamily
    document_class: str | None
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True, slots=True, kw_only=True)
class TemplateFiles:
    """An uploaded template's files, keyed by POSIX path relative to the main file's folder."""

    main_file: str
    texts: Mapping[str, str] = field(repr=False)
    assets: Mapping[str, bytes] = field(repr=False)
    origin: TemplateOrigin
    # Files that were left out (hidden, outside the main file's folder, or of another type).
    skipped: tuple[str, ...] = ()

    def check(self, kind: TemplateKind) -> "TemplateCheck":
        sizes = {path: len(data) for path, data in self.assets.items()}
        return check_template(self.main_file, self.texts, sizes, kind)


def strip_comments(source: str) -> str:
    """TeX source without `%` comments (slot lines are comments, so check them first)."""
    return _COMMENT.sub("", source)


def slot_problems(source: str, kind: TemplateKind) -> list[str]:
    """Each slot must appear exactly once, on its own line, on the right side of the preamble."""
    slots = SLOTS[kind]
    found = [(m.group(1), m.start()) for m in SLOT_LINE.finditer(source)]
    names = [name for name, _ in found]
    problems: list[str] = []
    for slot in slots:
        count = names.count(slot)
        if count == 0:
            problems.append(f"missing slot line %%jaa:{slot}%%")
        elif count > 1:
            problems.append(f"slot line %%jaa:{slot}%% appears {count} times; it must appear once")
    for name in sorted(set(names) - set(slots)):
        problems.append(f"unknown slot line %%jaa:{name}%% for a {kind.value} template")
    begin = _BEGIN_DOCUMENT.search(strip_comments(source))
    if begin is None:
        problems.append(r"no \begin{document} in the main file")
        return problems
    # Comments are stripped to a shorter text, so compare against the original source.
    begin_at = _BEGIN_DOCUMENT.search(source)
    begin_pos = begin_at.start() if begin_at else begin.start()
    for name, pos in found:
        if name not in slots:
            continue
        if name in PREAMBLE_SLOTS and pos > begin_pos:
            problems.append(rf"slot line %%jaa:{name}%% must come before \begin{{document}}")
        if name not in PREAMBLE_SLOTS and pos < begin_pos:
            problems.append(rf"slot line %%jaa:{name}%% must come after \begin{{document}}")
    return problems


def document_class(source: str) -> str | None:
    match = _DOCUMENTCLASS.search(strip_comments(source))
    return match.group(1) if match else None


def _class_options(source: str) -> re.Match[str] | None:
    """The `\\documentclass` command in `source` that is not commented out."""
    for match in _CLASS_OPTIONS.finditer(source):
        line_start = source.rfind("\n", 0, match.start()) + 1
        if not _COMMENT.search(source[line_start : match.start()]):
            return match
    return None


def paper_problems(source: str) -> list[str]:
    """A main file may choose its paper only through `\\documentclass` options (ADR 0033)."""
    code = strip_comments(source)
    found = _class_options(code)
    if found is not None:
        code = code[: found.start()] + code[found.end() :]
    fixed = _FIXED_PAPER.search(code)
    if fixed is None:
        return []
    return [
        f"the main file sets the paper size ({fixed.group(0).strip()}) outside the "
        "\\documentclass options; jaa sets the paper for each build, so remove it"
    ]


def set_paper(source: str, paper: PaperSize) -> str:
    """`source` with its `\\documentclass` paper option set to `paper`.

    Replaces the first paper option in place, drops any others, or adds one when the class has
    none. Raises `ValueError` when there is no `\\documentclass` (the contract requires one).
    """
    found = _class_options(source)
    if found is None:
        raise ValueError("no \\documentclass in the main file")
    options = [o.strip() for o in (found.group(1) or "").split(",") if o.strip()]
    chosen = PAPER_OPTIONS[paper]
    kept: list[str] = []
    for option in options:
        if not _PAPER_OPTION.match(option):
            kept.append(option)
        elif chosen not in kept:
            kept.append(chosen)
    if chosen not in kept:
        kept.append(chosen)
    command = f"\\documentclass[{','.join(kept)}]{{"
    return source[: found.start()] + command + source[found.end() :]


def defined_macros(sources: Mapping[str, str]) -> set[str]:
    found: set[str] = set()
    for text in sources.values():
        for match in _DEFINES.finditer(strip_comments(text)):
            found.add(next(g for g in match.groups() if g))
    return found


def _file_problems(paths: list[str], sizes: Mapping[str, int]) -> list[str]:
    problems: list[str] = []
    if len(paths) > MAX_FILES:
        problems.append(f"the template has {len(paths)} files; at most {MAX_FILES} are allowed")
    total = sum(sizes.values())
    if total > MAX_TOTAL_BYTES:
        problems.append(f"the template's files add up to more than {MAX_TOTAL_BYTES // 2**20} MB")
    for path in paths:
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            problems.append(f"{path}: file paths must stay inside the template's folder")
        elif len(pure.parts) == 1 and pure.stem.lower() in RESERVED_STEMS:
            problems.append(f"{path}: the name {pure.stem!r} is used by jaa's build; rename it")
        if pure.suffix.lower() not in TEXT_SUFFIXES | ASSET_SUFFIXES:
            problems.append(f"{path}: unsupported file type")
    return problems


def _access_problems(path: str, text: str) -> tuple[list[str], list[str]]:
    code = strip_comments(text)
    problems: list[str] = []
    for match in _FORBIDDEN.finditer(code):
        what = next(g for g in match.groups() if g)
        what = "piped \\input" if what == "|" else f"\\{what}"
        problems.append(f"{path}: {what} is not allowed (it can run code or write files)")
    if _OUTSIDE_PATH.search(code):
        problems.append(
            f"{path}: refers to a file outside the template's folder (an absolute, ~ or .. path)"
        )
    opaque = sorted({f"\\{m.group(1)}" for m in _OPAQUE.finditer(code)})
    warnings = (
        [
            f"{path}: uses {', '.join(opaque)}, which can hide which files it reads; "
            "add templates only from sources you trust"
        ]
        if opaque
        else []
    )
    return problems, warnings


def check_template(
    main_file: str,
    texts: Mapping[str, str],
    asset_sizes: Mapping[str, int],
    kind: TemplateKind,
    *,
    uploaded: bool = True,
) -> TemplateCheck:
    """Check a template against the contract.

    `texts` maps each text file's path (including `main_file`) to its source; `asset_sizes`
    maps each binary file's path to its size. `uploaded` adds the file and file-access checks
    that shipped templates, which are reviewed in this repository, do not need.
    """
    main = texts.get(main_file)
    if main is None:
        return TemplateCheck(
            kind=kind,
            family=TemplateFamily.CUSTOM,
            document_class=None,
            problems=(f"the main file {main_file} is not among the template's files",),
        )
    problems = slot_problems(main, kind)
    warnings: list[str] = []
    doc_class = document_class(main)
    if doc_class is None:
        problems.append(r"no \documentclass in the main file")
    problems += paper_problems(main)
    family = TemplateFamily.MODERNCV if doc_class == "moderncv" else TemplateFamily.CUSTOM
    if family is TemplateFamily.CUSTOM:
        missing = [m for m in REQUIRED_MACROS[kind] if m not in defined_macros(texts)]
        if missing:
            problems.append(
                "the template neither uses the moderncv class nor defines "
                + ", ".join(f"\\{m}" for m in missing)
            )
    if uploaded:
        sizes = {p: len(t.encode()) for p, t in texts.items()} | dict(asset_sizes)
        problems += _file_problems(sorted(sizes), sizes)
        for path, text in sorted(texts.items()):
            found, cautions = _access_problems(path, text)
            problems += found
            warnings += cautions
        if "\\hyphenpenalty=10000" not in main.replace(" ", ""):
            warnings.append(
                "hyphenation is not turned off (\\hyphenpenalty=10000); words split across "
                "lines can break keyword matching in applicant tracking systems"
            )
    return TemplateCheck(
        kind=kind,
        family=family,
        document_class=doc_class,
        problems=tuple(problems),
        warnings=tuple(warnings),
    )
