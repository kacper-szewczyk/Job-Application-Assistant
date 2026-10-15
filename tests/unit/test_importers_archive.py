"""Hardening of user-supplied paths and archives (T19), beyond the LinkedIn import tests."""

import io
import os
import zipfile
from pathlib import Path

import pytest

from jaa.core.errors import ImportSourceError, UnsafeArchiveError
from jaa.importers import archive
from jaa.importers.archive import list_members, read_member, safe_member_name
from jaa.importers.base import resolve_source
from tests.fixtures.linkedin_builders import build_zip


def _zip(*infos: tuple[zipfile.ZipInfo | str, bytes]) -> zipfile.ZipFile:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for info, data in infos:
            zf.writestr(info, data)
    return zipfile.ZipFile(io.BytesIO(buffer.getvalue()))


@pytest.mark.parametrize("name", ["", "a\x00b.csv", "..", "a/../../b.csv", "\\\\server\\share"])
def test_unsafe_names_are_refused(name: str) -> None:
    with pytest.raises(UnsafeArchiveError):
        safe_member_name(name)


def test_encrypted_members_are_not_read() -> None:
    zf = _zip(("Skills.csv", b"Name\nsynthetic\n"))
    # zipfile cannot write encrypted members, so mark the parsed entry as an archive would.
    zf.infolist()[0].flag_bits |= 0x1
    (member,) = list_members(zf)
    assert member.encrypted
    with pytest.raises(UnsafeArchiveError, match="encrypted"):
        read_member(zf, member)


def test_archives_with_too_many_members_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive, "MAX_MEMBERS", 3)
    zf = zipfile.ZipFile(io.BytesIO(build_zip({f"f{i}.csv": b"x" for i in range(4)})))
    with pytest.raises(UnsafeArchiveError, match="more than 3 members"):
        list_members(zf)


def test_directories_are_skipped_but_still_validated() -> None:
    zf = _zip((zipfile.ZipInfo("export/"), b""), ("export/Skills.csv", b"Name\n"))
    assert [str(m.name) for m in list_members(zf)] == ["export/Skills.csv"]
    with pytest.raises(UnsafeArchiveError):
        list_members(_zip((zipfile.ZipInfo("../export/"), b"")))


def test_resolve_source_accepts_a_regular_file_through_a_link(tmp_path: Path) -> None:
    real = tmp_path / "cv.pdf"
    real.write_bytes(b"%PDF synthetic")
    link = tmp_path / "link.pdf"
    link.symlink_to(real)
    assert resolve_source(link) == real.resolve()


def test_resolve_source_refuses_what_is_not_a_readable_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pdf"
    empty.touch()
    big = tmp_path / "big.pdf"
    big.write_bytes(b"x" * 11)
    loop = tmp_path / "loop.pdf"
    loop.symlink_to(loop)
    fifo = tmp_path / "pipe.pdf"
    os.mkfifo(fifo)
    cases = [
        (tmp_path / "missing.pdf", "not found"),
        (tmp_path / "dangling.pdf", "not found"),
        (loop, "not found"),
        (tmp_path, "not a regular file"),
        (fifo, "not a regular file"),
        (empty, "empty"),
        (big, "larger than"),
    ]
    (tmp_path / "dangling.pdf").symlink_to(tmp_path / "nowhere.pdf")
    for path, message in cases:
        with pytest.raises(ImportSourceError, match=message):
            resolve_source(path, max_bytes=10)
