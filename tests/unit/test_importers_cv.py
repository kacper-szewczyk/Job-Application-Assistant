import pytest

from jaa.core.models import FactCandidate
from jaa.importers.base import SourceLine
from jaa.importers.cv import ParsedCV, Section, heading_section, parse_cv, slug


def lines(*texts: str, location: str = "page 1") -> list[SourceLine]:
    return [SourceLine(text=t, location=location) for t in texts]


def by_key(parsed: ParsedCV) -> dict[str, FactCandidate]:
    return {c.field_key: c for c in parsed.candidates}


def codes(parsed: ParsedCV) -> list[str]:
    return [i.code for i in parsed.issues]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("C++", "c-plus-plus"),
        ("C#", "c-sharp"),
        ("Node.js", "node-dot-js"),
        (".NET", "dot-net"),
        ("Example Widgets Ltd", "example-widgets-ltd"),
        ("PostgreSQL (replication)", "postgresql"),
        ("Łódź Tech", "lodz-tech"),
        ("???", "item"),
    ],
)
def test_slug(text: str, expected: str) -> None:
    assert slug(text) == expected


def test_heading_section_ignores_case_colons_and_ampersands() -> None:
    assert heading_section("WORK EXPERIENCE:") is Section.EXPERIENCE
    assert heading_section("Licenses & Certifications") is Section.CERTIFICATIONS
    assert heading_section("Languages: Python, Go") is None


def test_header_contact_details() -> None:
    parsed = parse_cv(
        lines(
            "Avery Synthetic",
            "avery@example.test • +1 (555) 010-9999 • linkedin.com/in/avery-synthetic",
            "https://avery.example.test",
            "Exampleton, Nowhere",
        )
    )
    facts = by_key(parsed)
    assert facts["identity.name"].value_normalized == "Avery Synthetic"
    assert facts["contact.email"].value_normalized == "avery@example.test"
    assert facts["contact.phone"].value_normalized == "+15550109999"
    assert facts["contact.links.linkedin"].value_normalized == (
        "https://linkedin.com/in/avery-synthetic"
    )
    assert facts["contact.links.website"].value_normalized == "https://avery.example.test"
    # The address line is not guessed at, but it is reported.
    assert "text_not_imported" in codes(parsed)
    assert "no_sections" in codes(parsed)


def test_curriculum_vitae_title_is_not_a_name() -> None:
    parsed = parse_cv(lines("Curriculum Vitae", "Skills", "Python"))
    assert "identity.name" not in by_key(parsed)


def test_contact_section_does_not_produce_a_name() -> None:
    parsed = parse_cv(lines("Contact", "Email: avery@example.test", "Avery Synthetic"))
    facts = by_key(parsed)
    assert facts["contact.email"].source_location == "page 1 · Contact"
    assert "identity.name" not in facts


def test_date_ranges_are_not_phone_numbers() -> None:
    # Phone numbers are only looked for at the top of the CV, where a date line may also be.
    parsed = parse_cv(lines("Avery Synthetic", "2019 - 2021 2022", "+44 20 7946 0958"))
    assert by_key(parsed)["contact.phone"].value_normalized == "+442079460958"
    assert "2019" not in str(by_key(parsed)["contact.phone"].value_raw)


def test_experience_title_at_employer() -> None:
    parsed = parse_cv(
        lines(
            "Experience",
            "Senior Widget Engineer at Example Widgets Ltd",
            "January 2021 – Present",
            "• Built things.",
        )
    )
    facts = by_key(parsed)
    assert facts["experience.example-widgets-ltd.title"].value_raw == "Senior Widget Engineer"
    assert facts["experience.example-widgets-ltd.employer"].confidence == 0.7
    dates = facts["experience.example-widgets-ltd.dates"]
    assert dates.value_normalized == {"start": "2021-01", "end": None, "current": True}
    assert dates.value_raw == "January 2021 – Present"
    assert facts["experience.example-widgets-ltd.description"].value_normalized == ["Built things."]
    # The excerpt shows the reviewer the original heading lines.
    assert "Senior Widget Engineer at Example Widgets Ltd" in (
        facts["experience.example-widgets-ltd.title"].source_excerpt or ""
    )


@pytest.mark.parametrize(
    ("dates", "expected"),
    [
        ("03/2017 - 12/2020", {"start": "2017-03", "end": "2020-12", "current": False}),
        ("2017-03 – 2020-12", {"start": "2017-03", "end": "2020-12", "current": False}),
        ("Sept. 2017 to Dec 2020", {"start": "2017-09", "end": "2020-12", "current": False}),
        ("2013-2016", {"start": "2013", "end": "2016", "current": False}),
        ("05.2019 – now", {"start": "2019-05", "end": None, "current": True}),
    ],
)
def test_date_formats(dates: str, expected: dict[str, object]) -> None:
    parsed = parse_cv(lines("Experience", f"Example Gadgets | Software Developer | {dates}"))
    assert by_key(parsed)["experience.example-gadgets.dates"].value_normalized == expected


def test_stacked_entries_without_bullets_are_split_at_each_date_line() -> None:
    parsed = parse_cv(
        lines(
            "Work History",
            "Example Gadgets Ltd",
            "Software Developer",
            "2019 – 2021",
            "Example Gizmos Ltd",
            "Junior Developer",
            "2017 – 2019",
        )
    )
    facts = by_key(parsed)
    assert facts["experience.example-gadgets-ltd.title"].value_raw == "Software Developer"
    assert facts["experience.example-gizmos-ltd.title"].value_raw == "Junior Developer"
    assert facts["experience.example-gizmos-ltd.dates"].value_normalized == {
        "start": "2017",
        "end": "2019",
        "current": False,
    }
    assert parsed.issues == ()


def test_two_roles_at_one_employer_get_distinct_keys() -> None:
    parsed = parse_cv(
        lines(
            "Experience",
            "Lead Engineer | Example Co | 2021 - present",
            "• Led.",
            "Engineer | Example Co | 2018 - 2021",
            "• Built.",
        )
    )
    facts = by_key(parsed)
    assert facts["experience.example-co.title"].value_raw == "Lead Engineer"
    assert facts["experience.example-co-2018.title"].value_raw == "Engineer"


def test_ambiguous_entry_heading_is_kept_whole_with_low_confidence() -> None:
    parsed = parse_cv(lines("Experience", "Example Co | Widgets | 2018 - 2021", "• Built."))
    facts = by_key(parsed)
    heading = facts["experience.example-co.heading"]
    assert heading.value_raw == "Example Co | Widgets"
    assert heading.confidence is not None and heading.confidence < 0.5
    assert "experience.example-co.employer" not in facts


def test_leftover_heading_parts_are_reported() -> None:
    parsed = parse_cv(
        lines("Experience", "Software Developer | Example Co | Exampleton | 2018 - 2021")
    )
    assert by_key(parsed)["experience.example-co.employer"].value_raw == "Example Co"
    issue = next(i for i in parsed.issues if i.code == "text_not_imported")
    assert "Exampleton" in issue.message


def test_education_entries() -> None:
    parsed = parse_cv(
        lines(
            "Education",
            "University of Exampleton",
            "MSc Widget Science",
            "2016",
            "• Thesis on synthetic widgets.",
        )
    )
    facts = by_key(parsed)
    assert facts["education.university-of-exampleton.degree"].value_raw == "MSc Widget Science"
    assert facts["education.university-of-exampleton.dates"].value_normalized == {
        "start": None,
        "end": "2016",
        "current": False,
    }
    assert facts["education.university-of-exampleton.details"].value_normalized == [
        "Thesis on synthetic widgets."
    ]


def test_skills_split_on_separators_but_not_inside_parentheses() -> None:
    parsed = parse_cv(
        lines(
            "Technical Skills",
            "Languages: Python, C#, TypeScript",
            "• Kubernetes | Terraform; AWS (EC2, S3)",
        )
    )
    keys = set(by_key(parsed))
    assert keys == {
        "skills.python",
        "skills.c-sharp",
        "skills.typescript",
        "skills.kubernetes",
        "skills.terraform",
        "skills.aws",
    }
    assert by_key(parsed)["skills.aws"].value_raw == "AWS (EC2, S3)"
    assert parsed.issues == ()


def test_sentences_in_skills_are_reported_not_imported() -> None:
    parsed = parse_cv(
        lines("Skills", "Python", "I am very good at explaining complicated things to anyone")
    )
    assert set(by_key(parsed)) == {"skills.python"}
    assert codes(parsed) == ["text_not_imported"]


def test_languages_and_certifications() -> None:
    parsed = parse_cv(
        lines(
            "Languages",
            "English (C2), Brazilian Portuguese – B1",
            "Certifications",
            "Synthetic Cloud Practitioner (2022)",
        )
    )
    facts = by_key(parsed)
    assert facts["languages.english"].value_normalized == {"language": "English", "level": "C2"}
    assert facts["languages.brazilian-portuguese"].value_normalized == {
        "language": "Brazilian Portuguese",
        "level": "B1",
    }
    cert = facts["certifications.synthetic-cloud-practitioner"]
    assert cert.value_normalized == {
        "name": "Synthetic Cloud Practitioner",
        "date": {"start": None, "end": "2022", "current": False},
    }


def test_unsupported_and_unknown_styled_sections_are_reported() -> None:
    parsed = parse_cv(
        [
            SourceLine(text="Avery Synthetic", location="paragraph 1", is_heading=True),
            SourceLine(text="Skills", location="paragraph 2", is_heading=True),
            SourceLine(text="Python", location="paragraph 3"),
            SourceLine(text="Side Quests", location="paragraph 4", is_heading=True),
            SourceLine(text="Built a synthetic robot", location="paragraph 5"),
            SourceLine(text="Projects", location="paragraph 6"),
            SourceLine(text="Widget tracker", location="paragraph 7"),
        ]
    )
    # A styled first line is the name, not an unknown section.
    assert by_key(parsed)["identity.name"].value_raw == "Avery Synthetic"
    # The unknown section's text does not leak into Skills.
    assert set(by_key(parsed)) == {"identity.name", "skills.python"}
    skipped = [i for i in parsed.issues if i.code == "section_not_imported"]
    assert [i.location for i in skipped] == ["paragraph 4", "paragraph 6"]
    assert "“Side Quests”" in skipped[0].message


def test_second_value_for_the_same_key_is_reported() -> None:
    parsed = parse_cv(lines("a@example.test", "b@example.test"))
    assert by_key(parsed)["contact.email"].value_normalized == "a@example.test"
    assert "duplicate_field" in codes(parsed)


def test_long_excerpts_are_truncated() -> None:
    parsed = parse_cv(lines("Summary", "word " * 200))
    excerpt = by_key(parsed)["summary"].source_excerpt or ""
    assert len(excerpt) <= 300
    assert excerpt.endswith("…")


def test_every_candidate_has_provenance() -> None:
    parsed = parse_cv(lines("Avery Synthetic", "Skills", "Python, Go", "Summary", "Synthetic."))
    for candidate in parsed.candidates:
        assert candidate.source_location
        assert candidate.source_excerpt
        assert candidate.confidence is not None and 0 < candidate.confidence <= 1
    assert parsed.sections == {"header": 1, "skills": 2, "summary": 1}


def test_named_entry_fields_are_used_as_given_and_extras_reported() -> None:
    from jaa.importers.base import EntryFields

    parsed = parse_cv(
        [
            SourceLine(text="Experience", location="line 1", is_heading=True),
            SourceLine(
                text="Widget Wrangler | Example Corp | Summer 2019 | Remote",
                location="line 2",
                entry=EntryFields(
                    role="Widget Wrangler",
                    organization="Example Corp",
                    dates="Summer 2019",
                    extra=("Remote",),
                ),
            ),
            SourceLine(text="Wrangled widgets.", location="line 3", is_bullet=True),
            SourceLine(
                text="Example Labs | 2018",
                location="line 4",
                entry=EntryFields(organization="Example Labs", dates="2018"),
            ),
        ]
    )
    facts = by_key(parsed)
    # "Wrangler" is not a title word; the markup says it is the role anyway.
    assert facts["experience.example-corp.title"].value_normalized == "Widget Wrangler"
    assert facts["experience.example-corp.employer"].confidence == 0.8
    assert facts["experience.example-corp.description"].value_normalized == ["Wrangled widgets."]
    dates = facts["experience.example-corp.dates"].value_normalized
    assert isinstance(dates, dict) and dates["end"] == "2019"
    assert facts["experience.example-labs.dates"].value_normalized == {
        "start": None,
        "end": "2018",
        "current": False,
    }
    assert "experience.example-labs.title" not in facts
    unused = [i for i in parsed.issues if i.code == "text_not_imported"]
    assert len(unused) == 1
    assert "2 lines" in unused[0].message  # "Remote" and the unparsed "Summer"


def test_headings_list_every_section() -> None:
    parsed = parse_cv(lines("Avery Synthetic", "Skills", "Python", "Hobbies", "Chess"))
    assert parsed.headings == (("Skills", Section.SKILLS), ("Hobbies", Section.UNSUPPORTED))


# --- Polish (ADR 0031) -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Doświadczenie zawodowe", Section.EXPERIENCE),
        ("DOŚWIADCZENIE ZAWODOWE:", Section.EXPERIENCE),
        ("Doswiadczenie zawodowe", Section.EXPERIENCE),
        ("Wykształcenie", Section.EDUCATION),
        ("Umiejętności", Section.SKILLS),
        ("Języki obce", Section.LANGUAGES),
        ("Certyfikaty", Section.CERTIFICATIONS),
        ("O mnie", Section.SUMMARY),
        ("Dane kontaktowe", Section.CONTACT),
        ("Zainteresowania", Section.UNSUPPORTED),
    ],
)
def test_polish_headings(text: str, expected: Section) -> None:
    assert heading_section(text) is expected


def test_slug_folds_polish_diacritics() -> None:
    assert slug("Zażółć gęślą jaźń") == "zazolc-gesla-jazn"
    assert slug("ĄĆĘŁŃÓŚŹŻ") == "acelnoszz"


@pytest.mark.parametrize(
    ("text", "dates"),
    [
        ("styczeń 2021 – obecnie", {"start": "2021-01", "end": None, "current": True}),
        ("od stycznia 2020 do teraz", {"start": "2020-01", "end": None, "current": True}),
        ("paź. 2019 – do dziś", {"start": "2019-10", "end": None, "current": True}),
        ("wrzesień 2018 - czerwiec 2019", {"start": "2018-09", "end": "2019-06", "current": False}),
        ("Wrz 2018 – Lut 2019", {"start": "2018-09", "end": "2019-02", "current": False}),
        ("01.2020 – aktualnie", {"start": "2020-01", "end": None, "current": True}),
        ("2015 do 2017", {"start": "2015", "end": "2017", "current": False}),
    ],
)
def test_polish_date_ranges(text: str, dates: dict[str, object]) -> None:
    parsed = parse_cv(lines("Doświadczenie", f"Programista, Example Widgets | {text}"))
    facts = by_key(parsed)
    assert facts["experience.example-widgets.dates"].value_normalized == dates
    # The dates are cut out of the heading as written, diacritics and all.
    assert facts["experience.example-widgets.dates"].value_raw == text
    assert facts["experience.example-widgets.employer"].value_raw == "Example Widgets"
    assert facts["experience.example-widgets.title"].value_raw == "Programista"
    assert codes(parsed) == []


def test_polish_role_at_employer() -> None:
    parsed = parse_cv(
        lines(
            "Doświadczenie zawodowe",
            "Starsza programistka w Przykładowe Widżety sp. z o.o.",
            "styczeń 2021 – obecnie",
            "• Prowadziłam migrację potoków.",
        )
    )
    facts = by_key(parsed)
    entry = "experience.przykladowe-widzety-sp-z-o-dot-o"
    assert facts[f"{entry}.title"].value_raw == "Starsza programistka"
    assert facts[f"{entry}.employer"].value_raw == "Przykładowe Widżety sp. z o.o."
    assert facts[f"{entry}.dates"].value_normalized == {
        "start": "2021-01",
        "end": None,
        "current": True,
    }


def test_polish_w_is_not_split_without_a_role() -> None:
    # "w" is also part of names ("Bank Spółdzielczy w Przykładowie"): no role, no split.
    parsed = parse_cv(lines("Doświadczenie", "Bank Spółdzielczy w Przykładowie | 2019 - 2020"))
    facts = by_key(parsed)
    assert "experience.bank-spoldzielczy-w-przykladowie.heading" in facts
    assert not any(k.endswith(".employer") for k in facts)


def test_polish_education_keywords() -> None:
    parsed = parse_cv(
        lines(
            "Wykształcenie",
            "Politechnika Przykładowa | Inżynier, Informatyka | 2012 - 2016",
            "Liceum Ogólnokształcące nr 1 | Matura | 2012",
        )
    )
    facts = by_key(parsed)
    assert facts["education.politechnika-przykladowa.degree"].value_raw == "Inżynier"
    assert facts["education.liceum-ogolnoksztalcace-nr-1.degree"].value_raw == "Matura"
    assert facts["education.liceum-ogolnoksztalcace-nr-1.dates"].value_normalized == {
        "start": None,
        "end": "2012",
        "current": False,
    }


def test_language_keys_are_the_same_in_english_and_polish() -> None:
    polish = by_key(parse_cv(lines("Języki", "Język angielski: C1, niemiecki (B2), polski")))
    english = by_key(parse_cv(lines("Languages", "English: C1, German (B2), Polish")))
    assert (
        set(polish) == set(english) == {"languages.english", "languages.german", "languages.polish"}
    )
    assert polish["languages.english"].value_normalized == {
        "language": "Język angielski",
        "level": "C1",
    }
    # Only a bare language name is mapped; anything more keeps its own key.
    assert "languages.business-english" in by_key(parse_cv(lines("Languages", "Business English")))


@pytest.mark.parametrize("title", ["Życiorys", "CURRICULUM VITAE"])
def test_document_title_is_skipped_and_the_name_still_found(title: str) -> None:
    parsed = parse_cv(lines(title, "Żaneta Przykładowa", "Umiejętności", "Python"))
    assert by_key(parsed)["identity.name"].value_raw == "Żaneta Przykładowa"
    assert codes(parsed) == []


def test_mixed_language_cv_keeps_every_section() -> None:
    parsed = parse_cv(
        lines(
            "Avery Synthetic",
            "Doświadczenie zawodowe",
            "Senior Engineer at Example Widgets Ltd | Jan 2021 – obecnie",
            "Education",
            "Uniwersytet Przykładowy | Magister | 2014 - 2016",
            "Umiejętności",
            "Python, Go",
            "Languages",
            "angielski (C1)",
        )
    )
    facts = by_key(parsed)
    assert facts["experience.example-widgets-ltd.title"].value_raw == "Senior Engineer"
    assert facts["experience.example-widgets-ltd.dates"].value_normalized == {
        "start": "2021-01",
        "end": None,
        "current": True,
    }
    assert facts["education.uniwersytet-przykladowy.degree"].value_raw == "Magister"
    assert {"skills.python", "skills.go", "languages.english"} <= set(facts)
    assert codes(parsed) == []
    assert [section for _, section in parsed.headings] == [
        Section.EXPERIENCE,
        Section.EDUCATION,
        Section.SKILLS,
        Section.LANGUAGES,
    ]


@pytest.mark.parametrize(
    "clause",
    [
        ("Wyrażam zgodę na przetwarzanie moich danych osobowych w celu rekrutacji.",),
        ("Klauzula informacyjna", "Zgodnie z art. 6 ust. 1 lit. a RODO wyrażam zgodę."),
        ("I hereby consent to the processing of my personal data for recruitment.",),
    ],
)
def test_consent_clause_is_reported_not_imported(clause: tuple[str, ...]) -> None:
    parsed = parse_cv(lines("Umiejętności", "Python, Go", *clause))
    facts = by_key(parsed)
    assert set(facts) == {"skills.python", "skills.go"}
    assert codes(parsed) == ["consent_clause_not_imported"]
    assert parsed.headings == (("Umiejętności", Section.SKILLS),)


def test_consent_clause_alone_is_not_a_section() -> None:
    parsed = parse_cv(lines("Avery Synthetic", "Wyrażam zgodę na przetwarzanie danych."))
    assert "no_sections" in codes(parsed)
