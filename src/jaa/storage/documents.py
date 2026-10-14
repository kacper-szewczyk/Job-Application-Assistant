"""Generated documents and their manifests (`DocumentArtifact`, SPEC §8). Logs ids only."""

import logging
import sqlite3

from jaa.core.clock import utcnow
from jaa.core.models import ArtifactKind, DocumentArtifact, JsonObject
from jaa.storage.repository import document_artifacts

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
