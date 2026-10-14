"""Read LaTeX CV sources into located text lines for the CV parser (ADR 0009).

There is no TeX engine here. A small tokenizer and converter understand the commands CVs use:
sectioning, lists, tables, text formatting, accents and symbols, links, the moderncv and
awesome-cv personal-data and entry macros, and simple user-defined `\\newcommand`s and
`\\newenvironment`s, which are expanded. Anything else is reported: unknown commands keep their
arguments as text and are listed, and environments that hold no readable text (pictures,
figures, listings) are skipped with a warning. Nothing is dropped silently.

`read_latex_project` is the only function here that touches the filesystem. It reads the main
file, the files it `\\input`s or `\\include`s, and local `.cls`/`.sty` files next to it, and
refuses paths that leave the main file's directory. `extract_latex` is pure.
"""

import posixpath
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Final

from jaa.core.models import JsonValue
from jaa.importers.base import (
    MAX_SOURCE_BYTES,
    EntryFields,
    ExtractedText,
    ImportIssue,
    IssueSeverity,
    SourceLine,
)
from jaa.importers.cv import heading_section
from jaa.importers.latex_vocabulary import (
    ACCENTS,
    BREAKS,
    DEF,
    DEFINE,
    DIMENSION,
    DIMENSION_CMDS,
    DROP,
    IGNORE,
    IMAGES,
    INCLUDES,
    LETTERS,
    LIST_ENVS,
    NEWENV,
    NO_LABEL,
    PACKAGES,
    PERSONAL,
    SECTIONS,
    SILENT_ENVS,
    SKIPPED_ENVS,
    SOCIAL_HOSTS,
    SUBSECTIONS,
    SYMBOLS,
    TABLE_ENVS,
    TRANSPARENT_ENVS,
    XPARSE,
)

# A CV project is a main file and a handful of section files.
MAX_PROJECT_FILES: Final = 30
# Bounds on macro expansion, so a recursive definition cannot loop or blow up memory.
MAX_EXPANSIONS: Final = 5000
MAX_TOKENS: Final = 500_000

_SAMPLE: Final = 256 * 1024
_LATEX_MARKER: Final = re.compile(rb"\\documentclass\b|\\begin\s*\{document\}")
_LATEX_FRAGMENT: Final = re.compile(
    rb"\\(?:section|subsection|cvsection|cventry|item)\b|\\begin\s*\{(?:itemize|tabular)\}"
)
_COMMENT: Final = re.compile(r"(?<!\\)%.*")
_BEGIN_DOCUMENT: Final = re.compile(r"\\begin\s*\{document\}")
_INCLUDE_REF: Final = re.compile(
    r"\\(?:input|include|subfile)\s*\{([^{}]+)\}|\\(?:sub)?import\s*\{([^{}]*)\}\s*\{([^{}]+)\}"
)
_CLASS_REF: Final = re.compile(r"\\(?:documentclass|LoadClass)\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}")
_PACKAGE_REF: Final = re.compile(
    r"\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}"
)


def is_latex(data: bytes) -> bool:
    """A LaTeX document: text that declares a document class or starts a document."""
    sample = data[:_SAMPLE]
    return b"\x00" not in sample and _LATEX_MARKER.search(sample) is not None


def is_latex_fragment(data: bytes) -> bool:
    """LaTeX without `\\documentclass`, such as a section file `\\input` by a main file."""
    sample = data[:_SAMPLE]
    return b"\x00" not in sample and _LATEX_FRAGMENT.search(sample) is not None


def decode_tex(data: bytes) -> tuple[str, bool]:
    """Decode a source file. Returns the text and whether the encoding had to be guessed."""
    try:
        return data.decode("utf-8-sig"), False
    except UnicodeDecodeError:
        # Pre-UTF-8 LaTeX files are usually Latin-1 (inputenc latin1); it never fails to decode.
        return data.decode("latin-1"), True


@dataclass(frozen=True, slots=True, kw_only=True)
class LatexProject:
    """The main file and the local files it uses, keyed by POSIX path relative to its folder."""

    main: str
    files: dict[str, str] = field(repr=False)
    issues: tuple[ImportIssue, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class LatexResult:
    extracted: ExtractedText
    document_class: str | None
    packages: tuple[str, ...]


def _tex_key(name: str) -> str:
    key = posixpath.normpath(name.strip().replace("\\", "/"))
    return key if key.endswith(".tex") else f"{key}.tex"


def _issue(code: str, message: str, location: str | None = None) -> ImportIssue:
    return ImportIssue(
        code=code, severity=IssueSeverity.WARNING, message=message, location=location
    )


def read_latex_project(path: Path, data: bytes) -> LatexProject:
    """Read a main `.tex` file (already read and size-checked) and the local files it uses.

    Included files must be `.tex` files inside the main file's folder. Local `.cls`/`.sty` files
    named by `\\documentclass`/`\\usepackage` are read when present; classes installed with TeX
    are not, and that is normal.
    """
    root = path.parent.resolve()
    text, guessed = decode_tex(data)
    files = {path.name: text}
    issues: list[ImportIssue] = []
    if guessed:
        issues.append(
            _issue(
                "latex_encoding_guessed",
                f"{path.name} is not UTF-8; it was read as Latin-1. Check accented characters "
                "during review.",
            )
        )
    total = len(data)
    queue = [path.name]
    while queue:
        current = queue.pop(0)
        stripped = "\n".join(_COMMENT.sub("", line) for line in files[current].splitlines())
        wanted: list[tuple[str, bool]] = []
        for match in _INCLUDE_REF.finditer(stripped):
            if match.group(1) is not None:
                wanted.append((match.group(1), True))
            else:
                wanted.append((posixpath.join(match.group(2), match.group(3)), True))
        for match in _CLASS_REF.finditer(stripped):
            wanted.append((f"{match.group(1).strip()}.cls", False))
        for match in _PACKAGE_REF.finditer(stripped):
            wanted.extend((f"{name.strip()}.sty", False) for name in match.group(1).split(","))
        for name, is_include in wanted:
            key = _tex_key(name) if is_include else posixpath.normpath(name)
            if key in files:
                continue
            target = (root / key).resolve()
            if not target.is_relative_to(root) or Path(key).is_absolute():
                if is_include:
                    issues.append(
                        _issue(
                            "latex_include_refused",
                            f"“{name}” is outside the CV's folder, so it was not read.",
                            current,
                        )
                    )
                continue
            if not target.is_file():
                if is_include:
                    issues.append(
                        _issue(
                            "latex_include_missing",
                            f"Included file “{name}” was not found next to the CV, so its "
                            "content is not imported.",
                            current,
                        )
                    )
                continue
            size = target.stat().st_size
            if len(files) >= MAX_PROJECT_FILES or total + size > MAX_SOURCE_BYTES:
                issues.append(
                    _issue(
                        "latex_include_limit",
                        f"“{name}” was not read: the CV uses more than {MAX_PROJECT_FILES} "
                        "files or 25 MB.",
                        current,
                    )
                )
                continue
            total += size
            content, guessed = decode_tex(target.read_bytes())
            if guessed:
                issues.append(
                    _issue(
                        "latex_encoding_guessed",
                        f"{key} is not UTF-8; it was read as Latin-1.",
                    )
                )
            files[key] = content
            queue.append(key)
    return LatexProject(main=path.name, files=files, issues=tuple(issues))


# --- tokens ----------------------------------------------------------------------------------


class _K(Enum):
    CMD = auto()
    TEXT = auto()
    OPEN = auto()
    CLOSE = auto()
    LBRACK = auto()
    RBRACK = auto()
    AMP = auto()
    TILDE = auto()
    MATH = auto()
    PARAM = auto()
    SPACE = auto()
    PAR = auto()


@dataclass(frozen=True, slots=True)
class _Tok:
    kind: _K
    value: str
    file: str
    line: int


_SPECIALS: Final = {
    "{": _K.OPEN,
    "}": _K.CLOSE,
    "[": _K.LBRACK,
    "]": _K.RBRACK,
    "&": _K.AMP,
    "~": _K.TILDE,
    "$": _K.MATH,
}
_TEXT_STOP: Final = frozenset("\\{}[]&~$#% \t\r\n")
# Markers spliced around an entry description so its lines are read as details.
_DETAILS_ON: Final = "\x00details-on"
_DETAILS_OFF: Final = "\x00details-off"


def _is_letter(char: str, *, at_letter: bool) -> bool:
    return (char.isascii() and char.isalpha()) or (at_letter and char == "@")


def _tokenize(text: str, file: str, *, at_letter: bool = False) -> list[_Tok]:
    """Split TeX source into tokens. `at_letter` treats `@` as a letter, as in classes."""
    toks: list[_Tok] = []
    i, line, n = 0, 1, len(text)
    while i < n:
        char = text[i]
        if char == "%":
            end = text.find("\n", i)
            if end < 0:
                break
            i = end
            continue
        if char == "\\":
            j = i + 1
            if j >= n:
                break
            if _is_letter(text[j], at_letter=at_letter):
                while j < n and _is_letter(text[j], at_letter=at_letter):
                    j += 1
                toks.append(_Tok(_K.CMD, text[i + 1 : j], file, line))
                while j < n and text[j] in " \t":
                    j += 1
            else:
                toks.append(_Tok(_K.CMD, text[j], file, line))
                if text[j] == "\n":
                    line += 1
                j += 1
            i = j
            continue
        if char in " \t\r\n":
            j, breaks = i, 0
            while j < n and text[j] in " \t\r\n":
                breaks += text[j] == "\n"
                j += 1
            toks.append(_Tok(_K.PAR if breaks >= 2 else _K.SPACE, " ", file, line))
            line += breaks
            i = j
            continue
        if kind := _SPECIALS.get(char):
            toks.append(_Tok(kind, char, file, line))
            i += 1
            continue
        if char == "#":
            if i + 1 < n and text[i + 1].isdigit():
                toks.append(_Tok(_K.PARAM, text[i + 1], file, line))
                i += 2
            else:
                toks.append(_Tok(_K.TEXT, "#", file, line))
                i += 1
            continue
        j = i
        while j < n and text[j] not in _TEXT_STOP:
            j += 1
        toks.append(_Tok(_K.TEXT, text[i:j], file, line))
        i = j
    return toks


def _env_marker(toks: list[_Tok], command: str, name: str) -> tuple[int, int] | None:
    """Find `\\begin{name}`/`\\end{name}`: (index of the command, index after the marker)."""
    for j, tok in enumerate(toks):
        if tok.kind is _K.CMD and tok.value == command:
            k = j + 1
            while k < len(toks) and toks[k].kind is _K.SPACE:
                k += 1
            window = toks[k : k + 3]
            if [t.kind for t in window] == [_K.OPEN, _K.TEXT, _K.CLOSE] and (
                window[1].value == name
            ):
                return j, k + 3
    return None


def _document_body(toks: list[_Tok]) -> list[_Tok]:
    """The body of a file that is a document of its own (the `subfiles` package)."""
    begin = _env_marker(toks, "begin", "document")
    if begin is None:
        return toks
    toks = toks[begin[1] :]
    end = _env_marker(toks, "end", "document")
    return toks if end is None else toks[: end[0]]


def _raw(toks: list[_Tok]) -> str:
    """Tokens as literal text, for URLs, environment names and option lists."""
    parts: list[str] = []
    for tok in toks:
        match tok.kind:
            case _K.TEXT:
                parts.append(tok.value)
            case _K.TILDE | _K.AMP | _K.LBRACK | _K.RBRACK:
                parts.append(tok.value)
            case _K.CMD if not _is_letter(tok.value[0], at_letter=True):
                parts.append(tok.value)
            case _K.SPACE:
                parts.append(" ")
            case _:
                pass
    return "".join(parts).strip()


def _ligatures(text: str) -> str:
    return text.replace("---", "—").replace("--", "–").replace("``", "“").replace("''", "”")


def _clean(text: str) -> str:
    text = " ".join(text.split())
    text = re.sub(r"(?:\s*\|\s*)+", " | ", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+([,.;:)])", r"\1", text)
    return text.strip(" |•·")


# --- converter -------------------------------------------------------------------------------


@dataclass(slots=True)
class _Macro:
    nargs: int
    default: list[_Tok] | None
    body: list[_Tok]
    end: list[_Tok] | None = None


@dataclass(slots=True)
class _List:
    bullets: bool
    in_item: bool = False


class _Mode(Enum):
    DEFINITIONS = auto()  # a local class or package: only collect definitions
    PREAMBLE = auto()
    BODY = auto()


@dataclass(slots=True)
class _Shared:
    files: dict[str, str]
    main: str
    macros: dict[str, _Macro] = field(default_factory=dict[str, _Macro])
    envs: dict[str, _Macro] = field(default_factory=dict[str, _Macro])
    included: set[str] = field(default_factory=set[str])
    unknown: Counter[str] = field(default_factory=Counter[str])
    unknown_envs: Counter[str] = field(default_factory=Counter[str])
    skipped: Counter[str] = field(default_factory=Counter[str])
    first_seen: dict[str, str] = field(default_factory=dict[str, str])
    repeated_includes: list[str] = field(default_factory=list[str])
    unsupported_definitions: int = 0
    document_class: str | None = None
    packages: list[str] = field(default_factory=list[str])
    expansions: int = 0
    expansion_limited: bool = False
    in_header: bool = True
    math: bool = False
    stop: bool = False
    # Personal data from preamble macros: (priority, order, line); emitted at the top.
    personal: list[tuple[int, int, SourceLine]] = field(
        default_factory=list[tuple[int, int, SourceLine]]
    )
    first_name: tuple[str, str] | None = None
    last_name: tuple[str, str] | None = None

    def location(self, tok: _Tok) -> str:
        return f"line {tok.line}" if tok.file == self.main else f"{tok.file} line {tok.line}"

    def note(self, counter: Counter[str], key: str, tok: _Tok) -> None:
        counter[key] += 1
        self.first_seen.setdefault(key, self.location(tok))


class _Converter:
    def __init__(
        self, shared: _Shared, toks: list[_Tok], *, mode: _Mode, inline: bool = False
    ) -> None:
        self.s = shared
        self.toks = toks
        self.i = 0
        self.mode = mode
        self.inline = inline
        self.lines: list[SourceLine] = []
        self.buf: list[str] = []
        self.buf_tok: _Tok | None = None
        self.lists: list[_List] = []
        self.envs: list[tuple[str, str]] = []  # (name, kind) of open environments
        self.tables = 0
        self.details = 0

    # --- token navigation

    def _skip_spaces(self) -> None:
        while self.i < len(self.toks) and self.toks[self.i].kind is _K.SPACE:
            self.i += 1

    def _peek(self) -> _Tok | None:
        j = self.i
        while j < len(self.toks) and self.toks[j].kind is _K.SPACE:
            j += 1
        return self.toks[j] if j < len(self.toks) else None

    def _close_of(self, start: int) -> int:
        depth = 0
        for j in range(start, len(self.toks)):
            kind = self.toks[j].kind
            if kind is _K.OPEN:
                depth += 1
            elif kind is _K.CLOSE:
                depth -= 1
                if depth == 0:
                    return j
        return len(self.toks)

    def _arg(self) -> list[_Tok] | None:
        """A required argument: a braced group, or a single token as TeX does."""
        self._skip_spaces()
        if self.i >= len(self.toks):
            return None
        tok = self.toks[self.i]
        if tok.kind is _K.OPEN:
            end = self._close_of(self.i)
            inner = self.toks[self.i + 1 : end]
            self.i = end + 1
            return inner
        if tok.kind is _K.TEXT:
            if len(tok.value) > 1:
                self.toks[self.i] = _Tok(_K.TEXT, tok.value[1:], tok.file, tok.line)
            else:
                self.i += 1
            return [_Tok(_K.TEXT, tok.value[0], tok.file, tok.line)]
        if tok.kind in (_K.CMD, _K.TILDE, _K.PARAM):
            self.i += 1
            return [tok]
        return None

    def _opt(self) -> list[_Tok] | None:
        """An optional `[...]` argument, if one follows."""
        nxt = self._peek()
        if nxt is None or nxt.kind is not _K.LBRACK:
            return None
        self._skip_spaces()
        depth, k = 0, self.i + 1
        while k < len(self.toks):
            kind = self.toks[k].kind
            if kind is _K.OPEN:
                depth += 1
            elif kind is _K.CLOSE:
                depth -= 1
            elif kind is _K.RBRACK and depth <= 0:
                break
            k += 1
        inner = self.toks[self.i + 1 : k]
        self.i = k + 1
        return inner

    def _star(self) -> None:
        if self.i < len(self.toks):
            tok = self.toks[self.i]
            if tok.kind is _K.TEXT and tok.value.startswith("*"):
                if len(tok.value) > 1:
                    self.toks[self.i] = _Tok(_K.TEXT, tok.value[1:], tok.file, tok.line)
                else:
                    self.i += 1

    def _count_groups(self) -> int:
        j, count = self.i, 0
        while True:
            while j < len(self.toks) and self.toks[j].kind is _K.SPACE:
                j += 1
            if j >= len(self.toks) or self.toks[j].kind is not _K.OPEN:
                return count
            j = self._close_of(j) + 1
            count += 1

    def _consume(self, spec: str) -> list[list[_Tok] | None]:
        return [self._opt() if kind == "o" else self._arg() for kind in spec]

    def _skip_args(self) -> None:
        while (nxt := self._peek()) is not None and nxt.kind in (_K.OPEN, _K.LBRACK):
            if nxt.kind is _K.LBRACK:
                self._opt()
            else:
                self._arg()

    def _splice(self, toks: list[_Tok]) -> None:
        self.toks[self.i : self.i] = toks

    # --- output

    def _render(self, toks: list[_Tok] | None) -> str:
        if not toks:
            return ""
        sub = _Converter(self.s, list(toks), mode=_Mode.BODY, inline=True)
        sub.run()
        return _clean("".join(sub.buf))

    def _put(self, text: str, tok: _Tok) -> None:
        if self.buf_tok is None and text.strip():
            self.buf_tok = tok
        self.buf.append(text)

    def _bullet(self) -> bool:
        if self.tables:
            return False
        if self.details:
            return True
        return bool(self.lists) and self.lists[-1].bullets and self.lists[-1].in_item

    def _newline(self) -> None:
        if self.inline:
            self.buf.append(" ")
            return
        text = _clean("".join(self.buf))
        if text and self.buf_tok is not None:
            self.lines.append(
                SourceLine(
                    text=text, location=self.s.location(self.buf_tok), is_bullet=self._bullet()
                )
            )
        self.buf = []
        self.buf_tok = None

    def _emit(
        self,
        text: str,
        tok: _Tok,
        *,
        bullet: bool = False,
        heading: bool = False,
        entry: EntryFields | None = None,
    ) -> None:
        self._newline()
        text = _clean(text)
        if not text:
            return
        if self.inline:
            self.buf.append(f" {text} ")
            return
        self.lines.append(
            SourceLine(
                text=text,
                location=self.s.location(tok),
                is_heading=heading,
                is_bullet=(bullet or self._bullet()) and not heading and entry is None,
                entry=entry,
            )
        )

    # --- main loop

    def run(self) -> None:
        while self.i < len(self.toks) and not self.s.stop:
            tok = self.toks[self.i]
            self.i += 1
            if tok.kind is _K.CMD:
                if self.mode is _Mode.BODY:
                    self._command(tok)
                else:
                    self._setup_command(tok)
                continue
            if self.mode is not _Mode.BODY:
                continue
            match tok.kind:
                case _K.TEXT:
                    value = _ligatures(tok.value)
                    if self.s.math:
                        value = value.replace("^", "").replace("_", "")
                    self._put(value, tok)
                case _K.SPACE | _K.TILDE:
                    self._put(" ", tok)
                case _K.PAR:
                    self._newline()
                case _K.AMP:
                    self._put(" | ", tok)
                case _K.MATH:
                    self.s.math = not self.s.math
                case _K.LBRACK | _K.RBRACK:
                    self._put(tok.value, tok)
                case _:
                    pass  # braces group formatting only; stray parameters are ignored

    def finish(self) -> None:
        self._newline()
        self._flush_personal()

    # --- preamble and class files

    def _setup_command(self, tok: _Tok) -> None:
        name = tok.value
        if name in DEFINE or name in DEF or name in NEWENV or name in XPARSE or name == "let":
            self._define(tok)
        elif self.mode is _Mode.DEFINITIONS:
            self._skip_args()
        elif name in PACKAGES:
            self._package(tok)
        elif name in INCLUDES:
            self._include(tok)
        elif name in PERSONAL:
            self._personal(tok)
        elif name == "begin":
            env = _raw(self._arg() or [])
            if env == "document":
                self._flush_personal()
                self.mode = _Mode.BODY
        else:
            self._skip_args()

    def _package(self, tok: _Tok) -> None:
        self._opt()
        names = [n.strip() for n in _raw(self._arg() or []).split(",") if n.strip()]
        if tok.value in ("documentclass", "LoadClass"):
            if self.s.document_class is None and names:
                self.s.document_class = names[0]
        else:
            self.s.packages.extend(n for n in names if n not in self.s.packages)

    def _personal(self, tok: _Tok) -> None:
        name = tok.value
        opt = self._opt()
        if name == "photo":
            self._opt()
            self._arg()
            self.s.note(self.s.skipped, "image", tok)
            return
        limit = {"name": 2, "address": 3, "stackoverflow": 2}.get(name, 1)
        count = min(self._count_groups(), limit) or 1
        args = [self._render(self._arg()) for _ in range(count)]
        values = [a for a in args if a]
        if not values:
            return
        priority, text = 2, values[0]
        match name:
            case "name" | "author":
                priority, text = 0, " ".join(values)
            case "firstname":
                self.s.first_name = (values[0], self.s.location(tok))
                return
            case "familyname" | "lastname":
                self.s.last_name = (values[0], self.s.location(tok))
                return
            case "title" | "position":
                priority = 1
            case "homepage":
                if not re.match(r"(?i)https?://", text) and "." in text:
                    text = f"https://{text}"
            case "social" | "github" | "gitlab" | "linkedin" | "twitter" | "orcid":
                site = (_raw(opt or []).lower() or "social") if name == "social" else name
                host = SOCIAL_HOSTS.get(site)
                if host and "." not in text.split("/")[0]:
                    text = f"{host}{text}"
                elif not host:
                    text = f"{site}: {text}"
            case "stackoverflow":
                text = f"stackoverflow: {values[-1]}"
            case "address":
                priority, text = 3, ", ".join(values)
            case "extrainfo" | "quote":
                priority = 4
            case _:
                pass
        line = SourceLine(text=text, location=self.s.location(tok))
        self.s.personal.append((priority, len(self.s.personal), line))

    def _flush_personal(self) -> None:
        if self.s.first_name or self.s.last_name:
            parts = [p for p in (self.s.first_name, self.s.last_name) if p]
            text = " ".join(p[0] for p in parts)
            self.s.personal.append((0, -1, SourceLine(text=text, location=parts[0][1])))
            self.s.first_name = self.s.last_name = None
        self.lines.extend(line for _, _, line in sorted(self.s.personal, key=lambda p: p[:2]))
        self.s.personal.clear()

    # --- definitions and expansion

    def _command_name(self, toks: list[_Tok] | None) -> str | None:
        for tok in toks or []:
            if tok.kind is _K.CMD:
                return tok.value
        return None

    def _define(self, tok: _Tok) -> None:
        kind = tok.value
        self._star()
        if kind in XPARSE:
            self._consume("mmm")
            self.s.unsupported_definitions += 1
            return
        if kind == "let":
            self._skip_spaces()
            target = self._arg()
            nxt = self._peek()
            if nxt is not None and nxt.kind is _K.TEXT and nxt.value == "=":
                self._skip_spaces()
                self.i += 1
            source = self._arg()
            name = self._command_name(target)
            if name and source:
                existing = self.s.macros.get(source[0].value) if source[0].kind is _K.CMD else None
                self.s.macros[name] = existing or _Macro(0, None, list(source))
            return
        if kind in DEF:
            self._skip_spaces()
            target = self._arg()
            params: list[_Tok] = []
            while self.i < len(self.toks) and self.toks[self.i].kind is not _K.OPEN:
                params.append(self.toks[self.i])
                self.i += 1
            body = self._arg() or []
            name = self._command_name(target)
            numbered = [p.value for p in params] == [str(n) for n in range(1, len(params) + 1)]
            if name is None or not all(p.kind is _K.PARAM for p in params) or not numbered:
                # Delimited parameters (`\def\x#1.{…}`) need a real TeX engine.
                self.s.unsupported_definitions += 1
                return
            self.s.macros[name] = _Macro(len(params), None, body)
            return
        if kind in NEWENV:
            env = _raw(self._arg() or [])
            nargs, default = self._opt(), self._opt()
            begin, end = self._arg() or [], self._arg() or []
            if env:
                count = int(_raw(nargs)) if nargs and _raw(nargs).isdigit() else 0
                self.s.envs[env] = _Macro(count, default, begin, end)
            return
        target = self._arg()
        nargs, default = self._opt(), self._opt()
        body = self._arg() or []
        name = self._command_name(target)
        if name is None or (kind == "providecommand" and name in self.s.macros):
            return
        count = int(_raw(nargs)) if nargs and _raw(nargs).isdigit() else 0
        self.s.macros[name] = _Macro(count, default, body)

    def _substitute(self, macro_body: list[_Tok], args: list[list[_Tok]], at: _Tok) -> list[_Tok]:
        out: list[_Tok] = []
        for tok in macro_body:
            if tok.kind is _K.PARAM:
                index = int(tok.value) - 1
                if 0 <= index < len(args):
                    out.extend(args[index])  # arguments keep their own source locations
            else:
                # Text from the definition is reported at the place the macro is used.
                out.append(_Tok(tok.kind, tok.value, at.file, at.line))
        return out

    def _macro_args(self, macro: _Macro) -> list[list[_Tok]]:
        args: list[list[_Tok]] = []
        if macro.default is not None:
            opt = self._opt()
            args.append(opt if opt is not None else macro.default)
        while len(args) < macro.nargs:
            args.append(self._arg() or [])
        return args

    def _can_expand(self) -> bool:
        if self.s.expansions >= MAX_EXPANSIONS or len(self.toks) > MAX_TOKENS:
            self.s.expansion_limited = True
            return False
        self.s.expansions += 1
        return True

    # --- body commands

    def _command(self, tok: _Tok) -> None:
        name = tok.value
        if name == _DETAILS_ON:
            self._newline()
            self.details += 1
        elif name == _DETAILS_OFF:
            self._newline()
            self.details -= 1
        elif name in SECTIONS or name in SUBSECTIONS:
            self._section(tok)
        elif name in BREAKS:
            if name == "\\":
                self._star()
                self._opt()
            self._newline()
        elif name == "item":
            self._item(tok)
        elif name == "begin":
            self._begin(tok)
        elif name == "end":
            self._end(tok)
        elif name in ("href", "url", "nolinkurl"):
            self._link(tok)
        elif name in INCLUDES:
            self._include(tok)
        elif name in PACKAGES:
            self._package(tok)
        elif name in DEFINE or name in DEF or name in NEWENV or name in XPARSE or name == "let":
            self._define(tok)
        elif name.startswith("cv") and self._cv_macro(tok):
            pass
        elif name in IMAGES:
            self._consume("oom" if name == "photo" else "om")
            self.s.note(self.s.skipped, "image", tok)
        elif name in SYMBOLS:
            self._put(SYMBOLS[name], tok)
        elif name in ACCENTS:
            self._accent(tok)
        elif name in LETTERS:
            self._put(LETTERS[name], tok)
        elif name in IGNORE or re.match(r"fa[A-Z]", name):
            pass
        elif name in DROP:
            self._star()
            self._consume(DROP[name])
        elif name in DIMENSION_CMDS:
            self._skip_dimension()
        elif name in self.s.macros and self._can_expand():
            macro = self.s.macros[name]
            self._splice(self._substitute(macro.body, self._macro_args(macro), tok))
        else:
            self._unknown(tok)

    def _skip_dimension(self) -> None:
        for _ in range(8):
            nxt = self._peek()
            if nxt is None:
                return
            if (nxt.kind is _K.TEXT and DIMENSION.match(nxt.value)) or nxt.kind is _K.CMD:
                self._skip_spaces()
                self.i += 1
            else:
                return

    def _unknown(self, tok: _Tok) -> None:
        if "@" not in tok.value:  # internal class commands are not the author's markup
            self.s.note(self.s.unknown, f"\\{tok.value}", tok)
        parts: list[str] = []
        while (nxt := self._peek()) is not None and nxt.kind in (_K.OPEN, _K.LBRACK):
            part = self._render(self._opt() if nxt.kind is _K.LBRACK else self._arg())
            if part:
                parts.append(part)
        if parts:
            self._put(f" {' | '.join(parts)} ", tok)

    def _section(self, tok: _Tok) -> None:
        self._star()
        self._opt()
        title = self._render(self._arg())
        top = tok.value in SECTIONS
        if top or heading_section(title) is not None:
            self.s.in_header = False
        self._emit(title, tok, heading=top)

    def _item(self, tok: _Tok) -> None:
        label = self._opt()
        self._newline()
        if self.lists:
            self.lists[-1].in_item = True
        if text := self._render(label):
            self._put(f"{text}: ", tok)

    def _link(self, tok: _Tok) -> None:
        url = _raw(self._arg() or [])
        label = self._render(self._arg()) if tok.value == "href" else ""
        if tok.value == "href" and not self.s.in_header:
            self._put(label or url, tok)
            return
        # In the header the link target is the contact detail; the label is often just an icon.
        self._put(f" {re.sub(r'(?i)^(?:mailto|tel):', '', url)} ", tok)

    def _accent(self, tok: _Tok) -> None:
        base = self._render(self._arg())
        if not base:
            return
        base = base.replace("ı", "i").replace("ȷ", "j")
        self._put(unicodedata.normalize("NFC", base[0] + ACCENTS[tok.value] + base[1:]), tok)

    def _include(self, tok: _Tok) -> None:
        if tok.value in ("import", "subimport"):
            folder = _raw(self._arg() or [])
            key = _tex_key(posixpath.join(folder, _raw(self._arg() or [])))
        else:
            key = _tex_key(_raw(self._arg() or []))
        if key not in self.s.files:
            return  # reported by read_latex_project
        if key in self.s.included:
            self.s.repeated_includes.append(key)
            return
        self.s.included.add(key)
        self._splice(_document_body(_tokenize(self.s.files[key], key)))

    def _cv_macro(self, tok: _Tok) -> bool:
        """moderncv and awesome-cv content macros. Returns False if `tok` is not one of them."""
        name = tok.value
        match name:
            case "cventry":
                self._cventry(tok)
            case "cvitem" | "cvline":
                self._opt()
                label, text = self._render(self._arg()), self._render(self._arg())
                # An unlabelled item is a description line under the entry above it.
                line = f"{label}: {text}" if label and text else label or text
                self._emit(line, tok, bullet=not label)
            case "cvitemwithcomment":
                self._opt()
                label, text, comment = (self._render(self._arg()) for _ in range(3))
                body = f"{text} ({comment})" if comment else text
                self._emit(f"{label}: {body}" if label else body, tok, bullet=not label)
            case "cvdoubleitem" | "cvcomputer":
                self._opt()
                values = [self._render(self._arg()) for _ in range(4)]
                for label, text in (values[:2], values[2:]):
                    self._emit(f"{label}: {text}" if label and text else label or text, tok)
            case "cvlistitem":
                self._opt()
                self._emit(self._render(self._arg()), tok, bullet=True)
            case "cvlistdoubleitem":
                self._opt()
                for _ in range(2):
                    self._emit(self._render(self._arg()), tok, bullet=True)
            case "cvlanguage":
                language, level, comment = (self._render(self._arg()) for _ in range(3))
                detail = ", ".join(p for p in (level, comment) if p)
                self._emit(f"{language} ({detail})" if detail else language, tok)
            case "cvskill":
                kind, skills = self._render(self._arg()), self._render(self._arg())
                self._emit(f"{kind}: {skills}" if kind else skills, tok)
            case "cvhonor" | "cvsubentry":
                count = min(self._count_groups(), 4 if name == "cvhonor" else 3)
                self._emit(" | ".join(self._render(self._arg()) for _ in range(count)), tok)
            case _:
                return False
        return True

    def _cventry(self, tok: _Tok) -> None:
        self._opt()
        count = self._count_groups()
        awesome = self.s.document_class == "awesome-cv" or count == 5
        if count < 5 or (count < 6 and not awesome):
            self._unknown(tok)
            return
        args = [self._arg() or [] for _ in range(5 if awesome else 6)]
        description = args[4] if awesome else args[5]
        if awesome:
            role, org, place, dates = (self._render(a) for a in args[:4])
            extra = (place,)
        else:
            dates, role, org, place, grade = (self._render(a) for a in args[:5])
            extra = (place, grade)
        extra = tuple(e for e in extra if e)
        text = " | ".join(p for p in (role, org, dates, *extra) if p)
        entry = EntryFields(
            role=role or None, organization=org or None, dates=dates or None, extra=extra
        )
        self._emit(text, tok, entry=entry)
        if description:
            marker = (tok.file, tok.line)
            self._splice(
                [
                    _Tok(_K.CMD, _DETAILS_ON, *marker),
                    *description,
                    _Tok(_K.CMD, _DETAILS_OFF, *marker),
                ]
            )

    # --- environments

    def _begin(self, tok: _Tok) -> None:
        env = _raw(self._arg() or [])
        if env in LIST_ENVS:
            options = _raw(self._opt() or [])
            self._newline()
            self.lists.append(_List(bullets=NO_LABEL.search(options) is None))
            self.envs.append((env, "list"))
        elif env in TABLE_ENVS:
            self._consume(TABLE_ENVS[env])
            self._newline()
            self.tables += 1
            self.envs.append((env, "table"))
        elif env in TRANSPARENT_ENVS:
            self._consume(TRANSPARENT_ENVS[env])
            self._newline()
            self.envs.append((env, "block"))
        elif env in SKIPPED_ENVS or env in SILENT_ENVS:
            if env in SKIPPED_ENVS:
                self.s.note(self.s.skipped, env, tok)
            self._skip_environment(env)
        elif env in self.s.envs and self._can_expand():
            macro = self.s.envs[env]
            body = self._substitute(macro.body, self._macro_args(macro), tok)
            self._newline()
            self.envs.append((env, "user"))
            self._splice(body)
        else:
            self.s.note(self.s.unknown_envs, env, tok)
            parts: list[str] = []
            while (nxt := self._peek()) is not None and nxt.kind in (_K.OPEN, _K.LBRACK):
                part = self._render(self._opt() if nxt.kind is _K.LBRACK else self._arg())
                if part:
                    parts.append(part)
            # Custom section environments (`\begin{rSection}{Experience}`) name the section.
            heading = len(parts) == 1 and heading_section(parts[0]) is not None
            if heading:
                self.s.in_header = False
            self._emit(" | ".join(parts), tok, heading=heading)
            self.envs.append((env, "block"))

    def _end(self, tok: _Tok) -> None:
        env = _raw(self._arg() or [])
        if env == "document":
            self._newline()
            self.s.stop = True
            return
        names = [name for name, _ in self.envs]
        if env not in names:
            return
        while self.envs:
            name, kind = self.envs.pop()
            self._newline()
            if kind == "list" and self.lists:
                self.lists.pop()
            elif kind == "table":
                self.tables -= 1
            elif kind == "user" and (macro := self.s.envs.get(name)) and macro.end:
                self._splice(self._substitute(macro.end, [], tok))
            if name == env:
                return

    def _skip_environment(self, env: str) -> None:
        depth = 1
        while self.i < len(self.toks):
            tok = self.toks[self.i]
            self.i += 1
            if tok.kind is _K.CMD and tok.value in ("begin", "end"):
                saved = self.i
                if _raw(self._arg() or []) == env:
                    depth += 1 if tok.value == "begin" else -1
                    if depth == 0:
                        return
                else:
                    self.i = saved


# --- entry point -----------------------------------------------------------------------------


def _counted(counter: Counter[str], limit: int = 8) -> str:
    items = [f"{name} ×{n}" if n > 1 else name for name, n in counter.most_common(limit)]
    if len(counter) > limit:
        items.append(f"and {len(counter) - limit} more")
    return ", ".join(items)


def _report(s: _Shared) -> list[ImportIssue]:
    issues: list[ImportIssue] = []
    if s.unknown:
        first = s.first_seen[s.unknown.most_common(1)[0][0]]
        issues.append(
            _issue(
                "latex_unknown_command",
                f"{len(s.unknown)} LaTeX command{'s are' if len(s.unknown) != 1 else ' is'} not "
                f"known to the importer: {_counted(s.unknown)}. Their arguments were kept as "
                "text; check the facts from those lines.",
                first,
            )
        )
    if s.unknown_envs:
        first = s.first_seen[s.unknown_envs.most_common(1)[0][0]]
        issues.append(
            _issue(
                "latex_unknown_environment",
                f"Unknown environment{'s' if len(s.unknown_envs) != 1 else ''} "
                f"{_counted(s.unknown_envs)}: their content was read as plain text.",
                first,
            )
        )
    for env, count in sorted(s.skipped.items()):
        if env == "image":
            message = (
                f"{count} image{'s were' if count != 1 else ' was'} not imported (photos and "
                "logos are not profile facts)."
            )
            code = "latex_image_skipped"
        else:
            message = (
                f"{count} “{env}” environment{'s were' if count != 1 else ' was'} skipped: "
                "the importer cannot read text from it. Add anything important during review."
            )
            code = "latex_content_skipped"
        issues.append(_issue(code, message, s.first_seen[env]))
    if s.unsupported_definitions:
        issues.append(
            _issue(
                "latex_definition_unsupported",
                f"{s.unsupported_definitions} custom command definition"
                f"{'s use' if s.unsupported_definitions != 1 else ' uses'} a form the importer "
                "cannot expand (delimited \\def or \\NewDocumentCommand).",
            )
        )
    if s.expansion_limited:
        issues.append(
            _issue(
                "latex_expansion_limit",
                "Custom commands expand too deeply (a recursive definition?); expansion "
                "stopped and the remaining uses are listed as unknown commands.",
            )
        )
    for key in sorted(set(s.repeated_includes)):
        message = f"“{key}” is included more than once; it was read the first time only."
        issues.append(_issue("latex_include_repeated", message))
    return issues


def extract_latex(project: LatexProject) -> LatexResult:
    """Convert a LaTeX project into located text lines, issues and template metadata."""
    shared = _Shared(files=project.files, main=project.main)
    for key in sorted(k for k in project.files if k.endswith((".cls", ".sty"))):
        toks = _tokenize(project.files[key], key, at_letter=True)
        _Converter(shared, toks, mode=_Mode.DEFINITIONS).run()

    main_text = project.files[project.main]
    stripped = "\n".join(_COMMENT.sub("", line) for line in main_text.splitlines())
    has_document = _BEGIN_DOCUMENT.search(stripped) is not None
    shared.included.add(project.main)
    converter = _Converter(
        shared,
        _tokenize(main_text, project.main),
        mode=_Mode.PREAMBLE if has_document else _Mode.BODY,
    )
    converter.run()
    converter.finish()

    issues = [*project.issues]
    if not has_document:
        issues.append(
            _issue(
                "latex_no_document",
                "No \\begin{document} was found, so the whole file was read as CV content.",
            )
        )
    issues.extend(_report(shared))
    stats: dict[str, JsonValue] = {
        "files": len(project.files),
        "document_class": shared.document_class,
        "custom_definitions": len(shared.macros) + len(shared.envs),
    }
    return LatexResult(
        extracted=ExtractedText(lines=tuple(converter.lines), issues=tuple(issues), stats=stats),
        document_class=shared.document_class,
        packages=tuple(shared.packages),
    )
