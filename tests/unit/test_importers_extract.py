import io
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from jaa.core.errors import ImportSourceError
from jaa.importers import docx as docx_module
from jaa.importers.base import IssueSeverity, read_source
from jaa.importers.docx import extract_docx, is_docx
from jaa.importers.pdf import extract_pdf, is_pdf
from tests.fixtures.cv_builders import (
    SAMPLE_NAME,
    Para,
    build_docx,
    build_pdf,
    sample_docx,
    sample_lines,
    sample_pdf,
)

# --- PDF -----------------------------------------------------------------------------------


def test_pdf_lines_are_located_by_page() -> None:
    extracted = extract_pdf(sample_pdf())
    assert extracted.stats == {"pages": 2}
    assert extracted.issues == ()
    assert extracted.lines[0].text == SAMPLE_NAME
    assert extracted.lines[0].location == "page 1"
    education = next(line for line in extracted.lines if line.text == "Education")
    assert education.location == "page 2"


def test_pdf_page_without_text_is_reported() -> None:
    extracted = extract_pdf(build_pdf([["Skills", "Python"], []]))
    assert [line.text for line in extracted.lines] == ["Skills", "Python"]
    (issue,) = extracted.issues
    assert issue.code == "page_without_text"
    assert issue.severity is IssueSeverity.ERROR
    assert issue.location == "page 2"


def test_pdf_page_footers_are_dropped() -> None:
    extracted = extract_pdf(build_pdf([["Skills", "Python", "Page 1 of 2"]]))
    assert [line.text for line in extracted.lines] == ["Skills", "Python"]


def test_damaged_pdf_raises_import_source_error() -> None:
    with pytest.raises(ImportSourceError, match="could not be read"):
        extract_pdf(b"%PDF-1.4\nnot really a pdf")


def _encrypted(user_password: str) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(build_pdf([["Skills", "Python"]]))))
    writer.encrypt(
        user_password=user_password,
        owner_password="synthetic-owner",  # noqa: S106 - test fixture
        algorithm="RC4-128",
    )
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_pdf_encrypted_without_user_password_is_read() -> None:
    extracted = extract_pdf(_encrypted(""))
    assert [line.text for line in extracted.lines] == ["Skills", "Python"]


def test_password_protected_pdf_is_reported() -> None:
    with pytest.raises(ImportSourceError, match="password-protected"):
        extract_pdf(_encrypted("synthetic-secret"))


def test_pdf_detection() -> None:
    assert is_pdf(sample_pdf())
    assert not is_pdf(sample_docx())


# --- DOCX ----------------------------------------------------------------------------------


def test_docx_marks_headings_and_bullets() -> None:
    extracted = extract_docx(sample_docx())
    assert [line.text for line in extracted.lines] == sample_lines()
    by_text = {line.text: line for line in extracted.lines}
    assert by_text["Experience"].is_heading
    assert by_text[SAMPLE_NAME].is_heading  # Title style
    assert by_text["Mentored four engineers."].is_bullet
    assert not by_text["Jan 2021 – Present"].is_bullet
    assert by_text["Experience"].location == "paragraph 5"


def test_docx_reads_page_headers_tables_and_line_breaks() -> None:
    data = build_docx(
        [Para("Skills", "Heading1"), Para("Python\nGo")],
        header=[Para(SAMPLE_NAME), Para("avery@example.test")],
        table=[[Para("Widget Engineer"), Para("2019 – 2021")]],
    )
    extracted = extract_docx(data)
    assert [(line.text, line.location) for line in extracted.lines] == [
        (SAMPLE_NAME, "header"),
        ("avery@example.test", "header"),
        ("Skills", "paragraph 1"),
        ("Python", "paragraph 2"),
        ("Go", "paragraph 2"),
        ("Widget Engineer", "paragraph 3"),
        ("2019 – 2021", "paragraph 4"),
    ]


def test_docx_text_boxes_are_read_once() -> None:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    box = "<w:txbxContent><w:p><w:r><w:t>Boxed</w:t></w:r></w:p></w:txbxContent>"
    xml = (
        f'<w:document xmlns:w="{w}" xmlns:mc="{mc}"><w:body><w:p><w:r><w:t>Outer</w:t></w:r>'
        f"<w:r><mc:AlternateContent><mc:Choice>{box}</mc:Choice>"
        f"<mc:Fallback>{box}</mc:Fallback></mc:AlternateContent></w:r></w:p></w:body></w:document>"
    )
    extracted = extract_docx(build_docx([], document_xml=xml))
    assert [line.text for line in extracted.lines] == ["Outer", "Boxed"]


def test_docx_with_dtd_is_rejected() -> None:
    xml = (
        '<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY lol "lol">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&lol;</w:t></w:r></w:p></w:body></w:document>"
    )
    with pytest.raises(ImportSourceError, match="DTD"):
        extract_docx(build_docx([], document_xml=xml))


def test_docx_part_size_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docx_module, "MAX_PART_BYTES", 100)
    with pytest.raises(ImportSourceError, match="too large"):
        extract_docx(build_docx([Para("x" * 500)]))


def test_damaged_docx_raises_import_source_error() -> None:
    with pytest.raises(ImportSourceError, match="not valid XML"):
        extract_docx(build_docx([], document_xml="<w:document"))


def test_zip_without_document_is_not_docx() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("hello.txt", "hi")
    assert not is_docx(out.getvalue())
    assert is_docx(sample_docx())


# --- reading sources -----------------------------------------------------------------------


def test_read_source_resolves_and_rejects_bad_paths(tmp_path: Path) -> None:
    good = tmp_path / "cv.pdf"
    good.write_bytes(b"%PDF-")
    resolved, data = read_source(tmp_path / "." / "cv.pdf")
    assert resolved == good.resolve()
    assert data == b"%PDF-"

    with pytest.raises(ImportSourceError, match="not found"):
        read_source(tmp_path / "missing.pdf")
    with pytest.raises(ImportSourceError, match="not a regular file"):
        read_source(tmp_path)
    (tmp_path / "empty.pdf").touch()
    with pytest.raises(ImportSourceError, match="empty"):
        read_source(tmp_path / "empty.pdf")


def test_read_source_rejects_huge_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jaa.importers import base

    monkeypatch.setattr(base, "MAX_SOURCE_BYTES", 10)
    big = tmp_path / "cv.pdf"
    big.write_bytes(b"x" * 11)
    with pytest.raises(ImportSourceError, match="larger than"):
        read_source(big)
