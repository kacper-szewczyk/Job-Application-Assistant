"""The MCP server end to end, through an in-process MCP client (T16, ADR 0019)."""

import io
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import anyio
import mcp_types as types
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.session import ClientRequestContext
from mcp.shared.exceptions import MCPError

from jaa.config import Settings, ensure_data_dir, load_settings
from jaa.connectors.base import JobListing
from jaa.core.job_details import JobDetails, ListedSkill
from jaa.core.models import (
    FactCandidate,
    FactState,
    ImportRun,
    ImportSourceType,
    ImportStatus,
    JobStatus,
    RetentionChoice,
    StatusActor,
)
from jaa.documents.engine import Engine
from jaa.mcp.server import build_server
from jaa.storage import listings
from jaa.storage import matches as matching
from jaa.storage import profile as review
from jaa.storage.db import open_database
from jaa.storage.jobs import set_job_status
from jaa.storage.repository import applications, jobs, profile_facts
from tests.fixtures.cv_profile import PROFILE, store_profile
from tests.fixtures.linkedin_builders import sample_archive

T1 = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
EMAIL = str(PROFILE["contact.email"])
PASTED = "Experience\nSynthetic Gizmo Maker at Gizmo Labs\n\nSkills\nSynthetic-Fortran\n"

READ_TOOLS = {
    "review_facts",
    "search_jobs",
    "get_job",
    "get_tailored_cv",
    "list_applications",
    "suggest_linkedin_updates",
}
ADDING_TOOLS = {"import_profile", "draft_tailored_cv", "generate_cv_pdf"}
CONFIRMED_TOOLS = {"decide_facts", "update_application_status"}


# A client without confirmation dialogs gets a protocol error (wrapped by the in-process client).
def no_dialog() -> pytest.RaisesGroup[MCPError]:
    return pytest.RaisesGroup(
        pytest.RaisesExc(MCPError, match="elicitation capability"),
        allow_unwrapped=True,
        flatten_subgroups=True,
    )


type Reply = Literal["yes", "no", "decline"] | None


@dataclass
class Call:
    result: types.CallToolResult
    asked: list[str] = field(default_factory=list[str])

    @property
    def data(self) -> dict[str, Any]:
        assert not self.result.is_error, self.text
        assert self.result.structured_content is not None
        return self.result.structured_content

    @property
    def text(self) -> str:
        return " ".join(c.text for c in self.result.content if isinstance(c, types.TextContent))


def call(
    settings: Settings,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    reply: Reply = None,
    mode: Literal["auto", "legacy"] = "auto",
) -> Call:
    """Call one tool. `reply` is how the user answers a confirmation; None = no dialog support."""
    asked: list[str] = []

    async def answer(
        context: ClientRequestContext, params: types.ElicitRequestParams
    ) -> types.ElicitResult:
        asked.append(params.message)
        if reply == "decline":
            return types.ElicitResult(action="decline")
        return types.ElicitResult(action="accept", content={"confirm": reply == "yes"})

    async def run() -> types.CallToolResult:
        server = build_server(settings)
        async with Client(
            server, mode=mode, elicitation_callback=answer if reply is not None else None
        ) as client:
            return await client.call_tool(tool, arguments or {})

    return Call(anyio.run(run), asked)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = load_settings({"JAA_DATA_DIR": str(tmp_path / "data")})
    ensure_data_dir(settings)
    open_database(settings.database_path).close()
    return settings


@pytest.fixture
def profile(settings: Settings) -> Settings:
    """A confirmed synthetic profile and one stored job (#1) asking for Python and Kubernetes."""
    conn = open_database(settings.database_path)
    try:
        store_profile(conn, unreviewed={"skills.go": "Go"})
        listing = JobListing(
            source_job_id="job-1",
            url="https://jobs.example.test/1",
            title="Widget Engineer",
            company="Example Widgets",
            location="Remote",
            description="Build widgets for synthetic customers.",
            details=JobDetails(skills=(ListedSkill(name="Python"), ListedSkill(name="Kubernetes"))),
        )
        listings.store_listings(conn, "test", "all", [listing], at=T1)
    finally:
        conn.close()
    return settings


def _db(settings: Settings) -> Any:
    return open_database(settings.database_path)


def test_tools_have_schemas_and_are_split_by_effect(settings: Settings) -> None:
    async def run() -> list[types.Tool]:
        async with Client(build_server(settings)) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in anyio.run(run)}
    assert set(tools) == READ_TOOLS | ADDING_TOOLS | CONFIRMED_TOOLS
    for name, tool in tools.items():
        assert tool.description, name
        assert tool.input_schema["type"] == "object", name
        assert tool.output_schema is not None, name
        assert tool.annotations is not None, name
        assert tool.annotations.open_world_hint is False, name
        assert tool.annotations.read_only_hint is (name in READ_TOOLS), name
        # The confirmation is asked from the user, never taken from the model's arguments.
        assert "answer" not in tool.input_schema.get("properties", {}), name
    for name in CONFIRMED_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None and annotations.destructive_hint is True


def test_tools_need_an_initialised_database(tmp_path: Path) -> None:
    settings = load_settings({"JAA_DATA_DIR": str(tmp_path / "none")})
    result = call(settings, "review_facts")
    assert result.result.is_error
    assert "jaa init" in result.text


def test_import_profile_stores_unreviewed_facts(settings: Settings, tmp_path: Path) -> None:
    imported = call(settings, "import_profile", {"text": PASTED}).data
    assert imported["facts_added"] > 0
    assert imported["status"] in {"succeeded", "partial"}
    assert "review_facts" in imported["next_step"]

    pending = call(settings, "review_facts").data
    assert pending["total"] == imported["facts_added"]
    assert {f["state"] for f in pending["facts"]} == {"unreviewed"}
    assert any(f["value"] == "Synthetic-Fortran" for f in pending["facts"])
    assert call(settings, "review_facts", {"state": "confirmed"}).data["total"] == 0

    again = call(settings, "import_profile", {"text": PASTED}).data
    assert again["facts_added"] == 0 and again["already_known"] == imported["facts_added"]

    source = tmp_path / "profile.txt"
    source.write_text(PASTED.replace("Fortran", "Cobol"), encoding="utf-8")
    from_file = call(settings, "import_profile", {"path": str(source)}).data
    assert from_file["source_type"] == "text" and from_file["facts_added"] >= 1


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "either path or text"),
        ({"path": "/x.pdf", "text": "Skills\nPython"}, "either path or text"),
        ({"path": "relative/cv.pdf"}, "absolute path"),
        ({"path": "/nonexistent/synthetic-cv.pdf"}, "cv.pdf"),
    ],
)
def test_import_profile_refuses_bad_input(
    settings: Settings, arguments: dict[str, Any], message: str
) -> None:
    result = call(settings, "import_profile", arguments)
    assert result.result.is_error
    assert message in result.text


def test_review_facts_hides_sensitive_values(profile: Settings) -> None:
    confirmed = call(profile, "review_facts", {"state": "confirmed", "limit": 500})
    email = next(f for f in confirmed.data["facts"] if f["field_key"] == "contact.email")
    assert email["sensitive"] is True and email["value"] is None
    assert EMAIL not in str(confirmed.result.structured_content)
    skills = call(profile, "review_facts", {"state": "confirmed", "field_prefix": "skills."}).data
    assert {f["value"] for f in skills["facts"]} == {"Python", "C++", "React Native"}


def _fact_id(settings: Settings, key: str) -> int:
    conn = _db(settings)
    try:
        fact = profile_facts.list(conn, field_key=key)[-1]
    finally:
        conn.close()
    assert fact.id is not None
    return fact.id


def _state(settings: Settings, fact_id: int) -> FactState:
    conn = _db(settings)
    try:
        return profile_facts.get(conn, fact_id).state
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_decide_facts_changes_nothing_without_the_users_yes(
    profile: Settings, mode: Literal["auto", "legacy"]
) -> None:
    go = _fact_id(profile, "skills.go")
    arguments = {"action": "confirm", "fact_ids": [go]}
    for reply in ("decline", "no"):
        refused = call(profile, "decide_facts", arguments, reply=reply, mode=mode)
        assert refused.data["confirmed_by_user"] is False
        assert "skills.go: Go" in refused.asked[0]
        assert _state(profile, go) is FactState.UNREVIEWED

    with no_dialog():
        call(profile, "decide_facts", arguments, mode=mode)
    assert _state(profile, go) is FactState.UNREVIEWED

    done = call(profile, "decide_facts", arguments, reply="yes", mode=mode).data
    assert done["confirmed_by_user"] is True
    assert done["changed"] == [{"id": go, "field_key": "skills.go", "state": "confirmed"}]
    assert done["profile_version_id"] is not None
    assert _state(profile, go) is FactState.CONFIRMED


def test_decide_facts_rejects_and_corrects(profile: Settings) -> None:
    go = _fact_id(profile, "skills.go")
    rejected = call(profile, "decide_facts", {"action": "reject", "fact_ids": [go]}, reply="yes")
    assert rejected.data["changed"][0]["state"] == "rejected"
    assert "Reject these facts" in rejected.asked[0]

    python = _fact_id(profile, "skills.python")
    corrected = call(
        profile,
        "decide_facts",
        {"action": "correct", "fact_ids": [python], "value": "Python 3"},
        reply="yes",
    )
    assert "new value: Python 3" in corrected.asked[0]
    states = {c["id"]: c["state"] for c in corrected.data["changed"]}
    assert states[python] == "rejected"
    new_id = _fact_id(profile, "skills.python")
    assert new_id != python and states[new_id] == "confirmed"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"action": "correct", "fact_ids": [1, 2], "value": "x"}, "exactly one fact"),
        ({"action": "correct", "fact_ids": [1]}, "corrected value"),
        ({"action": "confirm", "fact_ids": [1], "value": "x"}, "only used with correct"),
        ({"action": "confirm", "fact_ids": [1, 1]}, "unique"),
        ({"action": "confirm", "fact_ids": [9999]}, "9999"),
    ],
)
def test_decide_facts_checks_the_request_before_asking(
    profile: Settings, arguments: dict[str, Any], message: str
) -> None:
    result = call(profile, "decide_facts", arguments, reply="yes")
    assert result.result.is_error
    assert message in result.text
    assert result.asked == []


def test_confirming_over_a_confirmed_value_needs_replace(profile: Settings) -> None:
    conn = _db(profile)
    try:
        run = ImportRun(
            source_type=ImportSourceType.TEXT,
            imported_at=T1,
            status=ImportStatus.SUCCEEDED,
            retention=RetentionChoice.KEEP,
        )
        candidate = FactCandidate(
            field_key="skills.python", value_raw="Python 3.13", value_normalized="Python 3.13"
        )
        review.add_imported_facts(conn, run, [candidate])
    finally:
        conn.close()
    incoming = _fact_id(profile, "skills.python")
    conflicts = call(profile, "review_facts").data["conflicts"]
    assert [c["field_key"] for c in conflicts] == ["skills.python"]

    refused = call(
        profile, "decide_facts", {"action": "confirm", "fact_ids": [incoming]}, reply="yes"
    )
    assert refused.result.is_error and "conflicts" in refused.text
    done = call(
        profile,
        "decide_facts",
        {"action": "confirm", "fact_ids": [incoming], "replace": True},
        reply="yes",
    )
    assert "replaced" in done.asked[0]
    assert _state(profile, incoming) is FactState.CONFIRMED


def test_search_and_get_job(profile: Settings) -> None:
    found = call(profile, "search_jobs", {"text": "widget"}).data
    assert found["total"] == 1 and "jobs_last_fetched" in found
    job = found["jobs"][0]
    assert job["title"] == "Widget Engineer" and job["match_score"] is None
    assert call(profile, "search_jobs", {"text": "gizmo"}).data["total"] == 0

    detail = call(profile, "get_job", {"job_id": job["id"]}).data
    assert detail["requirements"]["listed_skills"] == ["Python", "Kubernetes"]
    assert detail["description"] == "Build widgets for synthetic customers."
    assert detail["match"] is None and detail["application"] is None

    conn = _db(profile)
    try:
        matching.match_jobs(conn, [job["id"]])
    finally:
        conn.close()
    detail = call(profile, "get_job", {"job_id": job["id"], "description_chars": 5}).data
    assert detail["description"] == "Build" and detail["description_truncated"] is True
    assert detail["match"]["met"] == ["Python"]
    assert detail["match"]["missing"] == ["Kubernetes"]
    assert detail["match"]["outdated_because"] == []
    assert call(profile, "search_jobs").data["jobs"][0]["match_score"] is not None

    missing = call(profile, "get_job", {"job_id": 999})
    assert missing.result.is_error and "999" in missing.text


def _matched(settings: Settings) -> None:
    conn = _db(settings)
    try:
        matching.match_jobs(conn, [1])
    finally:
        conn.close()


def test_draft_tailored_cv_checks_every_edit(profile: Settings) -> None:
    _matched(profile)
    proposed = call(profile, "draft_tailored_cv", {"job_id": 1}).data
    assert proposed["revision"] == 1 and proposed["origin"] == "proposed"
    assert proposed["blocked"] is False
    assert "generate_cv_pdf" in proposed["next_step"]

    plan = proposed["plan"]
    plan["edits"].append(
        {
            "kind": "rewrite",
            "target": "experience.example-co.description[1]",
            "text": "Led the migration of widget pipelines to Kubernetes.",
            "fact_ids": [],
        }
    )
    edited = call(profile, "draft_tailored_cv", {"job_id": 1, "plan": plan}).data
    assert edited["revision"] == 2 and edited["blocked"] is True
    last = edited["edits"][-1]
    assert last["status"] == "blocked" and last["approvable"] is True
    assert [i["code"] for i in last["issues"]] == ["unsupported_terms"]
    assert "jaa cv tailor approve 1" in edited["next_step"]

    shown = call(profile, "get_tailored_cv", {"job_id": 1}).data
    assert shown["revision"] == 2
    assert call(profile, "get_tailored_cv", {"job_id": 1, "revision": 1}).data["blocked"] is False

    bad = call(profile, "draft_tailored_cv", {"job_id": 1, "plan": {"edits": "nope"}})
    assert bad.result.is_error

    refused = call(profile, "generate_cv_pdf", {"job_id": 1})
    assert refused.result.is_error and "blocked: edit" in refused.text


@pytest.mark.latex
def test_generate_cv_pdf(profile: Settings, latex_engine: Engine) -> None:
    general = call(profile, "generate_cv_pdf").data
    assert Path(general["pdf_path"]).is_file()
    assert Path(general["pdf_path"]).parent == profile.documents_dir
    assert general["job_id"] is None and general["unreviewed_left_out"] == 1
    assert general["paper"] == "a4"
    letter = call(profile, "generate_cv_pdf", {"paper": "letter"}).data
    assert letter["paper"] == "letter"

    _matched(profile)
    call(profile, "draft_tailored_cv", {"job_id": 1})
    tailored = call(profile, "generate_cv_pdf", {"job_id": 1, "language": "pl"}).data
    assert tailored["job_id"] == 1 and tailored["changes_applied"] >= 1
    assert tailored["document_id"] != general["document_id"]


def _status(settings: Settings, job_id: int = 1) -> JobStatus:
    conn = _db(settings)
    try:
        return jobs.get(conn, job_id).status
    finally:
        conn.close()


def test_update_application_status_asks_the_user(profile: Settings) -> None:
    start = {"job_id": 1, "status": "preparing", "note": "synthetic note"}
    declined = call(profile, "update_application_status", start, reply="decline")
    assert declined.data["confirmed_by_user"] is False
    assert _status(profile) is JobStatus.DISCOVERED

    started = call(profile, "update_application_status", start, reply="yes")
    assert "Start an application for Widget Engineer at Example Widgets" in started.asked[0]
    assert started.data["status"] == "preparing"

    listed = call(profile, "list_applications").data["applications"]
    assert [(a["job_id"], a["status"], a["submitted_at"]) for a in listed] == [
        (1, "preparing", None)
    ]

    submit = {"job_id": 1, "status": "submitted", "reference": "REF-1"}
    not_yet = call(profile, "update_application_status", submit, reply="no")
    assert "Did you submit your application for Widget Engineer" in not_yet.asked[0]
    assert not_yet.data["confirmed_by_user"] is False
    assert _status(profile) is JobStatus.PREPARING

    with no_dialog():
        call(profile, "update_application_status", submit)
    assert _status(profile) is JobStatus.PREPARING

    submitted = call(profile, "update_application_status", submit, reply="yes").data
    assert submitted["status"] == "submitted" and submitted["submitted_at"] is not None
    conn = _db(profile)
    try:
        application = applications.list(conn)[0]
    finally:
        conn.close()
    assert application.submission_confirmed_by is not None
    assert application.submission_confirmed_by.value == "user"
    assert application.external_reference == "REF-1"

    again = call(profile, "update_application_status", submit, reply="yes")
    assert again.result.is_error and "already recorded as submitted" in again.text
    assert again.asked == []
    backwards = call(
        profile, "update_application_status", {"job_id": 1, "status": "saved"}, reply="yes"
    )
    assert backwards.result.is_error and backwards.asked == []

    interviewing = call(
        profile, "update_application_status", {"job_id": 1, "status": "interviewing"}, reply="yes"
    )
    assert "from submitted to interviewing" in interviewing.asked[0]
    assert _status(profile) is JobStatus.INTERVIEWING


def test_submission_fields_need_status_submitted(profile: Settings) -> None:
    result = call(
        profile,
        "update_application_status",
        {"job_id": 1, "status": "preparing", "reference": "REF"},
        reply="yes",
    )
    assert result.result.is_error and "status=submitted" in result.text
    assert _status(profile) is JobStatus.DISCOVERED


def test_possible_duplicate_application_is_refused_before_asking(profile: Settings) -> None:
    conn = _db(profile)
    try:
        twin = JobListing(
            source_job_id="job-2",
            url="https://jobs.example.test/2",
            title="Widget Engineer",
            company="Example Widgets",
        )
        listings.store_listings(conn, "test", "all", [twin], at=T1)
        set_job_status(conn, 2, JobStatus.SAVED, StatusActor.USER)
    finally:
        conn.close()
    assert (
        call(
            profile, "update_application_status", {"job_id": 2, "status": "preparing"}, reply="yes"
        ).data["status"]
        == "preparing"
    )

    refused = call(
        profile, "update_application_status", {"job_id": 1, "status": "preparing"}, reply="yes"
    )
    assert refused.result.is_error and "#2" in refused.text
    assert refused.asked == []

    allowed = call(
        profile,
        "update_application_status",
        {"job_id": 1, "status": "preparing", "allow_duplicate": True},
        reply="yes",
    )
    assert "look like the same role" in allowed.asked[0]
    assert allowed.data["status"] == "preparing"


def test_personal_values_are_not_logged(
    profile: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    # Unscrubbed, on the jaa logger itself (CLI tests may have stopped it propagating): proves
    # values are never passed to a logger. caplog covers the MCP SDK's loggers.
    logger = logging.getLogger("jaa")
    raw = io.StringIO()
    handler = logging.StreamHandler(raw)
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        call(profile, "import_profile", {"text": PASTED})
        call(profile, "review_facts", {"state": "confirmed", "limit": 500})
        go = _fact_id(profile, "skills.go")
        call(
            profile,
            "decide_facts",
            {"action": "correct", "fact_ids": [go], "value": "Golang-synthetic"},
            reply="yes",
        )
        _matched(profile)
        call(profile, "draft_tailored_cv", {"job_id": 1})
        call(profile, "suggest_linkedin_updates", {"job_ids": [1]})
        call(
            profile,
            "update_application_status",
            {"job_id": 1, "status": "preparing", "note": "synthetic-secret-note"},
            reply="yes",
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    logged = raw.getvalue() + caplog.text
    assert "mcp import run=" in logged
    for value in (
        EMAIL,
        "Synthetic-Fortran",
        "Golang-synthetic",
        "synthetic-secret-note",
        str(PROFILE["identity.name"]),
    ):
        assert value not in logged


def test_suggest_linkedin_updates_without_an_import(profile: Settings) -> None:
    data = call(profile, "suggest_linkedin_updates").data
    assert data["linkedin_import_id"] is None
    assert data["suggestions"] == []
    assert "import_profile" in data["next_step"]


def test_suggest_linkedin_updates_cites_confirmed_facts(profile: Settings, tmp_path: Path) -> None:
    archive = tmp_path / "linkedin.zip"
    archive.write_bytes(sample_archive())
    imported = call(profile, "import_profile", {"path": str(archive)}).data
    conn = _db(profile)
    try:
        set_job_status(conn, 1, JobStatus.SAVED, StatusActor.USER)
        confirmed = {f.id: f for f in profile_facts.list(conn, state=FactState.CONFIRMED)}
    finally:
        conn.close()

    data = call(profile, "suggest_linkedin_updates").data

    assert data["linkedin_import_id"] == imported["import_run_id"]
    assert data["target_job_ids"] == [1]
    suggestions = {s["address"]: s for s in data["suggestions"]}
    headline = suggestions["headline"]
    assert headline["kind"] == "update"
    assert headline["text"] == PROFILE["headline"]
    assert headline["current"] == "Backend engineer for widget platforms"
    for s in data["suggestions"]:
        if s["kind"] != "check":
            assert s["fact_ids"] and all(i in confirmed for i in s["fact_ids"])
    assert [g["requirement"] for g in data["gaps"]] == ["Kubernetes"]
    assert data["unreviewed_linkedin_facts"] > 0
    assert not any(s["section"] == "contact" for s in data["suggestions"])


def test_jaa_mcp_serve_speaks_mcp_over_stdio(profile: Settings) -> None:
    """What Claude Code runs: `jaa mcp serve` as a subprocess, with nothing else on stdout."""
    command = "import sys; from jaa.cli import app; sys.exit(app())"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", command, "mcp", "serve"],
        env={**os.environ, "JAA_DATA_DIR": str(profile.data_dir)},
    )

    async def run() -> tuple[list[str], types.CallToolResult]:
        async with Client(params) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            return names, await client.call_tool("search_jobs", {"text": "widget"})

    names, result = anyio.run(run)
    assert set(names) == READ_TOOLS | ADDING_TOOLS | CONFIRMED_TOOLS
    assert result.structured_content is not None
    assert result.structured_content["total"] == 1
