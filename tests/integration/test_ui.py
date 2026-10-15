"""The local review UI end to end, through Starlette's test client (T18, ADR 0022)."""

import hashlib
import logging
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from jaa.cli import app as cli
from jaa.config import Settings, ensure_data_dir, load_settings
from jaa.core import models as m
from jaa.core.errors import ConfigError
from jaa.storage import answers as answer_service
from jaa.storage import applications as application_service
from jaa.storage import duplicates as duplicate_service
from jaa.storage.db import open_database
from jaa.storage.jobs import add_job
from jaa.storage.repository import applications, document_artifacts, jobs, profile_facts
from jaa.ui.app import bind, build_app
from jaa.ui.security import UiSession, check_loopback
from tests.conftest import T0, ident
from tests.fixtures.cv_profile import PROFILE, store_profile

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
EMAIL = str(PROFILE["contact.email"])
NEW_EMAIL = "avery.new@example.test"
SPONSORSHIP_ANSWER = "synthetic-sponsorship-answer"
ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - refused, never bound
PDF = b"%PDF-1.4\n% synthetic\n%%EOF\n"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = load_settings({"JAA_DATA_DIR": str(tmp_path / "data")})
    ensure_data_dir(settings)
    open_database(settings.database_path).close()
    return settings


@pytest.fixture
def db(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = open_database(settings.database_path)
    yield conn
    conn.close()


@pytest.fixture
def session() -> UiSession:
    return UiSession(host="127.0.0.1", port=PORT)


@pytest.fixture
def client(settings: Settings, session: UiSession) -> Iterator[TestClient]:
    with TestClient(build_app(settings, session), base_url=ORIGIN) as client:
        yield client


@pytest.fixture
def browser(client: TestClient, session: UiSession) -> TestClient:
    """A client that opened the one-time link."""
    response = client.get(f"/login?token={session.login_token}")
    assert response.status_code == 200
    return client


def _csrf(page: str) -> str:
    found = re.search(r'name="csrf" value="([^"]+)"', page)
    assert found, "page has a form"
    return found.group(1)


def _job(db: sqlite3.Connection, source_id: str = "w-1", **fields: str) -> int:
    url = fields.get("url", f"https://boards.example.test/jobs/{source_id}")
    stored = add_job(
        db,
        m.Job(
            source="example_board",
            source_job_id=source_id,
            canonical_url=url,
            source_url=url,
            employer=fields.get("employer", "Example Co"),
            title=fields.get("title", "Widget Engineer"),
            first_seen_at=T0,
            last_seen_at=T0,
            status_changed_at=T0,
        ),
    )
    return ident(stored.id)


def _facts(db: sqlite3.Connection, key: str) -> list[m.ProfileFact]:
    return profile_facts.list(db, field_key=key)


# -- who may connect -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "host", [ALL_INTERFACES, "192.168.1.10", "example.test", "localhost", "::"]
)
def test_only_loopback_addresses_are_accepted(host: str) -> None:
    with pytest.raises(ConfigError, match="loopback"):
        check_loopback(host)
    with pytest.raises(ConfigError):
        bind(host, 0)


def test_binds_to_loopback_on_a_free_port() -> None:
    assert check_loopback("::1") == "::1"
    sock = bind("127.0.0.1", 0)
    try:
        host, port = sock.getsockname()[:2]
        assert host == "127.0.0.1"
        assert port > 0
    finally:
        sock.close()


def test_cli_refuses_a_non_loopback_host(settings: Settings) -> None:
    env = {"JAA_DATA_DIR": str(settings.data_dir)}
    result = CliRunner().invoke(cli, ["ui", "--host", ALL_INTERFACES, "--no-browser"], env=env)
    assert result.exit_code == 2
    assert "loopback" in result.output


def test_pages_need_the_one_time_link(client: TestClient, session: UiSession) -> None:
    assert client.get("/").status_code == 401
    assert client.get("/profile").status_code == 401
    assert client.get("/login?token=wrong").status_code == 403
    assert client.get("/static/app.css").status_code == 200

    response = client.get(f"/login?token={session.login_token}")
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert client.get("/").status_code == 200

    # The link works once: a second browser cannot use it.
    with TestClient(client.app, base_url=ORIGIN) as other:
        assert other.get(f"/login?token={session.login_token}").status_code == 403
        assert other.get("/").status_code == 401


def test_other_host_names_are_refused(browser: TestClient) -> None:
    # A page on another site that rebinds its name to 127.0.0.1 still sends its own Host.
    assert browser.get("/", headers={"host": "attacker.example.test"}).status_code == 400
    assert browser.get("/", headers={"host": f"localhost:{PORT}"}).status_code == 200


def test_responses_forbid_scripts_framing_and_caching(browser: TestClient) -> None:
    response = browser.get("/")
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script" not in csp
    assert "frame-ancestors 'self'" in csp
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "<script" not in response.text
    assert "googleapis" not in browser.get("/static/app.css").text


def test_forms_need_the_csrf_token(browser: TestClient, db: sqlite3.Connection) -> None:
    store_profile(db, unreviewed={"skills.go": "Go"})
    fact = _facts(db, "skills.go")[0]
    data = {"fact_id": str(fact.id), "action": "confirm"}
    assert browser.post("/profile/decide", data=data).status_code == 403
    assert browser.post("/profile/decide", data={**data, "csrf": "wrong"}).status_code == 403
    csrf = _csrf(browser.get("/profile").text)
    cross = browser.post(
        "/profile/decide",
        data={**data, "csrf": csrf},
        headers={"origin": "https://attacker.example.test"},
    )
    assert cross.status_code == 403
    assert _facts(db, "skills.go")[0].state is m.FactState.UNREVIEWED


# -- profile review ------------------------------------------------------------------------


def test_review_confirm_and_reject_facts(browser: TestClient, db: sqlite3.Connection) -> None:
    store_profile(db, unreviewed={"skills.go": "Go", "skills.cobol": "COBOL"})
    go, cobol = _facts(db, "skills.go")[0], _facts(db, "skills.cobol")[0]

    page = browser.get("/profile").text
    assert "skills.go" in page
    assert "skills.python" not in page  # confirmed facts are not in the default queue
    assert EMAIL not in browser.get("/profile?state=confirmed").text  # sensitive: masked
    assert EMAIL in browser.get("/profile?state=confirmed&reveal=1").text

    csrf = _csrf(page)
    done = browser.post(
        "/profile/decide",
        data={"csrf": csrf, "state": "unreviewed", "fact_id": str(go.id), "action": "confirm"},
    )
    assert done.status_code == 200  # followed the redirect back to the queue
    assert "Confirmed 1 fact(s)" in done.text
    browser.post(
        "/profile/decide",
        data={"csrf": csrf, "state": "unreviewed", "fact_id": str(cobol.id), "action": "reject"},
    )
    assert _facts(db, "skills.go")[0].state is m.FactState.CONFIRMED
    assert _facts(db, "skills.cobol")[0].state is m.FactState.REJECTED
    assert "No unreviewed facts" in browser.get("/profile").text


def test_a_conflict_is_replaced_only_when_asked(
    browser: TestClient, db: sqlite3.Connection
) -> None:
    store_profile(db, unreviewed={"contact.email": NEW_EMAIL})
    new = next(f for f in _facts(db, "contact.email") if f.state is m.FactState.UNREVIEWED)
    page = browser.get("/profile?reveal=1").text
    assert "Conflicting values" in page
    assert EMAIL in page
    assert NEW_EMAIL in page

    csrf = _csrf(page)
    data = {"csrf": csrf, "state": "unreviewed", "fact_id": str(new.id), "action": "confirm"}
    refused = browser.post("/profile/decide", data=data)
    assert "replace the confirmed value" in refused.text
    assert profile_facts.get(db, ident(new.id)).state is m.FactState.UNREVIEWED

    browser.post("/profile/decide", data={**data, "replace": "1"})
    confirmed = [f for f in _facts(db, "contact.email") if f.state is m.FactState.CONFIRMED]
    assert [f.id for f in confirmed] == [new.id]


def test_correct_a_fact_keeps_the_original(browser: TestClient, db: sqlite3.Connection) -> None:
    store_profile(db, unreviewed={"skills.go": "Golang (synthetic)"})
    original = _facts(db, "skills.go")[0]
    page = browser.get(f"/profile/facts/{original.id}").text
    assert "Golang (synthetic)" in page

    done = browser.post(
        f"/profile/facts/{original.id}/correct", data={"csrf": _csrf(page), "value": "Go"}
    )
    assert "Saved your correction" in done.text
    facts = {f.id: f for f in _facts(db, "skills.go")}
    corrected = next(f for f in facts.values() if f.origin is m.FactOrigin.CORRECTION)
    assert corrected.value_raw == "Go"
    assert corrected.state is m.FactState.CONFIRMED
    assert corrected.corrected_from_fact_id == original.id
    assert facts[original.id].state is m.FactState.REJECTED
    assert browser.get("/profile/facts/999999").status_code == 404


# -- jobs, answers and applications --------------------------------------------------------


def test_sensitive_questions_are_never_prefilled(
    browser: TestClient, db: sqlite3.Connection
) -> None:
    job = _job(db)
    answer_service.add_job_question(db, job, "Do you require visa sponsorship?", required=True)
    answer_service.add_job_question(db, job, "Why do you want this job?")

    page = browser.get(f"/jobs/{job}?tab=questions").text
    assert "Do you require visa sponsorship?" in page
    assert "sensitive" in page
    csrf = _csrf(page)
    keys = re.findall(r'name="key" value="([^"]+)"', page)
    sponsorship = next(k for k in keys if "sponsorship" in k)

    browser.post(
        f"/jobs/{job}/answers",
        data={"csrf": csrf, "key": sponsorship, "use": "typed", "value": SPONSORSHIP_ANSWER},
    )
    saved = answer_service.current_answer(db, sponsorship)
    assert saved.value == SPONSORSHIP_ANSWER
    assert saved.job_id == job

    # Shown masked until revealed, and only usable once the user has seen it.
    page = browser.get(f"/jobs/{job}?tab=questions").text
    assert SPONSORSHIP_ANSWER not in page
    refused = browser.post(
        f"/jobs/{job}/answers", data={"csrf": csrf, "key": sponsorship, "use": "suggestion"}
    )
    assert "show the sensitive suggestion" in refused.text
    assert SPONSORSHIP_ANSWER in browser.get(f"/jobs/{job}?tab=questions&reveal=1").text
    browser.post(
        f"/jobs/{job}/answers",
        data={"csrf": csrf, "key": sponsorship, "use": "suggestion", "seen": "1"},
    )
    assert answer_service.current_answer(db, sponsorship).origin is m.AnswerOrigin.RECONFIRMED


def test_add_a_question_and_reject_an_empty_answer(
    browser: TestClient, db: sqlite3.Connection
) -> None:
    job = _job(db)
    csrf = _csrf(browser.get(f"/jobs/{job}?tab=questions").text)
    browser.post(f"/jobs/{job}/questions", data={"csrf": csrf, "text": "Notice period?"})
    page = browser.get(f"/jobs/{job}?tab=questions").text
    assert "Notice period?" in page
    key = re.findall(r'name="key" value="([^"]+)"', page)[0]
    done = browser.post(
        f"/jobs/{job}/answers", data={"csrf": csrf, "key": key, "use": "typed", "value": " "}
    )
    assert "the answer must be" in done.text
    assert answer_service.current_answers(db) == {}


def test_submission_needs_the_users_own_tick(browser: TestClient, db: sqlite3.Connection) -> None:
    job = _job(db)
    page = browser.get(f"/jobs/{job}?tab=apply").text
    assert "Start an application" in page
    csrf = _csrf(page)
    browser.post(f"/jobs/{job}/start", data={"csrf": csrf})
    assert jobs.get(db, job).status is m.JobStatus.PREPARING

    page = browser.get(f"/jobs/{job}?tab=apply").text
    assert "I submitted this application myself" in page
    assert 'value="submitted"' not in page  # not offered as a plain status

    refused = browser.post(f"/jobs/{job}/submitted", data={"csrf": csrf, "reference": "R-1"})
    assert "nothing recorded" in refused.text
    assert not application_service.is_submitted(application_service.get_application(db, job))
    sneaky = browser.post(f"/jobs/{job}/status", data={"csrf": csrf, "status": "submitted"})
    assert "only when you confirm" in sneaky.text
    assert jobs.get(db, job).status is m.JobStatus.PREPARING

    browser.post(
        f"/jobs/{job}/submitted",
        data={"csrf": csrf, "self_submitted": "1", "reference": "R-1", "submitted_on": ""},
    )
    stored = applications.list(db, job_id=job)[0]
    assert stored.submission_confirmed_by is m.SubmissionConfirmation.USER
    assert stored.external_reference == "R-1"
    assert jobs.get(db, job).status is m.JobStatus.SUBMITTED

    browser.post(f"/jobs/{job}/status", data={"csrf": csrf, "status": "interviewing"})
    assert jobs.get(db, job).status is m.JobStatus.INTERVIEWING
    history = [e.actor for e in application_service.application_record(db, job).history[1:]]
    assert set(history) == {m.StatusActor.USER}


def test_dashboard_groups_by_status_with_duplicate_flags(
    browser: TestClient, db: sqlite3.Connection
) -> None:
    first = _job(db, "w-1")
    second = _job(db, "w-2", url="https://boards.example.test/jobs/w-1")  # the same link
    other = _job(db, "w-3", title="Gadget Designer", employer="Gadgets Sp. z o.o.")
    duplicate_service.flag_duplicates(db, [first, second], T0)
    application_service.start_application(db, first)
    application_service.start_application(db, other)
    application_service.set_application_status(db, other, m.JobStatus.WITHDRAWN)

    page = browser.get("/applications").text
    assert "Preparing" in page
    assert "Archived" in page
    assert f'href="/jobs/{second}"' in page  # flagged next to the first job
    overview = browser.get("/").text
    assert "Possible duplicates" in overview

    flag = duplicate_service.list_flags(db)[0]
    job_page = browser.get(f"/jobs/{first}").text
    assert "May be the same job" in job_page
    browser.post(
        f"/duplicates/{flag.id}", data={"csrf": _csrf(job_page), "verdict": "different_jobs"}
    )
    assert duplicate_service.list_flags(db) == []
    assert f'href="/jobs/{second}"' not in browser.get("/applications").text


# -- documents -----------------------------------------------------------------------------


def _document(db: sqlite3.Connection, path: Path, job: int | None = None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PDF)
    with db:
        stored = document_artifacts.insert(
            db,
            m.DocumentArtifact(
                kind=m.ArtifactKind.CV_PDF,
                path=str(path),
                job_id=job,
                content_sha256=hashlib.sha256(PDF).hexdigest(),
                created_at=T0,
            ),
        )
    return ident(stored.id)


def test_pdfs_preview_only_from_the_data_dir(
    browser: TestClient, db: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    job = _job(db)
    inside = _document(db, settings.documents_dir / "cv.pdf", job)
    outside = _document(db, tmp_path / "elsewhere" / "cv.pdf")

    response = browser.get(f"/files/{inside}")
    assert response.status_code == 200
    assert response.content == PDF
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert browser.get(f"/files/{outside}").status_code == 404
    assert browser.get("/files/999999").status_code == 404

    assert f'src="/files/{inside}"' in browser.get(f"/jobs/{job}?tab=cv").text
    assert f'src="/files/{outside}"' in browser.get("/documents").text  # newest first
    assert f'src="/files/{inside}"' in browser.get(f"/documents?show={inside}").text


# -- privacy -------------------------------------------------------------------------------


def test_no_personal_values_in_logs(
    browser: TestClient, db: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store_profile(db, unreviewed={"contact.email": NEW_EMAIL})
    job = _job(db)
    answer_service.add_job_question(db, job, "Do you require visa sponsorship?")
    caplog.clear()

    page = browser.get("/profile?reveal=1").text
    csrf = _csrf(page)
    new = next(f for f in _facts(db, "contact.email") if f.state is m.FactState.UNREVIEWED)
    browser.post(
        f"/profile/facts/{new.id}/correct",
        data={"csrf": csrf, "value": "avery.fixed@example.test", "replace": "1"},
    )
    page = browser.get(f"/jobs/{job}?tab=questions").text
    key = re.findall(r'name="key" value="([^"]+)"', page)[0]
    browser.post(
        f"/jobs/{job}/answers",
        data={"csrf": csrf, "key": key, "use": "typed", "value": SPONSORSHIP_ANSWER},
    )
    for tab in ("overview", "cv", "questions", "apply"):
        browser.get(f"/jobs/{job}?tab={tab}&reveal=1")

    logged = caplog.text
    assert caplog.records
    for value in (EMAIL, NEW_EMAIL, "avery.fixed@example.test", SPONSORSHIP_ANSWER, csrf):
        assert value not in logged
