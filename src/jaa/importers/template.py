"""Reading a template the user adds: a .tex file, a folder, a ZIP, or a template candidate.

Returns `TemplateFiles` for the contract check and storage (ADR 0029); nothing is written.
ZIP members are read in memory through `jaa.importers.archive`, so nothing is extracted. Files
are kept relative to the main file's folder; anything outside it, hidden files and other file
types are skipped and reported. Logging records counts only.
"""

import logging
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Final

from jaa.core.errors import ImportSourceError
from jaa.core.models import TemplateCandidate, TemplateOrigin
from jaa.documents.contract import (
    ASSET_SUFFIXES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    TEXT_SUFFIXES,
    TemplateFiles,
    document_class,
)
from jaa.importers.archive import list_members, read_member

log = logging.getLogger(__name__)

# Reading stops well before a huge folder or archive is walked to the end.
MAX_SCANNED: Final = 10 * MAX_FILES
_SKIPPED_DIRS: Final = frozenset({"__MACOSX"})
_DOCUMENTCLASS: Final = re.compile(rb"\\documentclass")


def _hidden(path: PurePosixPath) -> bool:
    return any(part.startswith(".") or part in _SKIPPED_DIRS for part in path.parts)


def _folder_files(root: Path) -> Iterator[tuple[PurePosixPath, bytes]]:
    total = 0
    for count, path in enumerate(sorted(root.rglob("*")), start=1):
        if count > MAX_SCANNED:
            raise ImportSourceError(f"the folder has more than {MAX_SCANNED} entries")
        relative = PurePosixPath(path.relative_to(root).as_posix())
        # A symlinked folder could lead anywhere, so files must resolve inside the root.
        if (
            _hidden(relative)
            or path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root)
        ):
            continue
        total += path.stat().st_size
        if total > MAX_TOTAL_BYTES:
            raise ImportSourceError(
                f"the folder holds more than {MAX_TOTAL_BYTES // 2**20} MB of files"
            )
        yield relative, path.read_bytes()


def _zip_files(path: Path) -> Iterator[tuple[PurePosixPath, bytes]]:
    try:
        with zipfile.ZipFile(path) as zf:
            members = [m for m in list_members(zf) if not _hidden(m.name)]
            if len(members) > MAX_SCANNED:
                raise ImportSourceError(f"the archive has more than {MAX_SCANNED} files")
            if sum(m.size for m in members) > MAX_TOTAL_BYTES:
                raise ImportSourceError(
                    f"the archive holds more than {MAX_TOTAL_BYTES // 2**20} MB of files"
                )
            for member in members:
                if member.encrypted:
                    raise ImportSourceError(f"{member.name} is encrypted")
                yield member.name, read_member(zf, member, limit=MAX_TOTAL_BYTES)
    except zipfile.BadZipFile as exc:
        raise ImportSourceError(f"not a readable ZIP archive: {exc}") from exc


def _main_file(files: dict[PurePosixPath, bytes], main: str | None) -> PurePosixPath:
    if main is not None:
        chosen = PurePosixPath(main)
        if chosen not in files:
            raise ImportSourceError(f"{main} is not among the template's files")
        return chosen
    mains = [
        p
        for p, data in files.items()
        if p.suffix.lower() == ".tex" and _DOCUMENTCLASS.search(data) is not None
    ]
    if len(mains) == 1:
        return mains[0]
    if not mains:
        raise ImportSourceError("no .tex file with \\documentclass found; name it with --main")
    listed = ", ".join(str(p) for p in sorted(mains))
    raise ImportSourceError(f"several main .tex files ({listed}); choose one with --main")


def _decode(path: PurePosixPath, data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportSourceError(
            f"{path} is not UTF-8 text; save it as UTF-8 and try again"
        ) from exc


def _arrange(
    files: dict[PurePosixPath, bytes], origin: TemplateOrigin, main: str | None
) -> TemplateFiles:
    main_path = _main_file(files, main)
    root = main_path.parent
    texts: dict[str, str] = {}
    assets: dict[str, bytes] = {}
    skipped: list[str] = []
    for path, data in sorted(files.items()):
        if not path.is_relative_to(root):
            skipped.append(str(path))
            continue
        key = path.relative_to(root).as_posix()
        suffix = path.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            texts[key] = _decode(path, data)
        elif suffix in ASSET_SUFFIXES:
            assets[key] = data
        else:
            skipped.append(str(path))
    log.info(
        "read template origin=%s texts=%s assets=%s skipped=%s",
        origin.value,
        len(texts),
        len(assets),
        len(skipped),
    )
    return TemplateFiles(
        main_file=main_path.relative_to(root).as_posix(),
        texts=texts,
        assets=assets,
        origin=origin,
        skipped=tuple(skipped),
    )


def read_template(path: Path, *, main: str | None = None) -> TemplateFiles:
    """Read a template from a `.tex` file, a folder or a `.zip` archive.

    `main` names the main file (relative to the folder or archive) when there are several.
    Raises `ImportSourceError` (or `UnsafeArchiveError`) for anything that cannot be read.
    """
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise ImportSourceError(f"not found: {path}") from exc
    if resolved.is_dir():
        return _arrange(dict(_folder_files(resolved)), TemplateOrigin.FOLDER, main)
    if not resolved.is_file():
        raise ImportSourceError(f"not a file or folder: {resolved}")
    if zipfile.is_zipfile(resolved):
        return _arrange(dict(_zip_files(resolved)), TemplateOrigin.ZIP, main)
    if resolved.suffix.lower() != ".tex":
        raise ImportSourceError("add a template as a .tex file, a folder or a .zip archive")
    if resolved.stat().st_size > MAX_TOTAL_BYTES:
        raise ImportSourceError(f"{resolved.name} is larger than {MAX_TOTAL_BYTES // 2**20} MB")
    name = PurePosixPath(resolved.name)
    return _arrange({name: resolved.read_bytes()}, TemplateOrigin.FILE, None)


def candidate_template(candidate: TemplateCandidate) -> TemplateFiles:
    """A template candidate's stored files (ADR 0009) as a template to check and add."""
    texts = {path: text for path, text in candidate.files.items() if isinstance(text, str)}
    if candidate.main_file not in texts or document_class(texts[candidate.main_file]) is None:
        raise ImportSourceError(f"template candidate #{candidate.id} has no main .tex file")
    return TemplateFiles(
        main_file=candidate.main_file,
        texts=texts,
        assets={},
        origin=TemplateOrigin.CANDIDATE,
    )
