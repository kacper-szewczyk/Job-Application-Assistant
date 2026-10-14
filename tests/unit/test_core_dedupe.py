import pytest

from jaa.core.dedupe import canonical_url, company_key, dedupe_key, fold, title_key


@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        ("https://Example.TEST/jobs/1", "https://example.test/jobs/1"),
        ("https://www.example.test/jobs/1/", "https://example.test/jobs/1"),
        ("https://example.test:443/jobs/1#apply", "https://example.test/jobs/1"),
        ("http://example.test:8080/jobs/1", "http://example.test:8080/jobs/1"),
        (
            "https://example.test/jobs?id=7&utm_source=x&UTM_Campaign=y&gclid=z&ref=feed",
            "https://example.test/jobs?id=7",
        ),
        ("https://example.test/jobs?b=2&a=1", "https://example.test/jobs?a=1&b=2"),
        ("https://example.test/Jobs/ABC", "https://example.test/Jobs/ABC"),
        ("https://example.test", "https://example.test"),
        ("  https://example.test/x  ", "https://example.test/x"),
        ("mailto:jobs@example.test", "mailto:jobs@example.test"),
        ("https://example.test:notaport/x", "https://example.test:notaport/x"),
    ],
)
def test_canonical_url(url: str, canonical: str) -> None:
    assert canonical_url(url) == canonical


def test_fold_drops_case_diacritics_and_punctuation() -> None:
    assert fold("Łódź, Kraków & Gdańsk!") == "lodz krakow gdansk"
    assert fold("Straße/ÆØ") == "strasse aeo"
    assert fold("  ") == ""


@pytest.mark.parametrize(
    ("company", "key"),
    [
        ("Example Widgets Sp. z o.o.", "example widgets"),
        ("EXAMPLE WIDGETS SP. Z O.O.", "example widgets"),
        ("Example Widgets sp. z o.o. sp.k.", "example widgets"),
        ("Przykład S.A.", "przyklad"),
        ("Example GmbH", "example"),
        ("Example, Inc.", "example"),
        # A legal form is only dropped at the end, and a name that is only one stays.
        ("SA Systems", "sa systems"),
        ("Inc", "inc"),
    ],
)
def test_company_key(company: str, key: str) -> None:
    assert company_key(company) == key


def test_title_key_drops_gender_tags() -> None:
    assert title_key("Python Developer (K/M)") == "python developer"
    assert title_key("Python Developer (m/f/d)") == "python developer"
    assert title_key("Senior Python Developer") == "senior python developer"


def test_dedupe_key_is_conservative() -> None:
    a = dedupe_key("Example Sp. z o.o.", "Python Developer (K/M)", "Kraków")
    b = dedupe_key("EXAMPLE sp. z o.o.", "Python developer", "krakow")
    assert a == b == "example | python developer | krakow"
    assert dedupe_key("Example", "Python Developer", "Warszawa") != a
    assert dedupe_key("Example", "Python Developer", None) is None
    assert dedupe_key("Example", "!!!", "Kraków") is None
