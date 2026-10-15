"""Template candidates (ADR 0009), uploaded templates and the default template (ADR 0029).

Template candidates are LaTeX CVs the user chose to keep as a starting point; uploaded templates
are checked against the template contract and used to build documents. Logging records ids and
counts only; the stored files can hold the CV's own text.
"""

import base64
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import replace
from typing import Final

from jaa.core.clock import utcnow
from jaa.core.errors import TemplateError
from jaa.core.models import (
    JsonObject,
    TemplateCandidate,
    TemplateDefault,
    TemplateKind,
    UserTemplate,
)
from jaa.documents.contract import TemplateFiles
from jaa.documents.latex import (
    DEFAULT_LETTER_TEMPLATE,
    DEFAULT_TEMPLATE,
    CvTemplate,
    get_template,
)
from jaa.storage.repository import template_candidates
from jaa.storage.repository import template_defaults as repo_template_defaults
from jaa.storage.repository import user_templates as repo_user_templates

log = logging.getLogger(__name__)


def register_template_candidate(
    conn: sqlite3.Connection, candidate: TemplateCandidate, *, import_run_id: int | None
) -> tuple[TemplateCandidate, bool]:
    """Store a candidate unless identical files are already kept. Returns (candidate, created)."""
    existing = template_candidates.list(conn, content_sha256=candidate.content_sha256)
    if existing:
        return existing[0], False
    with conn:
        stored = template_candidates.insert(
            conn, replace(candidate, id=None, import_run_id=import_run_id)
        )
    log.info("registered template candidate id=%s import_run=%s", stored.id, import_run_id)
    return stored, True


def list_template_candidates(conn: sqlite3.Connection) -> list[TemplateCandidate]:
    return template_candidates.list(conn)


# Uploaded templates and the default choice (#40, ADR 0029) ---------------------------------

USER_PREFIX: Final = "user-"
_SLUG: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
SHIPPED_DEFAULTS: Final = {
    TemplateKind.CV: DEFAULT_TEMPLATE,
    TemplateKind.COVER_LETTER: DEFAULT_LETTER_TEMPLATE,
}


def user_template_id(name: str) -> str:
    """`user-<name>`: uploaded ids carry a prefix so they never shadow a shipped template."""
    slug = name.removeprefix(USER_PREFIX)
    if not _SLUG.fullmatch(slug):
        raise TemplateError(
            f"a template id uses lowercase letters, digits and hyphens (at most 40), not {name!r}"
        )
    return USER_PREFIX + slug


def _encode_files(files: TemplateFiles) -> JsonObject:
    encoded: JsonObject = {path: {"text": text} for path, text in files.texts.items()}
    for path, data in files.assets.items():
        encoded[path] = {"base64": base64.b64encode(data).decode("ascii")}
    return dict(sorted(encoded.items()))


def _content_sha256(main_file: str, files: JsonObject) -> str:
    canonical = json.dumps({"main_file": main_file, "files": files}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def stored_files(template: UserTemplate) -> TemplateFiles:
    """The files of a stored template, decoded."""
    texts: dict[str, str] = {}
    assets: dict[str, bytes] = {}
    for path, entry in template.files.items():
        if not isinstance(entry, dict):
            raise TemplateError(f"{template.version_label}: stored file {path} is malformed")
        if isinstance(text := entry.get("text"), str):
            texts[path] = text
        elif isinstance(data := entry.get("base64"), str):
            assets[path] = base64.b64decode(data)
        else:
            raise TemplateError(f"{template.version_label}: stored file {path} is malformed")
    return TemplateFiles(
        main_file=template.main_file, texts=texts, assets=assets, origin=template.origin
    )


def user_templates(conn: sqlite3.Connection, template_id: str | None = None) -> list[UserTemplate]:
    """Stored versions, oldest first; of one template if `template_id` is given."""
    if template_id is None:
        return repo_user_templates.list(conn)
    return repo_user_templates.list(conn, template_id=template_id)


def latest_user_templates(
    conn: sqlite3.Connection, kind: TemplateKind | None = None
) -> list[UserTemplate]:
    """The newest version of each uploaded template, by id."""
    latest: dict[str, UserTemplate] = {}
    for template in user_templates(conn):
        if kind is None or template.kind is kind:
            latest[template.template_id] = template
    return sorted(latest.values(), key=lambda t: t.template_id)


def add_user_template(
    conn: sqlite3.Connection,
    files: TemplateFiles,
    *,
    name: str,
    kind: TemplateKind = TemplateKind.CV,
    title: str | None = None,
    description: str = "",
    template_candidate_id: int | None = None,
) -> tuple[UserTemplate, bool]:
    """Check and store an uploaded template. Returns (template, created).

    Adding changed files under an existing id stores the next version; identical files return
    the stored version unchanged. Raises `TemplateError` listing the contract's problems.
    """
    template_id = user_template_id(name)
    check = files.check(kind)
    if not check.ok:
        raise TemplateError(
            f"{template_id} does not meet the template contract: " + "; ".join(check.problems)
        )
    encoded = _encode_files(files)
    sha = _content_sha256(files.main_file, encoded)
    versions = user_templates(conn, template_id)
    if versions and versions[-1].kind is not kind:
        raise TemplateError(f"{template_id} is a {versions[-1].kind.value} template")
    if versions and versions[-1].content_sha256 == sha:
        return versions[-1], False
    template = UserTemplate(
        template_id=template_id,
        version=versions[-1].version + 1 if versions else 1,
        kind=kind,
        name=title or (versions[-1].name if versions else template_id),
        description=description or (versions[-1].description if versions else ""),
        family=check.family,
        main_file=files.main_file,
        files=encoded,
        content_sha256=sha,
        origin=files.origin,
        template_candidate_id=template_candidate_id,
        created_at=utcnow(),
    )
    with conn:
        stored = repo_user_templates.insert(conn, template)
    log.info(
        "added template id=%s version=%s files=%s",
        stored.id,
        stored.version,
        len(encoded),
    )
    return stored, True


def _as_cv_template(template: UserTemplate) -> CvTemplate:
    files = stored_files(template)
    check = files.check(template.kind)
    if not check.ok:
        # The contract may have grown stricter since the template was added.
        raise TemplateError(
            f"{template.version_label} no longer meets the template contract: "
            + "; ".join(check.problems)
        )
    return CvTemplate(
        id=template.template_id,
        version=template.version,
        name=template.name,
        family=template.family,
        description=template.description,
        license="Added by you",
        kind=template.kind,
        source=files.texts[files.main_file],
        sha256=template.content_sha256,
        assets=dict(files.assets)
        | {p: t.encode() for p, t in files.texts.items() if p != files.main_file},
        uploaded=True,
    )


def find_user_template(conn: sqlite3.Connection, ref: str) -> UserTemplate:
    """`user-x` (newest version) or `user-x@2`. Raises `TemplateError` if unknown."""
    template_id, _, version = ref.partition("@")
    versions = user_templates(conn, template_id)
    if version:
        versions = [v for v in versions if str(v.version) == version]
    if not versions:
        known = ", ".join(t.version_label for t in latest_user_templates(conn)) or "none"
        raise TemplateError(f"no uploaded template {ref!r}; uploaded: {known}")
    return versions[-1]


def default_template_id(conn: sqlite3.Connection, kind: TemplateKind) -> str:
    """The template used when none is named: the user's choice, else the shipped default."""
    rows = conn.execute(
        "SELECT template_id FROM template_defaults WHERE kind = ? ORDER BY id DESC LIMIT 1",
        (kind.value,),
    ).fetchone()
    return str(rows["template_id"]) if rows else SHIPPED_DEFAULTS[kind]


def resolve_template(
    conn: sqlite3.Connection, ref: str | None, kind: TemplateKind = TemplateKind.CV
) -> CvTemplate:
    """The template `ref` names (shipped or uploaded, optionally `@version`), else the default."""
    ref = ref or default_template_id(conn, kind)
    if not ref.startswith(USER_PREFIX):
        return get_template(ref, kind)
    template = find_user_template(conn, ref)
    if template.kind is not kind:
        raise TemplateError(f"{template.template_id} is not a {kind.value} template")
    return _as_cv_template(template)


def set_default_template(
    conn: sqlite3.Connection, ref: str, kind: TemplateKind = TemplateKind.CV
) -> TemplateDefault:
    """Record the user's default template for `kind`; it must resolve now."""
    resolve_template(conn, ref, kind)
    with conn:
        row = repo_template_defaults.insert(
            conn, TemplateDefault(kind=kind, template_id=ref, set_at=utcnow())
        )
    log.info("set default template kind=%s", kind.value)
    return row


def delete_user_template(conn: sqlite3.Connection, template_id: str) -> int:
    """Delete every version of an uploaded template. Returns the number of versions deleted.

    If it was a default, the default goes back to the shipped template, recorded as a new row.
    """
    versions = user_templates(conn, template_id)
    if not versions:
        raise TemplateError(f"no uploaded template {template_id!r}")
    with conn:
        conn.execute("DELETE FROM user_templates WHERE template_id = ?", (template_id,))
        for kind in TemplateKind:
            current = default_template_id(conn, kind)
            if current.partition("@")[0] == template_id:
                repo_template_defaults.insert(
                    conn,
                    TemplateDefault(kind=kind, template_id=SHIPPED_DEFAULTS[kind], set_at=utcnow()),
                )
    log.info("deleted template versions=%s", len(versions))
    return len(versions)
