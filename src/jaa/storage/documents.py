"""Generated documents and their manifests (`DocumentArtifact`, SPEC §8). Logs ids only.

`build_profile_cv` and `build_tailored_cv` are the CV builds shared by the CLI and the MCP
server: they pick the facts, compile the PDF and record it with its manifest.
"""

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from jaa.core.clock import utcnow
from jaa.core.errors import DocumentError, TailoringError
from jaa.core.models import ArtifactKind, DocumentArtifact, FactState, JsonObject, ProfileFact
from jaa.documents.content import build_content
from jaa.documents.engine import Engine
from jaa.documents.latex import CvTemplate
from jaa.documents.pipeline import BuildOptions, BuildOutput, build_cv
from jaa.storage import tailoring
from jaa.storage.profile import active_version_id, version_fact_ids
from jaa.storage.repository import document_artifacts, profile_facts

log = logging.getLogger(__name__)


def record_cv(
    conn: sqlite3.Connection,
    *,
    pdf_path: str,
    pdf_sha256: str,
    tex_path: str,
    tex_sha256: str,
    template_version: str,
    profile_version_id: int | None,
    manifest: JsonObject,
    job_id: int | None = None,
) -> tuple[DocumentArtifact, DocumentArtifact]:
    """Record a built CV: the PDF and its LaTeX source, sharing one manifest."""
    at = utcnow()
    with conn:
        pdf, tex = (
            document_artifacts.insert(
                conn,
                DocumentArtifact(
                    kind=kind,
                    path=path,
                    job_id=job_id,
                    profile_version_id=profile_version_id,
                    template_version=template_version,
                    content_sha256=sha,
                    manifest=manifest,
                    created_at=at,
                ),
            )
            for kind, path, sha in (
                (ArtifactKind.CV_PDF, pdf_path, pdf_sha256),
                (ArtifactKind.CV_SOURCE, tex_path, tex_sha256),
            )
        )
    log.info("recorded cv artifacts pdf=%s source=%s", pdf.id, tex.id)
    return pdf, tex


def list_cvs(conn: sqlite3.Connection) -> list[DocumentArtifact]:
    """Generated CV PDFs, oldest first."""
    return document_artifacts.list(conn, kind=ArtifactKind.CV_PDF)


@dataclass(frozen=True, slots=True, kw_only=True)
class BuiltCv:
    output: BuildOutput
    pdf: DocumentArtifact
    profile_version_id: int
    facts_used: int
    # Confirmed facts the template does not show (profile CVs only).
    unused: tuple[ProfileFact, ...] = ()
    # Unreviewed facts left out of the CV, recorded in the manifest.
    left_out: tuple[int, ...] = ()
    # Changes applied from the tailoring plan (tailored CVs only).
    changes: int = 0


def _record(
    conn: sqlite3.Connection,
    out: BuildOutput,
    template: CvTemplate,
    version: int,
    job_id: int | None = None,
) -> DocumentArtifact:
    pdf, _ = record_cv(
        conn,
        pdf_path=str(out.pdf),
        pdf_sha256=out.pdf_sha256,
        tex_path=str(out.tex),
        tex_sha256=out.tex_sha256,
        template_version=template.version_label,
        profile_version_id=version,
        manifest=out.manifest,
        job_id=job_id,
    )
    return pdf


def build_profile_cv(
    conn: sqlite3.Connection,
    *,
    template: CvTemplate,
    engine: Engine,
    output_dir: Path,
    options: BuildOptions,
) -> BuiltCv:
    """Build a CV from the active profile version's confirmed facts and record it.

    Unreviewed facts are left out and listed in the manifest; review them first to use them.
    """
    version = active_version_id(conn)
    facts = (
        [profile_facts.get(conn, i) for i in version_fact_ids(conn, version)]
        if version is not None
        else []
    )
    if version is None or not facts:
        raise DocumentError("no confirmed facts yet; import a CV and confirm its facts first")
    left_out = tuple(
        f.id for f in profile_facts.list(conn, state=FactState.UNREVIEWED) if f.id is not None
    )
    result = build_content(facts)
    out = build_cv(
        result.content,
        template=template,
        engine=engine,
        output_dir=output_dir,
        options=options,
        profile_version_id=version,
        excluded_fact_ids=left_out,
    )
    pdf = _record(conn, out, template, version)
    return BuiltCv(
        output=out,
        pdf=pdf,
        profile_version_id=version,
        facts_used=len(result.content.fact_ids),
        unused=result.unused,
        left_out=left_out,
    )


def build_tailored_cv(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    revision: int | None = None,
    template: CvTemplate,
    engine: Engine,
    output_dir: Path,
    options: BuildOptions,
) -> BuiltCv:
    """Build a job's tailored CV (latest revision by default) and link it to the job.

    Refuses while any edit or cover letter paragraph is blocked (ADR 0016). The CV is built
    from the profile version the revision was evaluated against, which may not be the active one.
    """
    evaluated = tailoring.evaluate(conn, job_id, revision)
    row, result = evaluated.row, evaluated.result
    if result.blocked:
        blocked = [
            f"{kind} {c.number}"
            for kind, checks in (
                ("edit", result.edit_checks),
                ("paragraph", result.paragraph_checks),
            )
            for c in checks
            if c.blocked
        ]
        raise TailoringError(
            f"blocked: {', '.join(blocked)}; see `jaa cv tailor show {job_id}`, then edit "
            "or approve them"
        )
    cited = {i for c in result.edit_checks for i in c.fact_ids}
    left_out = tuple(
        f.id
        for f in profile_facts.list(conn, state=FactState.UNREVIEWED)
        if f.id is not None and f.id not in cited
    )
    plan: JsonObject = {
        "tailoring_id": row.id,
        "revision": row.revision,
        "method": row.method,
        "job_match_id": row.job_match_id,
        "changes": [c.to_json() for c in result.changes],
        "approved": [c.number for c in result.edit_checks if c.issues and c.approved],
    }
    out = build_cv(
        result.content,
        template=template,
        engine=engine,
        output_dir=output_dir,
        options=options,
        profile_version_id=row.profile_version_id,
        excluded_fact_ids=left_out,
        job_id=job_id,
        tailoring=plan,
    )
    pdf = _record(conn, out, template, row.profile_version_id, job_id)
    return BuiltCv(
        output=out,
        pdf=pdf,
        profile_version_id=row.profile_version_id,
        facts_used=len(result.content.fact_ids),
        left_out=left_out,
        changes=len(result.changes),
    )
