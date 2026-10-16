import io
import logging
from urllib.parse import parse_qs, urlsplit

import pytest

from jaa.connectors.http import HttpResponse, TransportError
from jaa.core.errors import ImportSourceError
from jaa.core.models import FactCandidate, ImportSourceType, ImportStatus, JsonValue
from jaa.importers import linkedin_api as api
from jaa.importers.linkedin import CsvFile, parse_linkedin_files
from jaa.logging import Sensitive, setup_logging
from tests.fixtures.linkedin_api import TOKEN, FakeSnapshotApi, page_body
from tests.fixtures.linkedin_builders import LI_CAUSE, SAMPLE_FILES

ADDRESS = "1 Synthetic Lane, Exampleton"
PHONE = "+44 20 7946 0958"


def _token() -> Sensitive[str]:
    return api.parse_token(TOKEN)


def test_token_is_trimmed_and_a_bearer_prefix_dropped() -> None:
    assert api.parse_token(f"  Bearer {TOKEN}\n").reveal() == TOKEN


@pytest.mark.parametrize("pasted", ["", "short", f"{TOKEN} extra", f"{TOKEN}\nX-Header: 1"])
def test_unusable_token_is_refused_without_repeating_it(pasted: str) -> None:
    with pytest.raises(ImportSourceError) as excinfo:
        api.parse_token(pasted)
    assert not pasted.strip() or pasted.strip() not in str(excinfo.value)


def test_requests_only_the_mapped_domains_one_at_a_time() -> None:
    fake = FakeSnapshotApi()
    snapshot = api.fetch_snapshot(_token(), fake)
    domains = [parse_qs(urlsplit(r.url).query)["domain"][0] for r in fake.sent]
    assert set(domains) == set(api.DOMAINS)
    for request in fake.sent:
        parts = urlsplit(request.url)
        assert (parts.scheme, parts.hostname, parts.path) == (
            "https",
            "api.linkedin.com",
            "/rest/memberSnapshotData",
        )
        assert parse_qs(parts.query)["q"] == ["criteria"]
        assert request.headers["Linkedin-Version"] == "202312"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert {"INBOX", "CONNECTIONS", "MESSAGES"}.isdisjoint(domains)
    assert snapshot.requests == len(fake.sent)


def test_pages_until_linkedin_reports_no_more_data() -> None:
    fake = FakeSnapshotApi()
    snapshot = api.fetch_snapshot(_token(), fake, ["POSITIONS"])
    starts = [parse_qs(urlsplit(r.url).query)["start"][0] for r in fake.sent]
    assert starts == ["0", "1", "2"]
    positions = snapshot.get("POSITIONS")
    assert positions is not None and positions.pages == 2 and len(positions.rows) == 2


def test_domain_without_data_is_empty_not_an_error() -> None:
    snapshot = api.fetch_snapshot(_token(), FakeSnapshotApi(), ["CERTIFICATIONS"])
    certifications = snapshot.get("CERTIFICATIONS")
    assert certifications is not None and certifications.rows == () and certifications.pages == 0


def test_page_cap_stops_a_runaway_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "MAX_PAGES_PER_DOMAIN", 3)
    fake = FakeSnapshotApi(pages={"SKILLS": [[{"Name": f"Skill {i}"}] for i in range(10)]})
    snapshot = api.fetch_snapshot(_token(), fake, ["SKILLS"])
    skills = snapshot.get("SKILLS")
    assert skills is not None and skills.truncated and len(fake.sent) == 3
    prepared = api.prepare_linkedin_api_import(snapshot)
    assert "too_many_pages" in {i.code for i in prepared.issues}


def _refused(status: int, body: bytes = b"{}") -> FakeSnapshotApi:
    url = "https://api.linkedin.com/rest/memberSnapshotData"
    return FakeSnapshotApi(
        overrides={("PROFILE", 0): HttpResponse(status=status, url=url, body=body)}
    )


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "expired"), (403, "Member Data Portability"), (426, "version"), (429, "later")],
)
def test_refusals_stop_the_import_and_never_name_the_token(status: int, message: str) -> None:
    with pytest.raises(ImportSourceError, match=message) as excinfo:
        api.fetch_snapshot(_token(), _refused(status))
    assert TOKEN not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_other_404_is_not_taken_for_no_data() -> None:
    with pytest.raises(ImportSourceError, match="HTTP 404"):
        api.fetch_snapshot(_token(), _refused(404, b'{"message":"Not found"}'))


def test_redirect_to_another_host_is_refused() -> None:
    fake = FakeSnapshotApi(
        overrides={("PROFILE", 0): HttpResponse(status=302, url="https://www.linkedin.com/login")}
    )
    with pytest.raises(ImportSourceError, match="redirected"):
        api.fetch_snapshot(_token(), fake)


def test_unexpected_payload_is_refused() -> None:
    url = "https://api.linkedin.com/rest/memberSnapshotData"
    for body in (b"<html>", b'{"elements": 3}', page_body("INBOX", 0, [])):
        fake = FakeSnapshotApi(
            overrides={("PROFILE", 0): HttpResponse(status=200, url=url, body=body)}
        )
        with pytest.raises(ImportSourceError):
            api.fetch_snapshot(_token(), fake)


def test_transport_failure_does_not_chain_the_request() -> None:
    class Failing:
        def send(self, request: object, *, timeout: float, max_bytes: int) -> HttpResponse:
            raise TransportError("request failed: TimeoutError")

    with pytest.raises(ImportSourceError, match="could not reach LinkedIn") as excinfo:
        api.fetch_snapshot(_token(), Failing())
    assert excinfo.value.__cause__ is None


def test_profile_domains_become_reviewable_facts_with_archive_keys() -> None:
    prepared = api.prepare_linkedin_api_import(api.fetch_snapshot(_token(), FakeSnapshotApi()))
    keys = {c.field_key for c in prepared.candidates}
    assert {
        "identity.name",
        "headline",
        "summary",
        "contact.email",
        "contact.location",
        "experience.example-analytics.employer",
        "experience.example-analytics.dates",
        "experience.sample-systems.title",
        "skills.python",
        "languages.polish",
    } <= keys
    by_key = {c.field_key: c for c in prepared.candidates}
    assert by_key["experience.example-analytics.dates"].value_normalized == {
        "start": "2021-03",
        "end": None,
        "current": True,
    }
    assert by_key["skills.python"].source_location == "LinkedIn SKILLS row 1"
    run = prepared.run
    assert run.source_type is ImportSourceType.LINKEDIN_API
    assert run.source_path is None
    assert run.status is ImportStatus.PARTIAL  # Profile.csv-style left-out columns
    assert set(prepared.empty_domains) == {"PHONE_NUMBERS", "EDUCATION", "CERTIFICATIONS"}


def test_more_sections_map_to_the_same_keys_as_the_archive() -> None:
    sections = ("projects", "volunteering", "publications", "patents", "honors", "courses")
    prepared = api.prepare_linkedin_api_import(api.fetch_snapshot(_token(), FakeSnapshotApi()))
    archive = parse_linkedin_files([CsvFile(name=n, data=d) for n, d in SAMPLE_FILES.items()])

    def of(candidates: tuple[FactCandidate, ...]) -> dict[str, JsonValue]:
        return {
            c.field_key: c.value_normalized
            for c in candidates
            if c.field_key.split(".", 1)[0] in sections
        }

    from_api = of(prepared.candidates)
    assert {k.split(".", 1)[0] for k in from_api} == set(sections)
    assert from_api == of(archive.candidates)
    by_key = {c.field_key: c for c in prepared.candidates}
    assert by_key["projects.widget-telemetry-toolkit.title"].source_location == (
        "LinkedIn PROJECTS row 1"
    )
    assert by_key["projects.synthetic-side-project.title"].source_location == (
        "LinkedIn PROJECTS row 2"  # the second page
    )
    assert LI_CAUSE not in repr(list(from_api.values()))
    counts = prepared.run.summary["sections"]
    assert isinstance(counts, dict)
    assert {s: counts.get(s) for s in sections} == {
        "projects": 5,
        "volunteering": 4,
        "publications": 5,
        "patents": 4,
        "honors": 3,
        "courses": 2,
    }


def test_data_minimization_matches_the_archive_import() -> None:
    prepared = api.prepare_linkedin_api_import(api.fetch_snapshot(_token(), FakeSnapshotApi()))
    text = repr([(c.value_raw, c.value_normalized) for c in prepared.candidates])
    assert ADDRESS not in text and "1990" not in text
    labels = {label for label, _ in prepared.seeker_preferences}
    assert labels == {"job titles", "locations", "preferred job types", "open to recruiters"}
    assert PHONE not in repr(prepared.seeker_preferences)
    assert ADDRESS not in repr(prepared.seeker_preferences)


def test_job_domains_are_returned_for_the_user() -> None:
    prepared = api.prepare_linkedin_api_import(api.fetch_snapshot(_token(), FakeSnapshotApi()))
    assert [(j.title, j.company, j.url) for j in prepared.saved_jobs] == [
        ("Data Engineer", "Fictional Freight", "https://www.linkedin.com/jobs/view/1000000001"),
        ("Platform Engineer", "Imaginary Insurance", None),
    ]
    assert [(j.title, j.company) for j in prepared.applications] == [
        ("Data Engineer", "Fictional Freight")
    ]
    assert "Are you authorized" not in repr(prepared)
    assert len(prepared.job_alerts) == 1
    assert prepared.run.summary["saved_jobs"] == 2


def test_no_profile_data_yet_fails_with_a_hint() -> None:
    prepared = api.prepare_linkedin_api_import(
        api.fetch_snapshot(_token(), FakeSnapshotApi(pages={}))
    )
    assert prepared.run.status is ImportStatus.FAILED
    assert any("still be preparing" in i.message for i in prepared.issues)


def test_same_data_has_the_same_hash() -> None:
    first = api.prepare_linkedin_api_import(api.fetch_snapshot(_token(), FakeSnapshotApi()))
    second = api.prepare_linkedin_api_import(api.fetch_snapshot(_token(), FakeSnapshotApi()))
    assert first.run.source_sha256 == second.run.source_sha256


def test_logs_carry_counts_not_the_token_or_values() -> None:
    stream = io.StringIO()
    setup_logging("DEBUG", stream)
    try:
        prepared = api.prepare_linkedin_api_import(api.fetch_snapshot(_token(), FakeSnapshotApi()))
    finally:
        logging.getLogger("jaa").handlers.clear()
    logs = stream.getvalue()
    assert "domain=POSITIONS pages=2 rows=2" in logs
    for value in (TOKEN, "Avery", "Example Analytics", "Fictional Freight"):
        assert value not in logs
    assert prepared.candidates
