from jaa.core.job_details import JobDetails, ListedSkill, SalaryRange, WorkArrangement

DETAILS = JobDetails(
    skills=(ListedSkill(name="Python", level="Advanced"), ListedSkill(name="SQL")),
    languages=(ListedSkill(name="Angielski", level="B2"),),
    experience_level="Regular",
    work_arrangement=WorkArrangement.HYBRID,
    salaries=(SalaryRange(min=15000, max=21000, currency="PLN", period="month", contract="B2B"),),
)


def test_round_trip() -> None:
    assert JobDetails.from_json(DETAILS.to_json()) == DETAILS


def test_empty_details_are_an_empty_object() -> None:
    assert JobDetails().to_json() == {}
    assert JobDetails().is_empty()
    assert JobDetails.from_json({}) == JobDetails()


def test_malformed_values_are_skipped() -> None:
    details = JobDetails.from_json(
        {
            "skills": [{"name": " "}, "Python", {"name": "Go", "level": 3}],
            "languages": "English",
            "experience_level": 7,
            "work_arrangement": "moon",
            "salaries": [
                {"min": None, "max": None, "currency": "PLN", "period": "month"},
                {"min": 100, "currency": "pln", "period": "hour"},
                {"min": -5, "max": 10, "currency": "EUR"},
            ],
        }
    )
    assert details == JobDetails(
        skills=(ListedSkill(name="Go"),),
        salaries=(SalaryRange(min=100, currency="PLN", period="hour"),),
    )
