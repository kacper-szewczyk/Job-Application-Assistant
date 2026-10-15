"""Reading templates the user adds: .tex files, folders, ZIPs and candidates (ADR 0029)."""

import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jaa.core.errors import ImportSourceError, UnsafeArchiveError
from jaa.core.models import TemplateCandidate, TemplateOrigin
from jaa.importers.template import candidate_template, read_template

MAIN = "\\documentclass{moderncv}\n\\begin{document}\n\\end{document}\n"


def test_single_tex_file(tmp_path: Path) -> None:
    path = tmp_path / "my-cv.tex"
    path.write_text(MAIN, encoding="utf-8")
    files = read_template(path)
    assert files.origin is TemplateOrigin.FILE
    assert files.main_file == "my-cv.tex"
    assert dict(files.texts) == {"my-cv.tex": MAIN}
    assert files.assets == {}


def test_folder_keeps_files_relative_to_the_main_file(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "cv" / "img").mkdir(parents=True)
    (root / "cv" / "main.tex").write_text(MAIN)
    (root / "cv" / "style.sty").write_text("\\ProvidesPackage{style}")
    (root / "cv" / "img" / "logo.png").write_bytes(b"\x89PNG")
    (root / "cv" / "notes.md").write_text("notes")
    (root / "cv" / ".git").mkdir()
    (root / "cv" / ".git" / "config").write_text("secret")
    (root / "shared.sty").write_text("outside")
    (root / "cv" / "link.sty").symlink_to(root / "shared.sty")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "secret.sty").write_text("outside the folder")
    (root / "cv" / "linked").symlink_to(tmp_path / "elsewhere")

    files = read_template(root)
    assert files.origin is TemplateOrigin.FOLDER
    assert files.main_file == "main.tex"
    assert set(files.texts) == {"main.tex", "style.sty"}
    assert dict(files.assets) == {"img/logo.png": b"\x89PNG"}
    assert set(files.skipped) == {"cv/notes.md", "shared.sty"}


def test_several_main_files_need_a_choice(tmp_path: Path) -> None:
    (tmp_path / "a.tex").write_text(MAIN)
    (tmp_path / "b.tex").write_text(MAIN)
    with pytest.raises(ImportSourceError, match=r"several main .tex files"):
        read_template(tmp_path)
    assert read_template(tmp_path, main="b.tex").main_file == "b.tex"
    with pytest.raises(ImportSourceError, match=r"c\.tex is not among"):
        read_template(tmp_path, main="c.tex")


def test_no_main_file(tmp_path: Path) -> None:
    (tmp_path / "part.tex").write_text("\\section{x}")
    with pytest.raises(ImportSourceError, match=r"no \.tex file with"):
        read_template(tmp_path)


def test_zip_archive(tmp_path: Path) -> None:
    archive = tmp_path / "template.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("t/main.tex", MAIN)
        zf.writestr("t/fonts/Sans.otf", b"OTTO")
        zf.writestr("__MACOSX/t/._main.tex", b"junk")
    files = read_template(archive)
    assert files.origin is TemplateOrigin.ZIP
    assert files.main_file == "main.tex"
    assert dict(files.assets) == {"fonts/Sans.otf": b"OTTO"}
    assert files.skipped == ()


def test_unsafe_zip_is_refused(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("main.tex", MAIN)
        zf.writestr("../../escape.sty", "x")
    with pytest.raises(UnsafeArchiveError):
        read_template(archive)


def test_text_must_be_utf8(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_bytes(MAIN.encode() + "Łódź".encode("cp1250"))
    with pytest.raises(ImportSourceError, match="not UTF-8"):
        read_template(tmp_path)


def test_other_files_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "cv.docx"
    path.write_bytes(b"PK")
    with pytest.raises(ImportSourceError, match=r"as a \.tex file, a folder or a \.zip"):
        read_template(path)
    with pytest.raises(ImportSourceError, match="not found"):
        read_template(tmp_path / "missing.tex")


def test_candidate() -> None:
    candidate = TemplateCandidate(
        id=3,
        main_file="cv.tex",
        files={"cv.tex": MAIN, "parts/a.tex": "\\section{A}"},
        content_sha256="0" * 64,
        registered_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    files = candidate_template(candidate)
    assert files.origin is TemplateOrigin.CANDIDATE
    assert set(files.texts) == {"cv.tex", "parts/a.tex"}
