"""Typed results of the MCP tools (ADR 0019).

They carry ids, states and the text the assistant needs to help the user. Values of facts marked
sensitive are left out (`value` is None); the user sees them with `jaa profile show`.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from jaa.core.models import FactOrigin, FactState, ImportSourceType, ImportStatus, JobStatus


class Confirmation(BaseModel):
    """What the user answers in the confirmation dialog of a mutating tool."""

    confirm: bool = Field(description="Tick to confirm. Leave it unticked or decline to cancel.")


class ImportIssue(BaseModel):
    severity: str
    location: str | None = None
    message: str


class ImportOut(BaseModel):
    import_run_id: int
    source_type: ImportSourceType
    status: ImportStatus
    facts_added: int = Field(description="New facts, all unreviewed until the user reviews them.")
    already_known: int = Field(description="Values already stored, skipped.")
    earlier_import_ids: list[int] = Field(description="Earlier imports of the same file.")
    conflicting_fields: list[str] = Field(
        description="Field keys that now have competing values; see review_facts."
    )
    sections_read: list[str]
    sections_not_imported: list[str]
    issues: list[ImportIssue]
    next_step: str


class FactOut(BaseModel):
    id: int
    field_key: str
    value: str | None = Field(description="The value; None when the fact is sensitive.")
    sensitive: bool
    state: FactState
    origin: FactOrigin
    confidence: float | None
    import_run_id: int | None
    source_location: str | None
    corrects_fact_id: int | None


class ConflictOut(BaseModel):
    field_key: str
    confirmed_fact_id: int | None
    incoming_fact_ids: list[int]


class ReviewFactsOut(BaseModel):
    facts: list[FactOut]
    total: int = Field(description="Facts matching the filters, before the limit.")
    conflicts: list[ConflictOut]
    next_step: str


class ChangedFact(BaseModel):
    id: int
    field_key: str
    state: FactState


class DecideFactsOut(BaseModel):
    confirmed_by_user: bool
    changed: list[ChangedFact]
    profile_version_id: int | None = Field(description="New profile version, if one was made.")
    message: str


class JobSummary(BaseModel):
    id: int
    title: str
    employer: str
    location: str | None
    source: str
    url: str
    status: JobStatus
    archived: bool = Field(description="Rejected, withdrawn or closed: finished, kept on record.")
    first_seen_at: datetime
    no_longer_listed: bool
    possible_duplicates: list[int] = Field(description="Job ids flagged as maybe the same job.")
    match_score: float | None = Field(description="Latest advisory match score, 0 to 1.")


class SearchJobsOut(BaseModel):
    jobs: list[JobSummary]
    total: int = Field(description="Jobs matching, before the limit.")
    jobs_last_fetched: datetime | None


class Requirements(BaseModel):
    listed_skills: list[str]
    required: list[str]
    nice_to_have: list[str]
    responsibilities: list[str]
    questions: list[str]
    experience_level: str | None
    work_arrangement: str | None


class MatchOut(BaseModel):
    score: float | None = Field(description="Advisory; None when there was nothing to compare.")
    profile_version_id: int
    matched_at: datetime
    outdated_because: list[str]
    met: list[str] = Field(description="Requirements backed by confirmed facts.")
    unreviewed_only: list[str] = Field(description="Backed only by unreviewed facts.")
    missing: list[str] = Field(description="No confirmed fact mentions them.")
    preference_mismatches: list[str]


class DuplicateOut(BaseModel):
    flag_id: int
    other_job_id: int
    reason: str
    verdict: str | None


class ApplicationOut(BaseModel):
    job_id: int
    title: str
    employer: str
    status: JobStatus
    archived: bool = Field(description="Rejected, withdrawn or closed: finished, kept on record.")
    status_changed_at: datetime
    started_at: datetime
    submitted_at: datetime | None


class JobDetail(BaseModel):
    job: JobSummary
    description: str | None
    description_truncated: bool
    requirements: Requirements
    match: MatchOut | None
    application: ApplicationOut | None
    duplicates: list[DuplicateOut]
    latest_tailoring_revision: int | None


class CheckIssue(BaseModel):
    code: str
    message: str


class EditOut(BaseModel):
    number: int
    kind: str
    target: str
    before: str | None
    after: str | None
    fact_ids: list[int]
    reason: str | None
    status: str = Field(description="ok, approved (by the user) or blocked.")
    approvable: bool = Field(description="Whether the user may approve it despite its issues.")
    issues: list[CheckIssue]


class ParagraphOut(BaseModel):
    number: int
    text: str
    fact_ids: list[int]
    status: str
    approvable: bool
    issues: list[CheckIssue]


class TailoringOut(BaseModel):
    job_id: int
    revision: int
    origin: str
    profile_version_id: int
    profile_changed_since: bool
    edits: list[EditOut]
    cover_letter: list[ParagraphOut]
    blocked: bool
    plan: dict[str, object] = Field(
        description="The plan as JSON; change it and pass it to draft_tailored_cv as `plan`."
    )
    next_step: str


class CvPdfOut(BaseModel):
    document_id: int
    pdf_path: str
    tex_path: str
    pages: int
    template: str
    engine: str
    profile_version_id: int
    job_id: int | None
    facts_used: int
    changes_applied: int
    unreviewed_left_out: int
    layout_warnings: list[str]
    text_missing_from_pdf: int = Field(
        description="CV lines not found in the PDF's text; applicant tracking may miss them."
    )


class ApplicationsOut(BaseModel):
    applications: list[ApplicationOut]


class StatusOut(BaseModel):
    confirmed_by_user: bool
    job_id: int
    status: JobStatus
    submitted_at: datetime | None
    documents_linked: int
    answers_linked: int
    message: str


class LinkedInSuggestionOut(BaseModel):
    kind: str = Field(description="add, update, or check (a LinkedIn value the user rejected).")
    section: str
    address: str = Field(description="What to change, e.g. headline or experience.acme.title.")
    text: str = Field(
        description="Confirmed wording to use on LinkedIn, copied from the cited facts."
    )
    current: str | None = Field(description="What the LinkedIn import showed, if anything.")
    fact_ids: list[int] = Field(description="Confirmed facts the suggestion is based on.")
    linkedin_fact_ids: list[int]
    job_ids: list[int] = Field(description="Target jobs whose requirements these facts meet.")
    required_by_jobs: bool


class LinkedInGapOut(BaseModel):
    requirement: str
    importance: str
    job_ids: list[int]
    unreviewed_only: bool = Field(description="Only unreviewed facts would back it.")


class LinkedInSuggestionsOut(BaseModel):
    linkedin_import_id: int | None = Field(description="None when there is no LinkedIn import.")
    linkedin_imported_at: datetime | None
    target_job_ids: list[int]
    suggestions: list[LinkedInSuggestionOut]
    gaps: list[LinkedInGapOut] = Field(
        description="Requirements no confirmed fact meets. Never suggest claiming them."
    )
    unreviewed_linkedin_facts: int
    next_step: str
