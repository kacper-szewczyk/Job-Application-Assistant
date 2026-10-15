"""Synthetic CV and cover letter content for template previews (ADR 0029).

Nothing here describes a real person: previews must never use the user's profile, so a template
can be tried before any facts are confirmed and preview files carry no personal data. The text
has Polish diacritics and LaTeX special characters, so a preview also shows that a template
handles them.
"""

from datetime import date

from jaa.documents.content import (
    CvCertification,
    CvContent,
    CvEntry,
    CvLanguage,
    CvLanguageSkill,
)
from jaa.documents.letter import LetterContent

_HEADLINE = {CvLanguage.EN: "Senior Data Engineer", CvLanguage.PL: "Starsza inżynierka danych"}
_SUMMARY = {
    CvLanguage.EN: "Builds reliable data pipelines & reporting for 50% less cost.",
    CvLanguage.PL: "Buduje niezawodne potoki danych i raporty o 50% taniej.",
}
_LEVELS = {CvLanguage.EN: ("native", "C1"), CvLanguage.PL: ("ojczysty", "C1")}
_PARAGRAPHS = {
    CvLanguage.EN: (
        "I am applying for the Data Engineer role at Przykład Sp. z o.o.",
        "At Example Analytics I moved nightly reports to streaming pipelines.",
    ),
    CvLanguage.PL: (
        "Zgłaszam się na stanowisko inżyniera danych w Przykład Sp. z o.o.",
        "W Example Analytics przeniosłam nocne raporty na potoki strumieniowe.",
    ),
}


def sample_cv(language: CvLanguage) -> CvContent:
    """A one-page synthetic CV with every section a template can show."""
    native, c1 = _LEVELS[language]
    return CvContent(
        name="Zofia Przykładowa-Test",
        headline=_HEADLINE[language],
        email="zofia@example.test",
        phone="+48 500 000 000",
        location="Gdańsk, Poland",
        links=(("github", "https://github.com/example-sample"),),
        summary=_SUMMARY[language],
        experience=(
            CvEntry(
                entry_id="example-analytics",
                role="Senior Data Engineer",
                organization="Example Analytics",
                dates={"start": "2021-03", "end": None, "current": True},
                location="Gdańsk",
                items=(
                    "Moved nightly reports to streaming pipelines.",
                    "Cut warehouse costs by 50% with partitioned storage.",
                ),
            ),
            CvEntry(
                entry_id="przyklad",
                role="Data Engineer",
                organization="Przykład Sp. z o.o.",
                dates={"start": "2017-09", "end": "2021-02"},
                location="Łódź",
                items=("Built ETL jobs in Python and SQL.",),
            ),
        ),
        education=(
            CvEntry(
                entry_id="sample-university",
                role="MSc Computer Science",
                organization="Sample University of Technology",
                dates={"start": "2012", "end": "2017"},
            ),
        ),
        skills=("Python", "SQL", "Apache Kafka", "C#"),
        skill_keys=("skills.python", "skills.sql", "skills.apache-kafka", "skills.c"),
        languages=(
            CvLanguageSkill(name="Polski" if language is CvLanguage.PL else "Polish", level=native),
            CvLanguageSkill(name="English" if language is CvLanguage.EN else "Angielski", level=c1),
        ),
        certifications=(
            CvCertification(name="Sample Cloud Data Engineer", date="2022-05", authority="Example"),
        ),
        certification_keys=("certifications.sample-cloud-data-engineer",),
    )


def sample_letter(language: CvLanguage) -> LetterContent:
    """A synthetic cover letter with the sample CV's sender."""
    return LetterContent(
        sender=sample_cv(language),
        recipient=("Przykład Sp. z o.o.", "ul. Testowa 1", "00-001 Warszawa"),
        date=date(2026, 1, 15),
        paragraphs=_PARAGRAPHS[language],
    )
