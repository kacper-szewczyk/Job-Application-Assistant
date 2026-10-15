"""The skill's end-to-end walkthrough (T17): onboarding, a tailored CV, a tracked application.

Each step uses the tool or command the skill names for it, in the same order. Fetching is
replaced by stored listings, because tests never hit the network.
"""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.config import Settings, ensure_data_dir, load_settings
from jaa.connectors.base import JobListing
from jaa.core.job_details import JobDetails, ListedSkill
from jaa.documents.engine import Engine
from jaa.storage import listings
from jaa.storage.db import open_database
from tests.integration.test_mcp import call

SKILL: Final = Path(__file__).resolve().parents[2] / "skills/job-application-assistant/SKILL.md"
T1: Final = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
PASTED: Final = """\
Robin Synthetic
robin.synthetic@example.test

Experience
Widget Engineer at Example Widgets
2021 - present
Built widget pipelines in Python.

Skills
Python
Kubernetes

Languages
English (C1)
"""

runner = CliRunner()


def _cli(settings: Settings, *args: str) -> str:
    """Run a command the skill lets Claude run, and check it is one the skill names."""
    words = [a for a in args if re.fullmatch(r"[a-z][a-z-]*", a)]
    assert f"jaa {' '.join(words)}" in SKILL.read_text(encoding="utf-8")
    result = runner.invoke(
        app, list(args), env={"JAA_DATA_DIR": str(settings.data_dir), "COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = load_settings({"JAA_DATA_DIR": str(tmp_path / "data")})
    ensure_data_dir(settings)
    return settings


@pytest.mark.latex
def test_onboarding_to_tailored_cv_to_tracked_application(
    settings: Settings, latex_engine: Engine
) -> None:
    # 1. Onboarding: set up, import, review with the user, confirm what they agreed to.
    _cli(settings, "init")
    imported = call(settings, "import_profile", {"text": PASTED}).data
    assert imported["facts_added"] > 0
    pending = call(settings, "review_facts").data
    assert {f["state"] for f in pending["facts"]} == {"unreviewed"}
    fact_ids = [f["id"] for f in pending["facts"]]
    confirmed = call(
        settings, "decide_facts", {"action": "confirm", "fact_ids": fact_ids}, reply="yes"
    )
    assert confirmed.asked, "the user is asked before facts are confirmed"
    assert len(confirmed.data["changed"]) == len(fact_ids)
    assert call(settings, "review_facts").data["total"] == 0

    # 2. Search: a stored listing stands in for `jaa jobs fetch`; then search, match, read.
    conn = open_database(settings.database_path)
    try:
        listing = JobListing(
            source_job_id="w-1",
            url="https://jobs.example.test/w-1",
            title="Platform Engineer",
            company="Synthetic Platforms",
            location="Remote",
            description="Run synthetic platforms.",
            details=JobDetails(skills=(ListedSkill(name="Python"), ListedSkill(name="Kubernetes"))),
        )
        listings.store_listings(conn, "test", "all", [listing], at=T1)
    finally:
        conn.close()
    found = call(settings, "search_jobs", {"text": "platform"}).data["jobs"]
    assert [j["id"] for j in found] == [1]
    _cli(settings, "jobs", "match", "1")
    _cli(settings, "jobs", "matches")
    job = call(settings, "get_job", {"job_id": 1}).data
    assert job["match"] is not None

    # 3. Tailoring: propose from the match, then build the PDF.
    plan = call(settings, "draft_tailored_cv", {"job_id": 1}).data
    assert plan["blocked"] is False
    assert call(settings, "get_tailored_cv", {"job_id": 1}).data["revision"] == plan["revision"]
    pdf = call(settings, "generate_cv_pdf", {"job_id": 1}).data
    assert Path(pdf["pdf_path"]).is_file()
    assert pdf["unreviewed_left_out"] == 0

    # 4. Answers: see how the job's questions would be answered.
    _cli(settings, "answers", "prepare", "1")

    # 5. Apply: start with the user's yes, hand over, record only the user's own submission.
    started = call(
        settings, "update_application_status", {"job_id": 1, "status": "preparing"}, reply="yes"
    )
    assert started.data["status"] == "preparing"
    handoff = _cli(settings, "apply", "handoff", "1")
    assert "https://jobs.example.test/w-1" in handoff
    assert Path(pdf["pdf_path"]).name in handoff

    submit = {"job_id": 1, "status": "submitted", "reference": "SYN-1"}
    not_yet = call(settings, "update_application_status", submit, reply="no")
    assert not_yet.data["confirmed_by_user"] is False
    assert call(settings, "list_applications").data["applications"][0]["submitted_at"] is None

    submitted = call(settings, "update_application_status", submit, reply="yes")
    assert "Did you submit" in submitted.asked[0]
    assert submitted.data["status"] == "submitted"
    assert submitted.data["documents_linked"] >= 1
    tracked = call(settings, "list_applications").data["applications"]
    assert [(a["job_id"], a["status"]) for a in tracked] == [(1, "submitted")]
    assert "submitted" in _cli(settings, "apply", "show", "1")
