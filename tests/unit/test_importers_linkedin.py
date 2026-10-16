import logging
import zipfile
from pathlib import Path

import pytest

from jaa.core import models as m
from jaa.core.errors import ImportSourceError, UnsafeArchiveError
from jaa.importers.archive import MAX_MEMBER_BYTES, list_members, read_member, safe_member_name
from jaa.importers.cv import parse_cv
from jaa.importers.linkedin import (
    CsvFile,
    delete_sources,
    parse_date,
    parse_linkedin_files,
    prepare_linkedin_import,
)
from jaa.importers.text import text_lines
from tests.fixtures.cv_builders import SAMPLE_CV
from tests.fixtures.linkedin_builders import (
    LI_ADDRESS,
    LI_BIRTH_DATE,
    LI_CAUSE,
    LI_EMAIL,
    LI_MESSAGE,
    LI_OTHER_EMAIL,
    LI_PHONE,
    SAMPLE_FILES,
    build_zip,
    csv_bytes,
    sample_archive,
)

KEEP = m.RetentionChoice.KEEP


def parsed_sample() -> dict[str, m.FactCandidate]:
    files = [CsvFile(name=n, data=d) for n, d in SAMPLE_FILES.items()]
    return {c.field_key: c for c in parse_linkedin_files(files).candidates}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Jan 2021", "2021-01"),
        ("September 2019", "2019-09"),
        ("Sept 2019", "2019-09"),
        ("Mar 15, 2022", "2022-03"),
        ("2016", "2016"),
        ("03/2017", "2017-03"),
        ("13/2017", None),
        ("Janu 2021", None),
        ("soon", None),
    ],
)
def test_parse_date(text: str, expected: str | None) -> None:
    assert parse_date(text) == expected


def test_sample_archive_maps_to_cv_style_keys() -> None:
    facts = parsed_sample()
    assert facts["identity.name"].value_normalized == "Avery Synthetic"
    assert facts["headline"].value_raw == "Backend engineer for widget platforms"
    assert facts["summary"].value_normalized == (
        "I build reliable widget platforms. And mentor engineers."
    )
    assert facts["contact.location"].value_raw == "Exampleton, United Kingdom"
    assert facts["contact.links.website"].value_normalized == "https://avery.example.test"
    assert facts["contact.links.github"].value_raw == "https://github.com/avery-synthetic"
    # The primary address, not the first row.
    assert facts["contact.email"].value_normalized == LI_EMAIL.lower()
    assert facts["contact.phone"].value_normalized == "+442079460958"

    assert facts["experience.example-widgets-ltd.title"].value_raw == "Senior Widget Engineer"
    assert facts["experience.example-widgets-ltd.dates"].value_normalized == {
        "start": "2021-01",
        "end": None,
        "current": True,
    }
    assert facts["experience.example-widgets-ltd.description"].value_normalized == [
        "Led the migration of widget pipelines to event streaming.",
        "Mentored four engineers.",
    ]
    assert facts["experience.example-widgets-ltd.location"].value_raw == "Exampleton"
    # A second position at the same employer is keyed by its start date.
    assert facts["experience.example-gadgets.title"].value_raw == "Software Developer"
    assert facts["experience.example-gadgets-2016-06.title"].value_raw == "Intern"

    assert facts["education.university-of-exampleton.degree"].value_raw == "BSc Computer Science"
    assert facts["education.university-of-exampleton.dates"].value_normalized == {
        "start": "2013",
        "end": "2016",
        "current": False,
    }
    assert facts["education.university-of-exampleton.activities"].value_normalized == ["Chess club"]
    assert {k for k in facts if k.startswith("skills.")} == {
        "skills.python",
        "skills.go",
        "skills.c-plus-plus",
    }
    assert facts["languages.english"].value_normalized == {
        "language": "English",
        "level": "Native or bilingual proficiency",
    }
    assert facts["languages.german"].value_normalized == {"language": "German", "level": None}
    assert facts["certifications.certified-widget-architect"].value_normalized == {
        "name": "Certified Widget Architect",
        "date": {"start": "2022-03", "end": None, "current": False},
        "authority": "Widget Institute",
    }


def test_more_profile_sections_become_entries_with_stable_keys() -> None:
    facts = parsed_sample()
    project = "projects.widget-telemetry-toolkit"
    assert facts[f"{project}.title"].value_raw == "Widget Telemetry Toolkit"
    assert facts[f"{project}.dates"].value_normalized == {
        "start": "2022-02",
        "end": "2022-11",
        "current": False,
    }
    assert facts[f"{project}.url"].value_normalized == "https://example.test/widget-telemetry"
    assert facts[f"{project}.description"].value_normalized == [
        "Open-source collector for widget metrics.",
        "Used by three example teams.",
    ]
    assert facts[f"{project}.description"].source_location == "Projects.csv row 1"
    # A project with only a title is still an entry; nothing else is made up for it.
    assert [k for k in facts if k.startswith("projects.synthetic-side-project")] == [
        "projects.synthetic-side-project.title"
    ]

    club = "volunteering.example-code-club"
    assert facts[f"{club}.organization"].value_raw == "Example Code Club"
    assert facts[f"{club}.role"].value_raw == "Mentor"
    # An open end means ongoing, as for positions.
    assert facts[f"{club}.dates"].value_normalized == {
        "start": "2019-09",
        "end": None,
        "current": True,
    }
    assert facts[f"{club}.description"].value_normalized == ["Weekly coding sessions."]

    paper = "publications.streaming-widgets-at-scale"
    assert facts[f"{paper}.title"].value_raw == "Streaming Widgets at Scale"
    assert facts[f"{paper}.publisher"].value_raw == "Example Press"
    assert facts[f"{paper}.date"].value_raw == "Jun 2023"
    assert facts[f"{paper}.date"].value_normalized == "2023-06"
    assert facts[f"{paper}.url"].value_normalized == "https://example.test/papers/widgets"

    patent = "patents.self-balancing-widget"
    assert facts[f"{patent}.number"].value_raw == "EX-0000001"
    assert facts[f"{patent}.application_number"].value_raw == "EX-APP-01"
    assert facts[f"{patent}.date"].value_normalized == "2020-04"
    assert f"{patent}.description" not in facts

    award = "honors.example-widget-award"
    assert facts[f"{award}.title"].value_raw == "Example Widget Award"
    assert facts[f"{award}.date"].value_normalized == "2021-12"
    assert facts[f"{award}.description"].value_normalized == ["For reliability work."]

    assert facts["courses.distributed-systems"].value_raw == "Distributed Systems (CS-401)"
    assert facts["courses.distributed-systems"].value_normalized == {
        "name": "Distributed Systems",
        "number": "CS-401",
    }
    assert facts["courses.go"].value_normalized == {"name": "Go", "number": None}


def test_volunteering_cause_is_left_out_by_name_only() -> None:
    parsed = parse_linkedin_files([CsvFile(name=n, data=d) for n, d in SAMPLE_FILES.items()])
    issue = next(
        i
        for i in parsed.issues
        if (i.code, i.location) == ("fields_not_imported", "Volunteering.csv")
    )
    assert "cause" in issue.message
    assert LI_CAUSE not in issue.message
    assert all(LI_CAUSE not in repr((c.value_raw, c.value_normalized)) for c in parsed.candidates)


def test_repeated_entries_and_unknown_single_dates() -> None:
    parsed = parse_linkedin_files(
        [
            CsvFile(
                name="Honors.csv",
                data=csv_bytes(
                    ["Title", "Description", "Issued On"],
                    [["Best Widget", "", "2019"], ["Best Widget", "", "2021"], ["Prize", "", "x"]],
                ),
            ),
            CsvFile(
                name="Projects.csv",
                data=csv_bytes(["Title", "Started On", "Finished On"], [["", "2020", ""]]),
            ),
        ]
    )
    keys = {c.field_key for c in parsed.candidates}
    assert {"honors.best-widget.date", "honors.best-widget-2021.date", "honors.prize.title"} <= keys
    assert "honors.prize.date" not in keys
    codes = [(i.code, i.location) for i in parsed.issues]
    assert ("date_not_recognized", "Honors.csv row 3") in codes
    assert ("missing_value", "Projects.csv row 1") in codes
    assert parsed.sections == {"honors": 5}


def test_provenance_names_the_file_and_row_and_keeps_the_raw_value() -> None:
    facts = parsed_sample()
    title = facts["experience.example-gadgets.title"]
    assert title.source_location == "Positions.csv row 2"
    assert title.source_excerpt == "Software Developer"
    assert title.confidence == 0.9
    assert facts["experience.example-widgets-ltd.dates"].value_raw == "Jan 2021 – Present"


def test_keys_match_the_cv_importer_for_the_same_cv() -> None:
    pasted = "\n".join(f"• {text}" if bullet else text for text, _, bullet in SAMPLE_CV)
    from_cv = {c.field_key: c for c in parse_cv(text_lines(pasted)).candidates}
    from_linkedin = parsed_sample()
    shared = {
        "identity.name",
        "contact.email",
        "experience.example-widgets-ltd.title",
        "experience.example-widgets-ltd.dates",
        "experience.example-gadgets.employer",
        "education.university-of-exampleton.degree",
        "skills.python",
        "skills.c-plus-plus",
        "languages.english",
    }
    assert shared <= from_cv.keys() & from_linkedin.keys()
    for key in ("experience.example-widgets-ltd.dates", "skills.python", "contact.email"):
        assert from_cv[key].value_normalized == from_linkedin[key].value_normalized


def test_unknown_files_are_skipped_and_unimported_sections_reported() -> None:
    files = [CsvFile(name=n, data=d) for n, d in SAMPLE_FILES.items()]
    parsed = parse_linkedin_files(files)
    assert set(parsed.skipped_files) == {"messages.csv", "Connections.csv"}
    assert "Positions.csv" in parsed.imported_files
    codes = [(i.code, i.location) for i in parsed.issues]
    assert ("section_not_imported", "Organizations.csv") in codes


def test_unused_personal_fields_are_named_but_their_values_never_appear() -> None:
    files = [CsvFile(name=n, data=d) for n, d in SAMPLE_FILES.items()]
    parsed = parse_linkedin_files(files)
    messages = " ".join(i.message for i in parsed.issues)
    assert "address" in messages
    assert "birth date" in messages
    assert "1 other email address" in messages
    for value in (LI_ADDRESS, LI_BIRTH_DATE, LI_OTHER_EMAIL, LI_MESSAGE):
        assert value not in messages
        assert all(value not in c.value_raw for c in parsed.candidates)


def test_changed_columns_and_bad_encoding_are_reported_not_guessed() -> None:
    parsed = parse_linkedin_files(
        [
            CsvFile(name="Positions.csv", data=csv_bytes(["Employer", "Role"], [["A", "B"]])),
            CsvFile(name="Skills.csv", data="Name\nPython\xff".encode("latin-1")),
        ]
    )
    assert parsed.candidates == ()
    assert [(i.code, i.severity) for i in parsed.issues] == [
        ("unexpected_format", "warning"),
        ("unreadable", "error"),
    ]


def test_unrecognized_dates_are_left_out_with_a_warning() -> None:
    data = csv_bytes(
        ["Company Name", "Title", "Started On", "Finished On"],
        [["Example Co", "Engineer", "sometime", ""]],
    )
    parsed = parse_linkedin_files([CsvFile(name="Positions.csv", data=data)])
    keys = {c.field_key for c in parsed.candidates}
    assert keys == {"experience.example-co.employer", "experience.example-co.title"}
    assert [i.code for i in parsed.issues] == ["date_not_recognized"]


# --- archives and files ------------------------------------------------------------------------


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_prepare_archive_in_a_subfolder(tmp_path: Path) -> None:
    archive = write(tmp_path, "export.zip", sample_archive(prefix="Basic_LinkedInDataExport/"))
    prepared = prepare_linkedin_import([archive], retention=KEEP)
    run = prepared.run
    assert run.source_type is m.ImportSourceType.LINKEDIN_ARCHIVE
    assert run.status is m.ImportStatus.PARTIAL
    assert run.source_path == str(archive.resolve())
    assert run.retention is KEEP
    assert run.summary["archive_members"] == len(SAMPLE_FILES)
    assert prepared.skipped_files == (
        "Basic_LinkedInDataExport/Connections.csv",
        "Basic_LinkedInDataExport/messages.csv",
    )
    assert len(prepared.candidates) == run.summary["candidates"]
    assert prepared.candidates[0].source_location == ("Basic_LinkedInDataExport/Profile.csv row 1")


def test_prepare_csv_files(tmp_path: Path) -> None:
    skills = write(tmp_path, "Skills.csv", SAMPLE_FILES["Skills.csv"])
    langs = write(tmp_path, "Languages.csv", SAMPLE_FILES["Languages.csv"])
    prepared = prepare_linkedin_import([skills, langs], retention=KEEP)
    assert prepared.run.status is m.ImportStatus.SUCCEEDED
    assert prepared.run.source_path == str(tmp_path.resolve())
    assert len(prepared.candidates) == 5
    assert len(prepared.sources) == 2


def test_archive_without_profile_files_fails(tmp_path: Path) -> None:
    archive = write(tmp_path, "other.zip", build_zip({"notes.txt": b"hello"}))
    prepared = prepare_linkedin_import([archive], retention=KEEP)
    assert prepared.run.status is m.ImportStatus.FAILED
    assert prepared.skipped_files == ("notes.txt",)
    assert [i.code for i in prepared.issues] == ["no_facts"]


@pytest.mark.parametrize(
    "paths",
    [
        [("not-linkedin.pdf", b"%PDF-1.4")],
        [("export.zip", b"PK\x03\x04garbage")],
    ],
)
def test_unsupported_sources_are_refused(tmp_path: Path, paths: list[tuple[str, bytes]]) -> None:
    with pytest.raises(ImportSourceError):
        prepare_linkedin_import([write(tmp_path, n, d) for n, d in paths], retention=KEEP)


def test_archive_and_other_files_cannot_be_mixed(tmp_path: Path) -> None:
    archive = write(tmp_path, "export.zip", sample_archive())
    skills = write(tmp_path, "Skills.csv", SAMPLE_FILES["Skills.csv"])
    with pytest.raises(ImportSourceError, match="one LinkedIn archive at a time"):
        prepare_linkedin_import([archive, skills], retention=KEEP)


@pytest.mark.parametrize(
    "name",
    [
        "../Positions.csv",
        "export/../../Positions.csv",
        "/etc/Positions.csv",
        "C:/Positions.csv",
        "export\\..\\Positions.csv",
    ],
)
def test_malicious_member_paths_reject_the_whole_archive(tmp_path: Path, name: str) -> None:
    files = {**SAMPLE_FILES, name: SAMPLE_FILES["Positions.csv"]}
    (tmp_path / "in").mkdir()
    archive = write(tmp_path / "in", "evil.zip", build_zip(files))
    with pytest.raises(UnsafeArchiveError):
        prepare_linkedin_import([archive], retention=KEEP)
    # Nothing was extracted anywhere near the archive.
    assert [p.name for p in tmp_path.rglob("*")] == ["in", "evil.zip"]


def test_symlink_members_reject_the_archive(tmp_path: Path) -> None:
    archive = write(
        tmp_path, "evil.zip", build_zip(SAMPLE_FILES, symlinks={"Skills2.csv": "/etc/passwd"})
    )
    with pytest.raises(UnsafeArchiveError, match="symbolic link"):
        prepare_linkedin_import([archive], retention=KEEP)


def test_oversized_and_bomb_members_are_refused(tmp_path: Path) -> None:
    bomb = write(tmp_path, "bomb.zip", build_zip({"Skills.csv": b"Name\n" + b"a" * 4_000_000}))
    with pytest.raises(UnsafeArchiveError, match="ZIP bomb"):
        prepare_linkedin_import([bomb], retention=KEEP)
    big = write(
        tmp_path,
        "big.zip",
        build_zip({"Skills.csv": b"Name\n" + b"a" * (MAX_MEMBER_BYTES + 1)}),
    )
    with pytest.raises(UnsafeArchiveError, match="larger than"):
        prepare_linkedin_import([big], retention=KEEP)


def test_declared_sizes_are_not_trusted(tmp_path: Path) -> None:
    """A member whose header understates its size is still cut off at the limit."""
    path = write(tmp_path, "lying.zip", build_zip({"Skills.csv": b"Name\n" + b"x" * 64}))
    with zipfile.ZipFile(path) as zf:
        (member,) = list_members(zf)
        with pytest.raises(UnsafeArchiveError, match="larger than"):
            read_member(zf, member, limit=16)


def test_safe_member_name_accepts_normal_paths() -> None:
    assert str(safe_member_name("Export/Positions.csv")) == "Export/Positions.csv"


# --- deletion ----------------------------------------------------------------------------------


def test_delete_sources_removes_unchanged_files(tmp_path: Path) -> None:
    archive = write(tmp_path, "export.zip", sample_archive())
    prepared = prepare_linkedin_import([archive], retention=m.RetentionChoice.DELETE_AFTER_IMPORT)
    delete_sources(prepared.sources)
    assert not archive.exists()


def test_delete_sources_refuses_changed_files_and_deletes_nothing(tmp_path: Path) -> None:
    skills = write(tmp_path, "Skills.csv", SAMPLE_FILES["Skills.csv"])
    langs = write(tmp_path, "Languages.csv", SAMPLE_FILES["Languages.csv"])
    prepared = prepare_linkedin_import([skills, langs], retention=KEEP)
    langs.write_bytes(SAMPLE_FILES["Languages.csv"] + b"Polish,\n")
    with pytest.raises(ImportSourceError, match="changed since it was imported"):
        delete_sources(prepared.sources)
    assert skills.exists() and langs.exists()


def test_prepare_logs_counts_only(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    archive = write(tmp_path, "export.zip", sample_archive())
    with caplog.at_level(logging.DEBUG, logger="jaa"):
        prepared = prepare_linkedin_import([archive], retention=KEEP)
        delete_sources(prepared.sources)
    assert "candidates=" in caplog.text
    for value in (LI_EMAIL, LI_PHONE, "Senior Widget Engineer", "Avery", LI_MESSAGE):
        assert value not in caplog.text
