"""Inspect and read ZIP archives supplied by the user without trusting them.

Nothing is extracted to disk: members are read into memory with a byte cap, so a hostile
archive cannot write outside a directory (zip-slip) or fill the disk. An archive that contains
any unsafe member name is refused as a whole, because a genuine export never has one.
"""

import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from jaa.core.errors import UnsafeArchiveError

MAX_MEMBERS: Final = 10_000
# Per member read into memory. Members are read only when they are recognized.
MAX_MEMBER_BYTES: Final = 10 * 1024 * 1024
# Compressed data expanding more than this is treated as a ZIP bomb.
MAX_COMPRESSION_RATIO: Final = 200
_RATIO_MIN_BYTES: Final = 1024 * 1024
_ENCRYPTED_FLAG: Final = 0x1


@dataclass(frozen=True, slots=True, kw_only=True)
class ArchiveMember:
    """A file in an archive. `name` is the validated, normalized POSIX path."""

    name: PurePosixPath
    size: int
    encrypted: bool
    info: zipfile.ZipInfo


def safe_member_name(raw: str) -> PurePosixPath:
    """Validate a member name. Raises `UnsafeArchiveError` for anything that could escape."""
    if not raw or "\x00" in raw:
        raise UnsafeArchiveError("archive contains a member with an empty or invalid name")
    if "\\" in raw:
        raise UnsafeArchiveError(f"archive member uses backslashes in its path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or (len(raw) >= 2 and raw[1] == ":"):
        raise UnsafeArchiveError(f"archive member has an absolute path: {raw!r}")
    if ".." in path.parts:
        raise UnsafeArchiveError(f"archive member points outside the archive: {raw!r}")
    return path


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def list_members(zf: zipfile.ZipFile) -> list[ArchiveMember]:
    """Validate every member and return the files (directories are left out)."""
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise UnsafeArchiveError(f"archive has more than {MAX_MEMBERS} members")
    members: list[ArchiveMember] = []
    for info in infos:
        name = safe_member_name(info.filename)
        if _is_symlink(info):
            raise UnsafeArchiveError(f"archive contains a symbolic link: {info.filename!r}")
        if info.is_dir():
            continue
        members.append(
            ArchiveMember(
                name=name,
                size=info.file_size,
                encrypted=bool(info.flag_bits & _ENCRYPTED_FLAG),
                info=info,
            )
        )
    return members


def read_member(
    zf: zipfile.ZipFile, member: ArchiveMember, *, limit: int = MAX_MEMBER_BYTES
) -> bytes:
    """Read one member into memory, refusing oversized or suspiciously compressed data.

    The declared size in the archive is not trusted; reading stops after `limit` bytes.
    """
    if member.encrypted:
        raise UnsafeArchiveError(f"{member.name} is encrypted")
    if member.size > limit:
        raise UnsafeArchiveError(f"{member.name} is larger than {limit // (1024 * 1024)} MB")
    with zf.open(member.info) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise UnsafeArchiveError(f"{member.name} is larger than {limit // (1024 * 1024)} MB")
    compressed = max(member.info.compress_size, 1)
    if len(data) >= _RATIO_MIN_BYTES and len(data) / compressed > MAX_COMPRESSION_RATIO:
        raise UnsafeArchiveError(f"{member.name} is compressed suspiciously well (ZIP bomb?)")
    return data
