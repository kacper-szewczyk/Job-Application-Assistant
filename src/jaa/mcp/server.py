"""The jaa MCP server: narrow, typed tools over the core services (T16, ADR 0019).

Read tools never change anything. Mutating tools either produce something the user still
reviews (unreviewed facts, a tailoring revision with its checks, a CV PDF from confirmed facts)
or ask the user directly through an MCP confirmation dialog (elicitation) before changing what
counts as true or done: confirming or correcting facts and changing an application's status.
The dialog is answered by the user in the client, not by the model, and a client that cannot
show it gets an error instead of a silent change. A submission is recorded only when the user
says in that dialog that they submitted it themselves (ADR 0018).

Logs carry tool names, ids and counts only.
"""

import json
import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Elicit, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field
from pydantic import JsonValue as PlainJson

from jaa import __version__
from jaa.config import Settings, load_settings
from jaa.core.applications import check_transition, is_archived
from jaa.core.errors import (
    DuplicateApplicationError,
    ImportSourceError,
    JaaError,
    LatexCompileError,
)
from jaa.core.models import (
    FactCandidate,
    FactState,
    ImportRun,
    Job,
    JobMatch,
    JobStatus,
    JsonValue,
    ProfileFact,
    RetentionChoice,
    SubmissionConfirmation,
)
from jaa.core.review import find_conflicts
from jaa.core.search import JobQuery
from jaa.documents import linkedin_suggestions as linkedin_text
from jaa.documents import tailoring as tailor
from jaa.documents.content import CvLanguage
from jaa.documents.engine import find_engine
from jaa.documents.latex import DEFAULT_TEMPLATE, get_template
from jaa.documents.pipeline import BuildOptions
from jaa.importers.base import read_source
from jaa.importers.cv_file import prepare_cv_import
from jaa.importers.linkedin import prepare_linkedin_import
from jaa.importers.text import prepare_text_import
from jaa.logging import setup_logging
from jaa.matching.requirements import RequirementOrigin, extract_requirements
from jaa.mcp import models as out
from jaa.storage import applications as tracker
from jaa.storage import documents as artifacts
from jaa.storage import duplicates as dupes
from jaa.storage import linkedin_profile, searches
from jaa.storage import matches as matching
from jaa.storage import profile as review
from jaa.storage import tailoring as tailoring_service
from jaa.storage.db import open_database
from jaa.storage.profile import active_version_id
from jaa.storage.repository import jobs as jobs_table
from jaa.storage.repository import profile_facts

log = logging.getLogger(__name__)

INSTRUCTIONS: Final = """\
jaa is the user's local job application assistant. Its data is the user's own: facts come only
from their documents and their answers.
- Never invent facts. Imported facts are unreviewed until the user decides on them; only
  decide_facts confirms them, after the user agrees in a confirmation dialog.
- Scores and tailoring are advisory. Blocked tailoring edits are approved by the user in the
  terminal (`jaa cv tailor approve`), not here.
- The user submits applications. Record a submission with update_application_status only after
  they say they submitted; they confirm it again in a dialog.
- Do not ask the user for LinkedIn passwords, cookies or tokens; LinkedIn data comes from their
  data archive, or from `jaa import linkedin-api`, which they run in their own terminal and
  where they type their token themselves. Never take a token in the chat.
"""

READ: Final = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# Appends records the user reviews later; nothing is overwritten or deleted.
ADDS: Final = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
# Changes what counts as true or done; asks the user first.
CONFIRMED: Final = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)

MAX_TEXT_FILE_BYTES: Final = 10_000
DESCRIPTION_LIMIT: Final = 20_000

type ImportKind = Literal["auto", "cv", "linkedin", "text"]
type FactAction = Literal["confirm", "reject", "correct"]


def _plain(value: object) -> Any:
    return json.loads(json.dumps(value))


def _id(value: int | None) -> int:
    if value is None:
        raise ToolError("record has no id")
    return value


def _user_path(text: str, what: str) -> Path:
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ToolError(f"{what} must be an absolute path")
    return path.resolve()


def _confirmed(answer: ElicitationResult[out.Confirmation]) -> bool:
    return isinstance(answer, AcceptedElicitation) and answer.data.confirm


def _ask(message: str) -> Elicit[out.Confirmation]:
    return Elicit(message, out.Confirmation)


@contextmanager
def _database(settings: Settings, tool: str) -> Generator[sqlite3.Connection]:
    """Open the database for one tool call and turn domain errors into tool errors."""
    if not settings.database_path.is_file():
        raise ToolError("No database yet. Ask the user to run `jaa init` first.")
    conn = open_database(settings.database_path)
    try:
        yield conn
    except ToolError:
        raise
    except LatexCompileError as exc:
        raise ToolError("; ".join([str(exc), *exc.problems])) from exc
    except DuplicateApplicationError as exc:
        others = ", ".join(f"#{i}" for i in exc.job_ids)
        raise ToolError(
            f"{exc}. Possible duplicates: {others}. If they are different jobs, the user can "
            "mark the flag with `jaa jobs duplicates mark`, or confirm with allow_duplicate."
        ) from exc
    except JaaError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        # The message may quote personal values, so only the type is logged.
        log.error("tool %s failed with %s", tool, type(exc).__name__)
        raise ToolError(f"internal error in {tool} ({type(exc).__name__})") from exc
    finally:
        conn.close()


# Profile ----------------------------------------------------------------------------------


def _import_kind(path: Path, kind: ImportKind) -> ImportKind:
    if kind != "auto":
        return kind
    suffix = path.suffix.lower()
    if suffix in {".zip", ".csv"}:
        return "linkedin"
    if suffix in {".txt", ".md"}:
        return "text"
    return "cv"


def _prepare_import(
    path: Path | None, text: str | None, kind: ImportKind
) -> tuple[ImportRun, tuple[FactCandidate, ...]]:
    if text is not None:
        prepared = prepare_text_import(text)
        return prepared.run, prepared.candidates
    assert path is not None  # noqa: S101 - checked by the caller
    chosen = _import_kind(path, kind)
    if chosen == "linkedin":
        # Files are never deleted from here; `jaa import linkedin --delete-source` does that.
        linkedin = prepare_linkedin_import([path], retention=RetentionChoice.KEEP)
        return linkedin.run, linkedin.candidates
    if chosen == "text":
        source, data = read_source(path)
        try:
            decoded = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ImportSourceError(f"not UTF-8 text: {source}") from exc
        prepared = prepare_text_import(decoded, source_path=source)
        return prepared.run, prepared.candidates
    prepared = prepare_cv_import(path)
    return prepared.run, prepared.candidates


def _import_out(stored: review.StoredImport) -> out.ImportOut:
    run = stored.result.run
    read: list[str] = []
    skipped: list[str] = []
    headings = run.summary.get("headings")
    for heading in headings if isinstance(headings, list) else []:
        if isinstance(heading, dict):
            title = str(heading.get("title", ""))
            (skipped if heading.get("section") == "unsupported" else read).append(title)
    issues = [
        out.ImportIssue(
            severity=str(issue.get("severity", "error")),
            location=str(issue["location"]) if issue.get("location") else None,
            message=str(issue.get("message", "")),
        )
        for issue in run.errors
        if isinstance(issue, dict)
    ]
    added = len(stored.result.added)
    return out.ImportOut(
        import_run_id=_id(run.id),
        source_type=run.source_type,
        status=run.status,
        facts_added=added,
        already_known=stored.result.duplicates,
        earlier_import_ids=[_id(r.id) for r in stored.earlier],
        conflicting_fields=list(stored.conflicts),
        sections_read=read,
        sections_not_imported=skipped,
        issues=issues,
        next_step=(
            "Go through the new facts with the user (review_facts), then record their decisions "
            "with decide_facts."
            if added
            else "Nothing new to review."
        ),
    )


def _fact_out(fact: ProfileFact) -> out.FactOut:
    return out.FactOut(
        id=_id(fact.id),
        field_key=fact.field_key,
        value=None if fact.is_sensitive else fact.value_raw,
        sensitive=fact.is_sensitive,
        state=fact.state,
        origin=fact.origin,
        confidence=fact.confidence,
        import_run_id=fact.import_run_id,
        source_location=fact.source_location,
        corrects_fact_id=fact.corrected_from_fact_id,
    )


def _check_decision(
    conn: sqlite3.Connection, action: FactAction, fact_ids: list[int], value: str | None
) -> list[ProfileFact]:
    if len(set(fact_ids)) != len(fact_ids):
        raise ToolError("fact ids must be unique")
    if action == "correct":
        if len(fact_ids) != 1:
            raise ToolError("correct takes exactly one fact id")
        if value is None or not value.strip():
            raise ToolError("correct needs the corrected value")
    elif value is not None:
        raise ToolError("value is only used with correct")
    return [profile_facts.get(conn, i) for i in fact_ids]


def _decision_message(action: FactAction, facts: list[ProfileFact], value: str | None) -> str:
    lines = [f"  #{f.id} {f.field_key}: {' '.join(f.value_raw.split())}" for f in facts]
    if action == "correct":
        return (
            "Correct this fact? The new value is stored as confirmed and the old one is kept "
            f"as rejected.\n{lines[0]}\n  new value: {value}"
        )
    if action == "confirm":
        head = "Confirm these facts as correct? jaa will use them in CVs, matches and answers."
    else:
        head = "Reject these facts as wrong? jaa will not use them."
    return head + "\n" + "\n".join(lines)


# Jobs -------------------------------------------------------------------------------------


def _score(found: JobMatch | None) -> float | None:
    if found is None or any(
        isinstance(u, dict) and u.get("code") == "nothing_to_compare" for u in found.uncertainty
    ):
        return None
    return found.score


def _summary(job: Job, partners: list[int], found: JobMatch | None) -> out.JobSummary:
    return out.JobSummary(
        id=_id(job.id),
        title=job.title,
        employer=job.employer,
        location=job.location,
        source=job.source,
        url=job.source_url or job.canonical_url,
        status=job.status,
        archived=is_archived(job.status),
        first_seen_at=job.first_seen_at,
        no_longer_listed=job.removed_at is not None,
        possible_duplicates=partners,
        match_score=_score(found),
    )


def _label(item: dict[str, JsonValue]) -> str:
    text = str(item.get("requirement", ""))
    level = item.get("level")
    return f"{text} ({level})" if level else text


def _match_out(found: JobMatch, job: Job, active: int | None) -> out.MatchOut:
    met: list[str] = []
    unreviewed: list[str] = []
    missing: list[str] = []
    mismatches: list[str] = []
    for item in [*found.evidence, *found.gaps]:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "preference":
            mismatches.append(f"{item.get('preference')}: {item.get('reason')}")
            continue
        {"met": met, "unreviewed_only": unreviewed, "missing": missing}.get(
            str(item.get("outcome")), missing
        ).append(_label(item))
    return out.MatchOut(
        score=_score(found),
        profile_version_id=found.profile_version_id,
        matched_at=found.created_at,
        outdated_because=matching.outdated_reasons(found, job, active),
        met=met,
        unreviewed_only=unreviewed,
        missing=missing,
        preference_mismatches=mismatches,
    )


def _application_out(job: Job, started: datetime, submitted: datetime | None) -> out.ApplicationOut:
    return out.ApplicationOut(
        job_id=_id(job.id),
        title=job.title,
        employer=job.employer,
        status=job.status,
        archived=is_archived(job.status),
        status_changed_at=job.status_changed_at,
        started_at=started,
        submitted_at=submitted,
    )


def _job_detail(conn: sqlite3.Connection, job_id: int, description_chars: int) -> out.JobDetail:
    job = jobs_table.get(conn, job_id)
    flags = dupes.flags_for_job(conn, job_id)
    latest = matching.latest_match(conn, job_id)
    application = tracker.get_application(conn, job_id)
    tailoring_row = tailoring_service.latest(conn, job_id)
    requirements = extract_requirements(job)
    description = job.description or None
    truncated = description is not None and len(description) > description_chars
    if description is not None:
        description = description[:description_chars] or None
    partners = [f.partner(job_id).id for f in flags if f.verdict is None]
    return out.JobDetail(
        job=_summary(job, [p for p in partners if p is not None], latest),
        description=description,
        description_truncated=truncated,
        requirements=out.Requirements(
            listed_skills=[
                r.label() for r in requirements.requirements if r.origin is RequirementOrigin.LISTED
            ],
            required=[
                r.text for r in requirements.required if r.origin is not RequirementOrigin.LISTED
            ],
            nice_to_have=[r.text for r in requirements.preferred],
            responsibilities=list(requirements.responsibilities),
            questions=[q.label for q in requirements.questions],
            experience_level=requirements.experience_level,
            work_arrangement=(
                requirements.work_arrangement.value if requirements.work_arrangement else None
            ),
        ),
        match=None if latest is None else _match_out(latest, job, active_version_id(conn)),
        application=(
            None
            if application is None
            else _application_out(job, application.created_at, application.submitted_at)
        ),
        duplicates=[
            out.DuplicateOut(
                flag_id=f.id,
                other_job_id=_id(f.partner(job_id).id),
                reason=f.flag.reason.value,
                verdict=f.verdict.value if f.verdict else None,
            )
            for f in flags
        ],
        latest_tailoring_revision=tailoring_row.revision if tailoring_row else None,
    )


# Tailoring --------------------------------------------------------------------------------


def _check_fields(check: tailor.Check) -> tuple[str, bool, list[out.CheckIssue]]:
    if not check.issues:
        status = "ok"
    elif check.blocked:
        status = "blocked"
    else:
        status = "approved"
    issues = [out.CheckIssue(code=i.code.value, message=i.message) for i in check.issues]
    return status, check.approvable, issues


def _tailoring_out(evaluated: tailoring_service.Evaluated, active: int | None) -> out.TailoringOut:
    job, row, plan, result = evaluated.job, evaluated.row, evaluated.plan, evaluated.result
    changes = {c.number: c for c in result.changes}
    edits: list[out.EditOut] = []
    for number, (edit, check) in enumerate(
        zip(plan.edits, result.edit_checks, strict=True), start=1
    ):
        status, approvable, issues = _check_fields(check)
        change = changes.get(number)
        edits.append(
            out.EditOut(
                number=number,
                kind=edit.kind.value,
                target=edit.target,
                before=change.before if change else None,
                after=change.after if change else edit.text,
                fact_ids=list(check.fact_ids),
                reason=edit.reason,
                status=status,
                approvable=approvable,
                issues=issues,
            )
        )
    paragraphs: list[out.ParagraphOut] = []
    for paragraph, check in zip(plan.cover_letter, result.paragraph_checks, strict=True):
        status, approvable, issues = _check_fields(check)
        paragraphs.append(
            out.ParagraphOut(
                number=check.number,
                text=paragraph.text,
                fact_ids=list(check.fact_ids),
                status=status,
                approvable=approvable,
                issues=issues,
            )
        )
    if result.blocked:
        next_step = (
            "Some edits are blocked because the confirmed facts do not back their wording. "
            "Change them and call draft_tailored_cv with the plan, or the user approves "
            f"approvable ones in a terminal: `jaa cv tailor approve {job.id} --edit N`."
        )
    else:
        next_step = f"Ready to build: generate_cv_pdf with job_id={job.id}."
    return out.TailoringOut(
        job_id=_id(job.id),
        revision=row.revision,
        origin=row.origin.value,
        profile_version_id=row.profile_version_id,
        profile_changed_since=active != row.profile_version_id,
        edits=edits,
        cover_letter=paragraphs,
        blocked=result.blocked,
        plan=_plain(plan.to_json()),
        next_step=next_step,
    )


def _read_text_file(text: str, what: str) -> str:
    path = _user_path(text, what)
    if not path.is_file():
        raise ToolError(f"{what} is not a file: {path}")
    data = path.read_bytes()
    if len(data) > MAX_TEXT_FILE_BYTES:
        raise ToolError(f"{what} is larger than 10 kB")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"{what} must be UTF-8 text") from exc


# Applications -----------------------------------------------------------------------------


def _utc(at: datetime | None) -> datetime | None:
    if at is None:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _status_message(
    conn: sqlite3.Connection, job_id: int, status: JobStatus, allow_duplicate: bool
) -> str:
    """Check the change can be made and describe it for the confirmation dialog."""
    job = jobs_table.get(conn, job_id)
    application = tracker.get_application(conn, job_id)
    conflicts = tracker.find_application_conflicts(conn, job_id)
    starting = application is None and status in {JobStatus.PREPARING, JobStatus.READY_FOR_REVIEW}
    if (starting or status is JobStatus.SUBMITTED) and conflicts and not allow_duplicate:
        others = ", ".join(f"#{c.job.id}" for c in conflicts)
        raise ToolError(
            f"jobs {others} look like the same role and were already prepared or submitted; "
            "check them with get_job, or ask the user whether to go on with allow_duplicate"
        )
    where = f"{job.title} at {job.employer} (job #{job_id})"
    duplicate_note = (
        "\nIt goes ahead although jobs "
        + ", ".join(f"#{c.job.id}" for c in conflicts)
        + " look like the same role."
        if conflicts
        else ""
    )
    if status is JobStatus.SUBMITTED:
        if tracker.is_submitted(application):
            raise ToolError(f"job #{job_id} is already recorded as submitted")
        return (
            f"Did you submit your application for {where} yourself? jaa records it as "
            f"submitted on your word. Leave this unticked if you have not.{duplicate_note}"
        )
    if not starting:
        check_transition(job.status, status, submitted=tracker.is_submitted(application))
        return f"Change {where} from {job.status.value} to {status.value}?"
    return f"Start an application for {where} and set it to {status.value}?{duplicate_note}"


def _apply_status(
    conn: sqlite3.Connection,
    job_id: int,
    status: JobStatus,
    *,
    note: str | None,
    allow_duplicate: bool,
    submitted_at: datetime | None,
    reference: str | None,
    document_ids: list[int] | None,
) -> out.StatusOut:
    documents = answers = 0
    if status is JobStatus.SUBMITTED:
        tracker.record_submission(
            conn,
            job_id,
            confirmation=SubmissionConfirmation.USER,
            submitted_at=_utc(submitted_at),
            reference=reference,
            note=note,
            document_ids=document_ids,
            allow_duplicates=allow_duplicate,
        )
        record = tracker.application_record(conn, job_id)
        documents, answers = len(record.documents), len(record.answer_ids)
        message = (
            f"Recorded the submission with {documents} document(s) and {answers} saved answer(s)."
        )
    elif tracker.get_application(conn, job_id) is None and status in {
        JobStatus.PREPARING,
        JobStatus.READY_FOR_REVIEW,
    }:
        tracker.start_application(conn, job_id, allow_duplicates=allow_duplicate, note=note)
        if status is JobStatus.READY_FOR_REVIEW:
            tracker.set_application_status(conn, job_id, status)
        message = "Started the application."
    else:
        tracker.set_application_status(conn, job_id, status, note=note)
        message = "Status changed."
    job = jobs_table.get(conn, job_id)
    application = tracker.get_application(conn, job_id)
    return out.StatusOut(
        confirmed_by_user=True,
        job_id=job_id,
        status=job.status,
        submitted_at=application.submitted_at if application else None,
        documents_linked=documents,
        answers_linked=answers,
        message=message,
    )


# Server -----------------------------------------------------------------------------------

JobId = Annotated[int, Field(description="Job id, from search_jobs.", ge=1)]
Note = Annotated[
    str | None, Field(description="A note for the job's status history.", max_length=2000)
]
AllowDuplicate = Annotated[
    bool,
    Field(
        description="Go on although another prepared or submitted job looks like the same role. "
        "Only when the user said so."
    ),
]


def build_server(settings: Settings) -> MCPServer:
    """Create the MCP server with every tool bound to the user's data directory."""
    server = MCPServer("jaa", instructions=INSTRUCTIONS, version=__version__, log_level="WARNING")

    @server.tool(annotations=ADDS)
    def import_profile(
        path: Annotated[
            str | None,
            Field(
                description="Absolute path to a CV (PDF, DOCX, .tex), a LinkedIn data archive "
                "(.zip) or CSV from it, or a UTF-8 text file."
            ),
        ] = None,
        text: Annotated[
            str | None,
            Field(description="Profile text the user pasted, with headings such as Experience."),
        ] = None,
        kind: Annotated[
            ImportKind,
            Field(description="What the file is; auto decides by its extension."),
        ] = "auto",
    ) -> out.ImportOut:
        """Import the user's CV, LinkedIn data archive or pasted profile text as UNREVIEWED facts.

        Nothing becomes part of the profile until the user decides on each fact (review_facts,
        then decide_facts). Re-importing skips values already stored and never changes
        confirmed facts. LinkedIn data comes only from the user's own archive.
        """
        if (path is None) == (text is None):
            raise ToolError("give either path or text")
        resolved = None if path is None else _user_path(path, "path")
        with _database(settings, "import_profile") as conn:
            run, candidates = _prepare_import(resolved, text, kind)
            stored = review.store_import(conn, run, candidates)
            log.info("mcp import run=%s added=%d", stored.result.run.id, len(stored.result.added))
            return _import_out(stored)

    @server.tool(annotations=READ)
    def review_facts(
        state: Annotated[FactState, Field(description="Facts in this state.")] = (
            FactState.UNREVIEWED
        ),
        field_prefix: Annotated[
            str | None, Field(description="Only field keys starting with this, e.g. skills.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> out.ReviewFactsOut:
        """List profile facts to review with the user, and fields with competing values.

        Show the user each value and where it came from; they decide. Sensitive values are
        left out; the user can see them with `jaa profile show ID`.
        """
        with _database(settings, "review_facts") as conn:
            facts = profile_facts.list(conn, state=state)
            everything = profile_facts.list(conn)
        if field_prefix:
            facts = [f for f in facts if f.field_key.startswith(field_prefix)]
        conflicts = [
            out.ConflictOut(
                field_key=c.field_key,
                confirmed_fact_id=c.current.id if c.current else None,
                incoming_fact_ids=[_id(f.id) for f in c.candidates],
            )
            for c in find_conflicts(everything)
            if not field_prefix or c.field_key.startswith(field_prefix)
        ]
        return out.ReviewFactsOut(
            facts=[_fact_out(f) for f in facts[:limit]],
            total=len(facts),
            conflicts=conflicts,
            next_step=(
                "Record what the user decides with decide_facts. For a field with competing "
                "values, confirming another value needs replace=true."
            ),
        )

    def ask_decision(
        action: FactAction,
        fact_ids: list[int],
        value: str | None = None,
        replace: bool = False,
    ) -> Elicit[out.Confirmation]:
        with _database(settings, "decide_facts") as conn:
            facts = _check_decision(conn, action, fact_ids, value)
        message = _decision_message(action, facts, value)
        if replace:
            message += "\nA value already confirmed for the same field is replaced."
        return _ask(message)

    @server.tool(annotations=CONFIRMED)
    def decide_facts(
        action: Annotated[
            FactAction,
            Field(description="confirm (correct as is), reject (wrong) or correct (new value)."),
        ],
        fact_ids: Annotated[list[int], Field(min_length=1, max_length=100)],
        answer: Annotated[ElicitationResult[out.Confirmation], Resolve(ask_decision)],
        value: Annotated[
            str | None,
            Field(
                description="With correct: the right value, as the user gave it.", max_length=5000
            ),
        ] = None,
        replace: Annotated[
            bool,
            Field(description="Replace a value already confirmed for the same field."),
        ] = False,
    ) -> out.DecideFactsOut:
        """Record the user's review decision on facts. The user confirms it in a dialog.

        Use only for decisions the user made: confirm facts they said are right, reject those
        they said are wrong, correct one with the value they gave. Never decide for them.
        """
        if not _confirmed(answer):
            return out.DecideFactsOut(
                confirmed_by_user=False,
                changed=[],
                profile_version_id=None,
                message="The user did not confirm; nothing changed.",
            )
        with _database(settings, "decide_facts") as conn:
            _check_decision(conn, action, fact_ids, value)
            if action == "confirm":
                result = review.confirm_facts(conn, fact_ids, replace=replace)
            elif action == "reject":
                result = review.reject_facts(conn, fact_ids)
            else:
                assert value is not None  # noqa: S101 - checked by _check_decision
                result = review.correct_fact(conn, fact_ids[0], value, value, replace=replace)
        return out.DecideFactsOut(
            confirmed_by_user=True,
            changed=[
                out.ChangedFact(id=_id(f.id), field_key=f.field_key, state=f.state)
                for f in result.changed
            ],
            profile_version_id=result.version.id if result.version else None,
            message=f"{len(result.changed)} fact(s) changed.",
        )

    @server.tool(annotations=READ)
    def search_jobs(
        text: Annotated[
            str | None, Field(description="Words that must all appear, in any order.")
        ] = None,
        company: Annotated[str | None, Field(description="Employer name contains.")] = None,
        location: Annotated[str | None, Field(description="Location contains.")] = None,
        sources: Annotated[list[str] | None, Field(description="Only these sources.")] = None,
        statuses: Annotated[
            list[JobStatus] | None, Field(description="Only jobs with these statuses.")
        ] = None,
        include_removed: Annotated[
            bool, Field(description="Also jobs the source no longer lists.")
        ] = False,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
    ) -> out.SearchJobsOut:
        """Search stored job listings, most recently found first.

        Matching ignores case, diacritics and punctuation. It searches what was fetched; it
        does not fetch new listings.
        """
        query = JobQuery(
            text=(text or "").strip() or None,
            company=(company or "").strip() or None,
            location=(location or "").strip() or None,
            sources=tuple(dict.fromkeys(sources or ())),
            statuses=tuple(dict.fromkeys(statuses or ())),
            include_removed=include_removed,
        )
        with _database(settings, "search_jobs") as conn:
            found = searches.search_jobs(conn, query)
            partners = dupes.flagged_job_ids(conn)
            latest = matching.latest_matches(conn)
            fetched = searches.last_fetched(conn)
        return out.SearchJobsOut(
            jobs=[
                _summary(j, partners.get(_id(j.id), []), latest.get(_id(j.id)))
                for j in found[:limit]
            ],
            total=len(found),
            jobs_last_fetched=fetched,
        )

    @server.tool(annotations=READ)
    def get_job(
        job_id: JobId,
        description_chars: Annotated[
            int,
            Field(
                description="Return at most this much of the description.",
                ge=0,
                le=DESCRIPTION_LIMIT,
            ),
        ] = 6000,
    ) -> out.JobDetail:
        """One job: description, requirements, latest match, application and duplicate flags.

        The match score is advisory; `met` requirements are backed by confirmed facts.
        """
        with _database(settings, "get_job") as conn:
            return _job_detail(conn, job_id, description_chars)

    @server.tool(annotations=READ)
    def suggest_linkedin_updates(
        job_ids: Annotated[
            list[int] | None,
            Field(description="Jobs to compare with; default: saved and applied jobs."),
        ] = None,
    ) -> out.LinkedInSuggestionsOut:
        """Suggest LinkedIn profile changes backed by confirmed facts and the user's target jobs.

        Read-only. Compares the latest LinkedIn import with the confirmed profile. Every
        suggestion quotes confirmed facts; offer only that wording, never claims from `gaps`.
        The user edits LinkedIn themselves: never open, fetch or automate LinkedIn pages.
        """
        with _database(settings, "suggest_linkedin_updates") as conn:
            report = linkedin_profile.linkedin_suggestions(conn, job_ids or ())
        if report is None:
            return out.LinkedInSuggestionsOut(
                linkedin_import_id=None,
                linkedin_imported_at=None,
                target_job_ids=[],
                suggestions=[],
                gaps=[],
                unreviewed_linkedin_facts=0,
                next_step="No LinkedIn import yet: import the user's data archive with "
                "import_profile, or the user runs `jaa import linkedin-api` themselves.",
            )
        found = report.result
        return out.LinkedInSuggestionsOut(
            linkedin_import_id=report.run.id,
            linkedin_imported_at=report.run.imported_at,
            target_job_ids=[_id(j.id) for j in report.jobs],
            suggestions=[
                out.LinkedInSuggestionOut.model_validate(linkedin_text.to_json(s))
                for s in found.suggestions
            ],
            gaps=[
                out.LinkedInGapOut(
                    requirement=g.requirement,
                    importance=g.importance.value,
                    job_ids=list(g.job_ids),
                    unreviewed_only=g.unreviewed_only,
                )
                for g in found.gaps
            ],
            unreviewed_linkedin_facts=found.unreviewed_linkedin,
            next_step="Go through the suggestions with the user; they edit LinkedIn "
            "themselves. For a gap, ask whether it is true and add it as a fact first.",
        )

    @server.tool(annotations=READ)
    def get_tailored_cv(
        job_id: JobId,
        revision: Annotated[
            int | None, Field(description="Revision; the latest when omitted.", ge=1)
        ] = None,
    ) -> out.TailoringOut:
        """Show a job's tailored CV plan: each edit, the facts behind it and its check."""
        with _database(settings, "get_tailored_cv") as conn:
            evaluated = tailoring_service.evaluate(conn, job_id, revision)
            return _tailoring_out(evaluated, active_version_id(conn))

    @server.tool(annotations=ADDS)
    def draft_tailored_cv(
        job_id: JobId,
        plan: Annotated[
            dict[str, PlainJson] | None,
            Field(
                description=(
                    'Omit to let jaa propose a plan from the job match. Or a plan {"edits": '
                    '[...], "cover_letter": [...]}: each edit has kind hide, move (needs '
                    '"position"), emphasize or rewrite (needs "text"); target headline, summary, '
                    "experience.<id>, education.<id>, experience.<id>.description[<n>], "
                    "skills.<key> or certifications.<key>; fact_ids citing the confirmed facts "
                    'that back it; and a "reason". Cover letter paragraphs are {"text", '
                    '"fact_ids"}. Start from the plan get_tailored_cv returns.'
                )
            ),
        ] = None,
    ) -> out.TailoringOut:
        """Propose or save a tailored CV plan for a job as a new revision, and check it.

        Every rewrite and cover letter paragraph is checked against the confirmed facts it
        cites; unbacked wording is blocked, never applied silently. Do not add claims the
        user's confirmed facts do not support.
        """
        with _database(settings, "draft_tailored_cv") as conn:
            if plan is None:
                evaluated = tailoring_service.propose(conn, job_id)
            else:
                parsed = tailor.plan_from_json(_plain(plan))
                evaluated = tailoring_service.save_edited(conn, job_id, parsed)
            return _tailoring_out(evaluated, active_version_id(conn))

    @server.tool(annotations=ADDS)
    def generate_cv_pdf(
        job_id: Annotated[
            int | None,
            Field(description="Build the job's tailored CV; omit for the general CV.", ge=1),
        ] = None,
        revision: Annotated[
            int | None, Field(description="Tailoring revision; the latest when omitted.", ge=1)
        ] = None,
        template: Annotated[str, Field(description="CV template id.")] = DEFAULT_TEMPLATE,
        language: Annotated[
            CvLanguage, Field(description="Language of headings and dates.")
        ] = CvLanguage.EN,
        photo_path: Annotated[
            str | None, Field(description="Absolute path to a JPEG or PNG photo.")
        ] = None,
        consent_file: Annotated[
            str | None, Field(description="Absolute path to the user's consent clause text.")
        ] = None,
    ) -> out.CvPdfOut:
        """Build a CV PDF from confirmed facts into the data directory and record it.

        Unreviewed facts are left out. A tailored CV is refused while edits are blocked.
        """
        if revision is not None and job_id is None:
            raise ToolError("revision needs job_id")
        photo = None if photo_path is None else _user_path(photo_path, "photo_path")
        consent = None if consent_file is None else _read_text_file(consent_file, "consent_file")
        options = BuildOptions(language=language, photo=photo, consent_text=consent)
        with _database(settings, "generate_cv_pdf") as conn:
            chosen = get_template(template)
            engine = find_engine()
            if job_id is None:
                built = artifacts.build_profile_cv(
                    conn,
                    template=chosen,
                    engine=engine,
                    output_dir=settings.documents_dir,
                    options=options,
                )
            else:
                built = artifacts.build_tailored_cv(
                    conn,
                    job_id,
                    revision=revision,
                    template=chosen,
                    engine=engine,
                    output_dir=settings.documents_dir,
                    options=options,
                )
        result = built.output
        return out.CvPdfOut(
            document_id=_id(built.pdf.id),
            pdf_path=str(result.pdf),
            tex_path=str(result.tex),
            pages=result.pages,
            template=chosen.version_label,
            engine=engine.name,
            profile_version_id=built.profile_version_id,
            job_id=job_id,
            facts_used=built.facts_used,
            changes_applied=built.changes,
            unreviewed_left_out=len(built.left_out),
            layout_warnings=list(result.warnings),
            text_missing_from_pdf=len(result.missing_text),
        )

    @server.tool(annotations=READ)
    def list_applications(
        statuses: Annotated[
            list[JobStatus] | None, Field(description="Only these statuses.")
        ] = None,
    ) -> out.ApplicationsOut:
        """List the user's applications, most recently changed first."""
        with _database(settings, "list_applications") as conn:
            found = tracker.list_applications(conn, statuses or ())
        return out.ApplicationsOut(
            applications=[
                _application_out(job, application.created_at, application.submitted_at)
                for job, application in found
            ]
        )

    def ask_status(
        job_id: int, status: JobStatus, allow_duplicate: bool = False
    ) -> Elicit[out.Confirmation]:
        with _database(settings, "update_application_status") as conn:
            return _ask(_status_message(conn, job_id, status, allow_duplicate))

    @server.tool(annotations=CONFIRMED)
    def update_application_status(
        job_id: JobId,
        status: Annotated[
            JobStatus,
            Field(
                description="preparing starts an application (checking for duplicates); "
                "submitted records that the user submitted it themselves."
            ),
        ],
        answer: Annotated[ElicitationResult[out.Confirmation], Resolve(ask_status)],
        note: Note = None,
        allow_duplicate: AllowDuplicate = False,
        submitted_at: Annotated[
            datetime | None,
            Field(description="With submitted: when the user submitted (UTC); default now."),
        ] = None,
        reference: Annotated[
            str | None,
            Field(description="With submitted: the employer's reference.", max_length=200),
        ] = None,
        document_ids: Annotated[
            list[int] | None,
            Field(
                description="With submitted: documents sent; default the newest CV and "
                "cover letter built for the job."
            ),
        ] = None,
    ) -> out.StatusOut:
        """Change an application's status after the user asked for it; they confirm in a dialog.

        jaa never submits applications. Use status=submitted only after the user said they
        submitted the form themselves; the dialog asks them again.
        """
        if not _confirmed(answer):
            with _database(settings, "update_application_status") as conn:
                job = jobs_table.get(conn, job_id)
            return out.StatusOut(
                confirmed_by_user=False,
                job_id=job_id,
                status=job.status,
                submitted_at=None,
                documents_linked=0,
                answers_linked=0,
                message="The user did not confirm; nothing changed.",
            )
        if status is not JobStatus.SUBMITTED and (
            submitted_at is not None or reference is not None or document_ids is not None
        ):
            raise ToolError("submitted_at, reference and document_ids go with status=submitted")
        with _database(settings, "update_application_status") as conn:
            return _apply_status(
                conn,
                job_id,
                status,
                note=note,
                allow_duplicate=allow_duplicate,
                submitted_at=submitted_at,
                reference=reference,
                document_ids=document_ids,
            )

    return server


def serve() -> None:
    """Run the server over stdio for Claude Code. Logs go to stderr, scrubbed."""
    settings = load_settings()
    server = build_server(settings)
    handler = setup_logging(settings.log_level)
    # The SDK installs its own root handler; route everything through the scrubbing one.
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    server.run("stdio")
