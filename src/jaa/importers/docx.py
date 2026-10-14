"""Extract text lines from a DOCX CV with the standard library.

A DOCX is a ZIP of XML parts. Only the parts that hold text are read, straight from the archive
into memory (nothing is extracted to disk), each capped in size so a ZIP bomb cannot exhaust
memory. Parts with a DTD are rejected: WordprocessingML never needs one, and refusing them
rules out entity-expansion attacks regardless of the XML parser's own protections.

Paragraph styles named "Title" or "heading N" mark headings, and numbered paragraphs mark
bullets. Text in page headers (where many CVs put contact details), tables and text boxes is
included. DOCX has no fixed pages, so lines are located by paragraph number.
"""

import io
import logging
import re
import zipfile
from collections.abc import Iterator
from typing import Final
from xml.etree import ElementTree as ET

from jaa.core.errors import ImportSourceError
from jaa.importers.base import ExtractedText, SourceLine

log = logging.getLogger(__name__)

MAX_PART_BYTES: Final = 20 * 1024 * 1024
MAX_HEADER_PARTS: Final = 6

DOCUMENT_PART: Final = "word/document.xml"
STYLES_PART: Final = "word/styles.xml"
_HEADER_PART: Final = re.compile(r"^word/header\d*\.xml$")

_W: Final = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK: Final = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_HEADING_STYLE: Final = re.compile(r"(?i)^(title|heading\s*\d)$")


def is_docx(data: bytes) -> bool:
    if not data.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return DOCUMENT_PART in zf.namelist()
    except zipfile.BadZipFile:
        return False


def _read_part(zf: zipfile.ZipFile, name: str) -> ET.Element:
    with zf.open(name) as member:
        # Read one byte past the cap instead of trusting the size in the ZIP header.
        raw = member.read(MAX_PART_BYTES + 1)
    if len(raw) > MAX_PART_BYTES:
        raise ImportSourceError(f"the DOCX part {name} is too large")
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise ImportSourceError("the DOCX contains a DTD, which Word documents never need")
    try:
        return ET.fromstring(raw)  # noqa: S314 - DTDs are rejected above
    except ET.ParseError as exc:
        raise ImportSourceError(f"the DOCX part {name} is not valid XML") from exc


def _heading_style_ids(styles: ET.Element | None) -> frozenset[str]:
    if styles is None:
        # Without styles.xml, Word's built-in ids are the best guess.
        return frozenset({"Title", *(f"Heading{n}" for n in range(1, 10))})
    ids: set[str] = set()
    for style in styles.iter(f"{_W}style"):
        name = style.find(f"{_W}name")
        style_id = style.get(f"{_W}styleId")
        if style_id and name is not None and _HEADING_STYLE.match(name.get(f"{_W}val", "")):
            ids.add(style_id)
    return frozenset(ids)


def _paragraph_text(paragraph: ET.Element) -> str:
    """Text of the paragraph's own runs, excluding nested text-box paragraphs."""
    parts: list[str] = []

    def walk(element: ET.Element) -> None:
        for child in element:
            if child.tag in (f"{_W}txbxContent", _MC_FALLBACK):
                continue
            if child.tag == f"{_W}t":
                parts.append(child.text or "")
            elif child.tag == f"{_W}tab":
                parts.append("\t")
            elif child.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
            walk(child)

    walk(paragraph)
    return "".join(parts)


def _paragraphs(element: ET.Element) -> Iterator[ET.Element]:
    """Paragraphs in document order, including those in tables and text boxes."""
    for child in element:
        if child.tag == _MC_FALLBACK:
            # The fallback repeats the text box content of the preceding mc:Choice.
            continue
        if child.tag == f"{_W}p":
            yield child
        yield from _paragraphs(child)


def _lines(root: ET.Element, heading_ids: frozenset[str], location: str | None) -> list[SourceLine]:
    lines: list[SourceLine] = []
    for number, paragraph in enumerate(_paragraphs(root), start=1):
        props = paragraph.find(f"{_W}pPr")
        style = props.find(f"{_W}pStyle") if props is not None else None
        is_heading = style is not None and style.get(f"{_W}val", "") in heading_ids
        is_bullet = props is not None and props.find(f"{_W}numPr") is not None
        where = location or f"paragraph {number}"
        for text in _paragraph_text(paragraph).split("\n"):
            text = " ".join(text.replace("\t", " | ").split()).strip(" |")
            if text:
                lines.append(
                    SourceLine(
                        text=text, location=where, is_heading=is_heading, is_bullet=is_bullet
                    )
                )
    return lines


def extract_docx(data: bytes) -> ExtractedText:
    """Return header lines (located as `header`) followed by body lines (`paragraph N`)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if DOCUMENT_PART not in names:
                raise ImportSourceError("the file is a ZIP archive but not a Word document")
            styles = _read_part(zf, STYLES_PART) if STYLES_PART in names else None
            headers = sorted(n for n in names if _HEADER_PART.match(n))[:MAX_HEADER_PARTS]
            header_roots = [_read_part(zf, name) for name in headers]
            document = _read_part(zf, DOCUMENT_PART)
    except zipfile.BadZipFile as exc:
        raise ImportSourceError("the DOCX file is damaged (not a valid ZIP archive)") from exc

    heading_ids = _heading_style_ids(styles)
    header_lines: list[SourceLine] = []
    for root in header_roots:
        for line in _lines(root, heading_ids, "header"):
            # Word repeats the same header part across sections.
            if line.text not in {seen.text for seen in header_lines}:
                header_lines.append(line)
    body = document.find(f"{_W}body")
    body_lines = _lines(body, heading_ids, None) if body is not None else []
    paragraphs = sum(1 for _ in _paragraphs(body)) if body is not None else 0
    log.info(
        "extracted docx paragraphs=%d lines=%d header_lines=%d",
        paragraphs,
        len(body_lines),
        len(header_lines),
    )
    return ExtractedText(lines=(*header_lines, *body_lines), stats={"paragraphs": paragraphs})
