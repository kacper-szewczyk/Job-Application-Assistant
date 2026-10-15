"""Rendering a `CvContent` into LaTeX source with a template (ADR 0012, ADR 0029).

A shipped template is a folder in `jaa/documents/templates/` with `template.json` (metadata)
and `template.tex`; uploaded templates come from `jaa.storage.templates`. Lines
`%%jaa:<slot>%%` in the .tex are replaced with generated LaTeX. All fact text goes through
`escape`, so a fact can never become a LaTeX command.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Final, cast

from jaa.core.errors import TemplateError
from jaa.core.models import JsonValue, TemplateFamily, TemplateKind
from jaa.documents.content import LABELS, CvContent, CvEntry, CvLanguage, format_period
from jaa.documents.contract import SLOT_LINE, check_template

_SPECIAL: Final = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "^": r"\textasciicircum{}",
    "_": r"\_",
    "%": r"\%",
    "~": r"\textasciitilde{}",
}
_SPECIAL_RE: Final = re.compile("[" + re.escape("".join(_SPECIAL)) + "]")
# Control characters (other than whitespace) would end up as invalid input to the engine.
_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def escape(text: str) -> str:
    """Make arbitrary text safe as LaTeX body text. Line breaks become spaces."""
    text = _CONTROL_RE.sub("", text)
    text = " ".join(text.split())
    return _SPECIAL_RE.sub(lambda m: _SPECIAL[m.group(0)], text)


@dataclass(frozen=True, slots=True, kw_only=True)
class CvTemplate:
    id: str
    version: int
    name: str
    family: TemplateFamily
    description: str
    license: str
    kind: TemplateKind = TemplateKind.CV
    # The main .tex file with its slot lines.
    source: str = field(repr=False)
    # Recorded in each manifest: sha256 of template.tex for a shipped template, of all its
    # files for an uploaded one (ADR 0029).
    sha256: str
    # Uploaded templates only: the other files (classes, packages, images, fonts) by POSIX
    # path, copied next to the .tex before compiling.
    assets: Mapping[str, bytes] = field(default_factory=dict[str, bytes], repr=False)
    uploaded: bool = False

    @property
    def version_label(self) -> str:
        return f"{self.id}@{self.version}"


def _templates_root() -> Traversable:
    return resources.files("jaa.documents").joinpath("templates")


def _load(folder: Traversable) -> CvTemplate:
    try:
        meta = json.loads(folder.joinpath("template.json").read_text(encoding="utf-8"))
        source = folder.joinpath("template.tex").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateError(f"template {folder.name} is unreadable: {exc}") from exc
    if not isinstance(meta, dict):
        raise TemplateError(f"template {folder.name}: template.json must be an object")
    data = cast(dict[str, object], meta)
    try:
        template = CvTemplate(
            id=str(data["id"]),
            version=int(cast(int, data["version"])),
            name=str(data["name"]),
            family=TemplateFamily(str(data["family"])),
            description=str(data.get("description", "")),
            license=str(data["license"]),
            kind=TemplateKind(str(data.get("kind", TemplateKind.CV))),
            source=source,
            sha256=hashlib.sha256(source.encode()).hexdigest(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TemplateError(f"template {folder.name}: missing or invalid {exc}") from exc
    if template.id != folder.name:
        raise TemplateError(f"template {folder.name}: id {template.id!r} must match its folder")
    check = check_template(
        "template.tex", {"template.tex": source}, {}, template.kind, uploaded=False
    )
    if not check.ok or check.family is not template.family:
        problems = list(check.problems) or [f"family is {check.family.value}"]
        raise TemplateError(f"template {template.id}: " + "; ".join(problems))
    return template


def available_templates(kind: TemplateKind | None = None) -> dict[str, CvTemplate]:
    """Every template shipped with jaa (of one kind, if given), by id."""
    root = _templates_root()
    folders = sorted((f for f in root.iterdir() if f.is_dir()), key=lambda f: f.name)
    loaded = (_load(f) for f in folders if f.joinpath("template.json").is_file())
    return {t.id: t for t in loaded if kind is None or t.kind is kind}


DEFAULT_TEMPLATE: Final = "moderncv-classic"
DEFAULT_LETTER_TEMPLATE: Final = "moderncv-letter"


def get_template(template_id: str, kind: TemplateKind = TemplateKind.CV) -> CvTemplate:
    """The shipped template `template_id` (or `id@version`), which must be a `kind` template."""
    template_id, _, version = template_id.partition("@")
    templates = available_templates(kind)
    if template_id not in templates:
        known = ", ".join(sorted(templates))
        raise TemplateError(f"unknown {kind.value} template {template_id!r}; available: {known}")
    template = templates[template_id]
    if version and version != str(template.version):
        raise TemplateError(f"{template_id} has version {template.version} only, not {version}")
    return template


# moderncv rendering ------------------------------------------------------------------------


def _arg(text: str | None) -> str:
    return "{" + (escape(text) if text else "") + "}"


def _name_parts(name: str | None) -> tuple[str, str]:
    # moderncv wants first and family name separately. Splitting at the last space keeps the
    # whole name in the header, whatever the split; it is never shown separately.
    if not name:
        return "", ""
    first, _, family = name.rpartition(" ")
    return (first, family) if first else (family, "")


def _name_arg(text: str) -> str:
    # Hyphenation is off, and the name is set large in a column that narrows when there is a
    # photo, so let double-barrelled names ("Nowak-Kowalska") wrap after their hyphen.
    return "{" + escape(text).replace("-", "-\\allowbreak{}") + "}"


def _header(content: CvContent, photo_file: str | None) -> str:
    first, family = _name_parts(content.name)
    lines = [f"\\name{_name_arg(first)}{_name_arg(family)}"]
    if content.headline:
        lines.append(f"\\title{_arg(content.headline)}")
    lines.extend(_contact(content))
    if photo_file:
        lines.append(f"\\photo[64pt][0.4pt]{_arg(photo_file)}")
    if content.summary:
        lines.append(f"\\quote{_arg(content.summary)}")
    return "\n".join(lines)


def sender_header(content: CvContent) -> str:
    """The name and contact details only: the sender block of a cover letter."""
    first, family = _name_parts(content.name)
    return "\n".join([f"\\name{_name_arg(first)}{_name_arg(family)}", *_contact(content)])


def _contact(content: CvContent) -> list[str]:
    lines: list[str] = []
    if content.location:
        lines.append(f"\\address{_arg(content.location)}{{}}{{}}")
    if content.phone:
        lines.append(f"\\mobile{_arg(content.phone)}")
    if content.email:
        lines.append(f"\\email{_arg(content.email)}")
    if content.links:
        # Shown as text, not icons, so the URLs survive text extraction.
        joined = " | ".join(url for _, url in content.links)
        lines.append(f"\\extrainfo{_arg(joined)}")
    return lines


# A private-use character marks where the dash goes; it cannot occur in date text.
_DASH_MARK: Final = chr(0xE000)


def _period(dates: JsonValue, language: CvLanguage) -> str:
    # The date column is narrow and hyphenation is off, so allow a line break after the dash.
    period = format_period(dates, language, dash=_DASH_MARK) or ""
    return escape(period).replace(_DASH_MARK, "--\\allowbreak{}")


def _entry(entry: CvEntry, language: CvLanguage, *, bold_org: bool) -> list[str]:
    period = _period(entry.dates, language)
    org = escape(entry.organization) if entry.organization else ""
    if org and bold_org:
        org = f"\\textsc{{{org}}}"
    lines: list[str] = []
    if entry.items and not bold_org:
        # Education details go into the entry's description argument.
        details = escape("; ".join(entry.items))
        lines.append(
            f"\\cventry{{{period}}}{_arg(entry.role)}{{{org}}}"
            f"{_arg(entry.location)}{{}}{{{details}}}"
        )
        return lines
    lines.append(f"\\cventry{{{period}}}{_arg(entry.role)}{{{org}}}{_arg(entry.location)}{{}}{{}}")
    lines.extend(
        f"\\cvlistitem{{\\textbf{_arg(item)}}}"
        if i in entry.emphasized
        else f"\\cvlistitem{_arg(item)}"
        for i, item in enumerate(entry.items)
    )
    return lines


def _body(content: CvContent, language: CvLanguage) -> str:
    labels = LABELS[language]
    out: list[str] = []
    if content.experience:
        out.append(f"\\section{_arg(labels.experience)}")
        for entry in content.experience:
            out.extend(_entry(entry, language, bold_org=True))
            out.append("\\vspace{0.3cm}")
    if content.education:
        out.append(f"\\section{_arg(labels.education)}")
        for entry in content.education:
            out.extend(_entry(entry, language, bold_org=False))
    if content.skills:
        out.append(f"\\section{_arg(labels.skills)}")
        skills = (
            f"\\textbf{_arg(skill)}" if i in content.emphasized_skills else escape(skill)
            for i, skill in enumerate(content.skills)
        )
        out.append(f"\\cvitem{{}}{{{', '.join(skills)}}}")
    if content.certifications:
        out.append(f"\\section{_arg(labels.certifications)}")
        for cert in content.certifications:
            out.append(
                f"\\cventry{{{_period(cert.date, language)}}}{_arg(cert.name)}"
                f"{_arg(cert.authority)}{{}}{{}}{{}}"
            )
    if content.languages:
        out.append(f"\\section{_arg(labels.languages)}")
        out.extend(
            f"\\cvlanguage{_arg(lang.name)}{_arg(lang.level)}{{}}" for lang in content.languages
        )
    return "\n".join(out)


def _consent(text: str | None, language: CvLanguage) -> str:
    if not text:
        return ""
    paragraphs = [escape(p) for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    lines = ["\\vspace{0.5cm}", f"\\textbf{_arg(LABELS[language].consent)}", ""]
    lines.extend(f"\\textit{{{p}}}\n" for p in paragraphs)
    return "\n".join(lines)


def render(
    template: CvTemplate,
    content: CvContent,
    language: CvLanguage,
    *,
    consent_text: str | None = None,
    photo_file: str | None = None,
) -> str:
    """LaTeX source for `content`.

    `consent_text` is the user's own clause, never a default. `photo_file` is the name of a photo
    the caller placed next to the .tex file.
    """
    return fill_slots(
        template,
        {
            "babel": babel(language),
            "header": _header(content, photo_file),
            "body": _body(content, language),
            "consent": _consent(consent_text, language),
        },
    )


def babel(language: CvLanguage) -> str:
    return f"\\usepackage[{LABELS[language].babel}]{{babel}}"


def fill_slots(template: CvTemplate, slots: dict[str, str]) -> str:
    """The template's source with each slot line replaced by generated LaTeX."""
    return SLOT_LINE.sub(lambda m: slots[m.group(1)], template.source)
