"""Domain types (SPEC §8). Enum values must match the CHECK constraints in the migrations.

Fields holding CV contents, answers or free-text notes use `repr=False` so that accidentally
logging or printing a model never reveals them.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]
type JsonArray = list[JsonValue]


class FactState(StrEnum):
    UNREVIEWED = "unreviewed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class FactOrigin(StrEnum):
    IMPORT = "import"
    USER = "user"
    CORRECTION = "correction"


class ImportSourceType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    LATEX = "latex"
    LINKEDIN_ARCHIVE = "linkedin_archive"
    TEXT = "text"


class ImportStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class RetentionChoice(StrEnum):
    KEEP = "keep"
    DELETE_AFTER_IMPORT = "delete_after_import"


class PreferenceStatus(StrEnum):
    ANSWERED = "answered"
    SKIPPED = "skipped"


class ReusePolicy(StrEnum):
    ALWAYS_ASK = "always_ask"
    REUSE_WITH_REVIEW = "reuse_with_review"
    REUSE = "reuse"


class JobStatus(StrEnum):
    DISCOVERED = "discovered"
    SAVED = "saved"
    PREPARING = "preparing"
    READY_FOR_REVIEW = "ready_for_review"
    SUBMITTED = "submitted"
    INTERVIEWING = "interviewing"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    CLOSED = "closed"


class StatusActor(StrEnum):
    USER = "user"
    SYSTEM = "system"
    INTEGRATION = "integration"


class SubmissionConfirmation(StrEnum):
    USER = "user"
    INTEGRATION_RECEIPT = "integration_receipt"


class SourceStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"


class FetchRunOutcome(StrEnum):
    """How one connector fetch ended (ADR 0013)."""

    OK = "ok"
    # The source refused access; the source was paused.
    DENIED = "denied"
    # Network errors, 5xx or rate limiting outlasted the retries.
    UNAVAILABLE = "unavailable"
    # The source answered with data the connector could not use, or broke policy limits.
    FAILED = "failed"


class ArtifactKind(StrEnum):
    CV_PDF = "cv_pdf"
    CV_SOURCE = "cv_source"
    COVER_LETTER_PDF = "cover_letter_pdf"
    COVER_LETTER_SOURCE = "cover_letter_source"


@dataclass(frozen=True, slots=True, kw_only=True)
class Profile:
    id: int | None = None
    active_version_id: int | None = None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportRun:
    id: int | None = None
    source_type: ImportSourceType
    source_path: str | None = None
    source_sha256: str | None = None
    imported_at: datetime
    status: ImportStatus
    summary: JsonObject = field(default_factory=dict[str, JsonValue])
    errors: JsonArray = field(default_factory=list[JsonValue])
    retention: RetentionChoice
    source_deleted_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileFact:
    id: int | None = None
    field_key: str
    value_raw: str = field(repr=False)
    value_normalized: JsonValue = field(repr=False)
    state: FactState = FactState.UNREVIEWED
    origin: FactOrigin
    confidence: float | None = None
    is_sensitive: bool = False
    import_run_id: int | None = None
    source_location: str | None = None
    source_excerpt: str | None = field(default=None, repr=False)
    corrected_from_fact_id: int | None = None
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None = None
    rejected_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FactCandidate:
    """A fact proposed by an importer. It has no state: storing it always yields `unreviewed`."""

    field_key: str
    value_raw: str = field(repr=False)
    value_normalized: JsonValue = field(repr=False)
    confidence: float | None = None
    # None means "use the default for the field key" (jaa.core.sensitivity).
    is_sensitive: bool | None = None
    source_location: str | None = None
    source_excerpt: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileFactEvent:
    id: int | None = None
    fact_id: int
    from_state: FactState | None
    to_state: FactState
    occurred_at: datetime
    profile_version_id: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfileVersion:
    id: int | None = None
    created_at: datetime
    note: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Preference:
    id: int | None = None
    key: str
    value: JsonValue = field(repr=False)
    is_sensitive: bool = False
    updated_at: datetime
    status: PreferenceStatus = PreferenceStatus.ANSWERED


@dataclass(frozen=True, slots=True, kw_only=True)
class PreferenceEvent:
    id: int | None = None
    preference_key: str
    status: PreferenceStatus
    value: JsonValue = field(repr=False)
    is_sensitive: bool = False
    occurred_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class Answer:
    id: int | None = None
    question_key: str
    question_text: str
    value: JsonValue = field(repr=False)
    reuse_policy: ReusePolicy = ReusePolicy.ALWAYS_ASK
    is_sensitive: bool = False
    created_at: datetime
    confirmed_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Job:
    id: int | None = None
    source: str
    source_job_id: str
    canonical_url: str
    employer: str
    title: str
    location: str | None = None
    description: str | None = field(default=None, repr=False)
    posted_at: datetime | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    requirements: JsonObject = field(default_factory=dict[str, JsonValue])
    status: JobStatus = JobStatus.DISCOVERED
    status_changed_at: datetime
    # Connector bookkeeping (ADR 0013): the target the listing was last seen in, a hash of the
    # listing's content to detect changes, the source's own update time, and when a complete
    # fetch of its target stopped listing it (None while it is listed).
    source_target: str | None = None
    content_sha256: str | None = None
    source_updated_at: datetime | None = None
    removed_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class JobStatusEvent:
    id: int | None = None
    job_id: int
    from_status: JobStatus | None
    to_status: JobStatus
    actor: StatusActor
    occurred_at: datetime
    note: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceFetchRun:
    """One fetch of one connector target, with what changed. Append-only (ADR 0013).

    `issues` holds `SourceIssue`s as JSON (codes, severities, messages and source job ids, never
    personal data); `error` is the message of the error that ended an unsuccessful run.
    """

    id: int | None = None
    source: str
    target: str
    started_at: datetime
    finished_at: datetime
    outcome: FetchRunOutcome
    requests: int = 0
    listings: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    reappeared: int = 0
    removed: int = 0
    issues: JsonArray = field(default_factory=list[JsonValue])
    error: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceStatusEvent:
    """A job source paused or resumed, by the user or after the source refused access (ADR 0011).

    The latest event for a source is its stored status; no event means active.
    """

    id: int | None = None
    source: str
    status: SourceStatus
    actor: StatusActor
    reason: str | None = None
    occurred_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class JobMatch:
    id: int | None = None
    job_id: int
    profile_version_id: int
    score: float
    evidence: JsonArray = field(default_factory=list[JsonValue])
    gaps: JsonArray = field(default_factory=list[JsonValue])
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class Application:
    id: int | None = None
    job_id: int
    created_at: datetime
    submitted_at: datetime | None = None
    submission_confirmed_by: SubmissionConfirmation | None = None
    external_reference: str | None = None
    notes: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentArtifact:
    id: int | None = None
    kind: ArtifactKind
    path: str
    job_id: int | None = None
    application_id: int | None = None
    profile_version_id: int | None = None
    template_version: str | None = None
    content_sha256: str
    manifest: JsonObject = field(default_factory=dict[str, JsonValue])
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class TemplateCandidate:
    """An imported LaTeX CV kept as a possible starting template for generated CVs (T08).

    It is a snapshot: the files as they were when imported. The structured profile stays
    canonical (SPEC §6); a template only decides how confirmed facts are laid out.
    """

    id: int | None = None
    import_run_id: int | None = None
    main_file: str
    # {POSIX path relative to the main file's folder: LaTeX source}. Holds the CV's own text.
    files: JsonObject = field(repr=False)
    document_class: str | None = None
    packages: JsonArray = field(default_factory=list[JsonValue])
    content_sha256: str
    registered_at: datetime
