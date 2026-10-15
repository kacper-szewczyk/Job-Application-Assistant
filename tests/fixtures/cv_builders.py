"""Build small synthetic PDF and DOCX CVs for tests, so no binary CV files are committed.

All names, employers and contact details here are invented.
"""

import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from xml.sax.saxutils import escape

SAMPLE_NAME: Final = "Avery Synthetic"
SAMPLE_EMAIL: Final = "avery.synthetic@example.test"
SAMPLE_PHONE: Final = "+44 20 7946 0958"

# One CV used by both formats: (text, docx style or None, is_bullet).
type CvLines = Sequence[tuple[str, str | None, bool]]

SAMPLE_CV: Final[tuple[tuple[str, str | None, bool], ...]] = (
    (SAMPLE_NAME, "Title", False),
    (f"{SAMPLE_EMAIL} | {SAMPLE_PHONE} | github.com/avery-synthetic", None, False),
    ("Summary", "Heading1", False),
    ("Backend engineer building reliable widget platforms.", None, False),
    ("Experience", "Heading1", False),
    ("Senior Widget Engineer at Example Widgets Ltd", None, False),
    ("Jan 2021 – Present", None, False),
    ("Led the migration of widget pipelines to event streaming.", None, True),
    ("Mentored four engineers.", None, True),
    ("Example Gadgets | Software Developer | 03/2017 - 12/2020", None, False),
    ("Built gadget inventory APIs.", None, True),
    ("Education", "Heading1", False),
    ("BSc Computer Science, University of Exampleton", None, False),
    ("2013 – 2016", None, False),
    ("Skills", "Heading1", False),
    ("Languages: Python, Go, C++", None, False),
    ("Tools: Docker, PostgreSQL (replication, tuning)", None, False),
    ("Languages", "Heading1", False),
    ("English (native), German – B2", None, False),
    ("Volunteering", "Heading1", False),
    ("Synthetic coding club mentor", None, False),
)


# The same kinds of content in Polish, with Polish headings, dates and a RODO consent clause.
SAMPLE_CV_PL: Final[tuple[tuple[str, str | None, bool], ...]] = (
    ("Żaneta Przykładowa", "Title", False),
    ("zaneta.przykladowa@example.test | +48 600 100 200 | github.com/zaneta-przyklad", None, False),
    ("Podsumowanie", "Heading1", False),
    ("Inżynierka backendu budująca niezawodne platformy widżetów.", None, False),
    ("Doświadczenie zawodowe", "Heading1", False),
    ("Starsza programistka w Przykładowe Widżety sp. z o.o.", None, False),
    ("styczeń 2021 – obecnie", None, False),
    ("Prowadziłam migrację potoków widżetów na strumienie zdarzeń.", None, True),
    ("Gadżety Łódź S.A. | Inżynier oprogramowania | 03.2017 - 12.2020", None, False),
    ("Zbudowałam API do ewidencji gadżetów.", None, True),
    ("Wykształcenie", "Heading1", False),
    ("Magister informatyki, Politechnika Przykładowa", None, False),
    ("2013 – 2016", None, False),
    ("Umiejętności", "Heading1", False),
    ("Języki programowania: Python, Go, C++", None, False),
    ("Narzędzia: Docker, PostgreSQL (replikacja, strojenie)", None, False),
    ("Języki obce", "Heading1", False),
    ("polski (ojczysty), angielski – C1", None, False),
    ("Wolontariat", "Heading1", False),
    ("Mentorka w syntetycznym klubie programowania", None, False),
    (
        "Wyrażam zgodę na przetwarzanie moich danych osobowych dla potrzeb niezbędnych do "
        "realizacji procesu rekrutacji.",
        None,
        False,
    ),
)


def sample_lines() -> list[str]:
    return [text for text, _, _ in SAMPLE_CV]


# --- PDF -----------------------------------------------------------------------------------


# Polish letters missing from WinAnsi, placed on codes the font's /Differences renames.
_EXTRA_GLYPHS: Final = {
    "ą": "aogonek",
    "ć": "cacute",
    "ę": "eogonek",
    "ł": "lslash",
    "ń": "nacute",
    "ś": "sacute",
    "ź": "zacute",
    "ż": "zdotaccent",
    "Ą": "Aogonek",
    "Ć": "Cacute",
    "Ę": "Eogonek",
    "Ł": "Lslash",
    "Ń": "Nacute",
    "Ś": "Sacute",
    "Ź": "Zacute",
    "Ż": "Zdotaccent",
}
_FIRST_EXTRA: Final = 0x80
_EXTRA_CODES: Final = {char: _FIRST_EXTRA + i for i, char in enumerate(_EXTRA_GLYPHS)}


def _encode(char: str) -> bytes:
    if char in _EXTRA_CODES:
        return bytes([_EXTRA_CODES[char]])
    code = char.encode("cp1252")
    if _FIRST_EXTRA <= code[0] < _FIRST_EXTRA + len(_EXTRA_CODES):
        raise ValueError(f"{char!r} would be read as a Polish letter in the test PDF")
    return code


def _pdf_string(text: str) -> bytes:
    raw = b"".join(_encode(char) for char in text)
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def build_pdf(pages: Sequence[Sequence[str]]) -> bytes:
    """A minimal text PDF: one Helvetica line per string. An empty page has no text layer."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in once the page tree id is known
    differences = " ".join(f"/{name}" for name in _EXTRA_GLYPHS.values())
    font = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding << /BaseEncoding "
        + f"/WinAnsiEncoding /Differences [{_FIRST_EXTRA} {differences}] >> >>".encode()
    )
    tree = add(b"")
    page_ids: list[int] = []
    for lines in pages:
        ops = [b"BT /F1 11 Tf 14 TL 50 800 Td"]
        for line in lines:
            ops.append(_pdf_string(line) + b" Tj T*")
        ops.append(b"ET")
        stream = b"\n".join(ops) if lines else b""
        content = add(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
        page_ids.append(
            add(
                f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>".encode()
            )
        )
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    objects[tree - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {tree} 0 R >>".encode()

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def sample_pdf(cv: CvLines = SAMPLE_CV) -> bytes:
    # PDFs keep bullets as glyphs in the text, unlike DOCX numbering.
    lines = [f"• {text}" if bullet else text for text, _, bullet in cv]
    split = next(i for i, (_, style, _) in enumerate(cv) if style == "Heading1" and i > 4)
    return build_pdf([lines[:split], lines[split:]])


# --- DOCX ----------------------------------------------------------------------------------

_W: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_CONTENT_TYPES: Final = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_RELS: Final = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
_STYLES: Final = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="{_W}">'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
    "</w:styles>"
)


@dataclass(frozen=True)
class Para:
    text: str
    style: str | None = None
    bullet: bool = False


def _paragraph(p: Para) -> str:
    props = ""
    if p.style:
        props += f'<w:pStyle w:val="{p.style}"/>'
    if p.bullet:
        props += '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
    runs = "<w:r><w:br/></w:r>".join(
        f'<w:r><w:t xml:space="preserve">{escape(part)}</w:t></w:r>' for part in p.text.split("\n")
    )
    return f"<w:p><w:pPr>{props}</w:pPr>{runs}</w:p>"


def build_docx(
    paragraphs: Sequence[Para],
    *,
    header: Sequence[Para] = (),
    table: Sequence[Sequence[Para]] = (),
    document_xml: str | None = None,
) -> bytes:
    """A minimal DOCX. `table` rows are appended as a one-cell-per-item table after the body."""
    body = "".join(_paragraph(p) for p in paragraphs)
    if table:
        rows = "".join(
            "<w:tr>" + "".join(f"<w:tc>{_paragraph(c)}</w:tc>" for c in row) + "</w:tr>"
            for row in table
        )
        body += f"<w:tbl>{rows}</w:tbl>"
    xml = document_xml or (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>'
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/document.xml", xml)
        zf.writestr("word/styles.xml", _STYLES)
        if header:
            zf.writestr(
                "word/header1.xml",
                f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:hdr xmlns:w="{_W}">{"".join(_paragraph(p) for p in header)}</w:hdr>',
            )
    return out.getvalue()


def sample_docx(cv: CvLines = SAMPLE_CV) -> bytes:
    return build_docx([Para(text, style, bullet) for text, style, bullet in cv])
