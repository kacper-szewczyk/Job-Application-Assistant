"""Question topics: which application questions mean the same thing (ADR 0017).

Pure. A topic gives a question a stable `question_key` across employers and wordings, says
whether its answers need explicit user input every time (work authorization, sponsorship,
salary, demographics, criminal or medical disclosures, consent, legal attestations), and which
profile facts or preferences can suggest an answer. Patterns match folded text (`fold`), so
they are lowercase ASCII and cover English and Polish wordings.

A question that matches no topic gets a `custom.` key derived from its folded wording, so only
the same question asked again reuses its answer.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Final

from jaa.core.dedupe import fold
from jaa.core.sensitivity import is_sensitive_key


@dataclass(frozen=True, slots=True, kw_only=True)
class Topic:
    key: str
    label: str
    pattern: re.Pattern[str]
    # Answers are never used without the user's explicit input, unless the user configured a
    # safe reusable answer for this exact question (SPEC §4 step 6, §6).
    explicit: bool = False
    # Confirmed profile facts that answer the question directly, in order of preference.
    fact_keys: tuple[str, ...] = ()
    # A preference that can suggest an answer; mapping it to a question is an inference.
    preference_key: str | None = None


def _words(*alternatives: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(alternatives) + r")\b")


# Explicit topics come first: a question that mentions both a sensitive and an ordinary topic
# ("salary and start date") is treated as sensitive.
TOPICS: Final[tuple[Topic, ...]] = (
    Topic(
        key="sponsorship",
        label="Visa sponsorship",
        pattern=_words(r"sponsor\w*", r"visas?", r"wiz\w*"),
        explicit=True,
        preference_key="sponsorship.required",
    ),
    Topic(
        key="work_authorization",
        label="Work authorization",
        pattern=_words(
            r"authori[sz]ed to work",
            r"authori[sz]ation to work",
            r"work authori[sz]ation",
            r"right to work",
            r"work permit",
            r"(?:legally )?(?:allowed|eligible|entitled|permitted) to work",
            r"eligibility to work",
            r"pozwoleni\w* na prace",
            r"prawo do pracy",
        ),
        explicit=True,
    ),
    Topic(
        key="salary",
        label="Salary expectations",
        pattern=_words(
            r"salary",
            r"salaries",
            r"compensation",
            r"remuneration",
            r"pay",
            r"(?:hourly|daily|day) rate",
            r"rate expectations?",
            r"wynagrodzeni\w*",
            r"stawk\w*",
            r"widelk\w*",
            r"zarobk\w*",
        ),
        explicit=True,
        preference_key="salary.range",
    ),
    Topic(
        key="criminal",
        label="Criminal record",
        pattern=_words(
            r"criminal",
            r"convicted",
            r"convictions?",
            r"felon\w*",
            r"offen[cs]es?",
            r"background checks?",
            r"karan\w*",
            r"niekaran\w*",
        ),
        explicit=True,
    ),
    Topic(
        key="medical",
        label="Health or disability",
        pattern=_words(
            r"medical",
            r"health",
            r"disabilit\w*",
            r"disabled",
            r"accommodations?",
            r"niepelnosprawn\w*",
            r"zdrowi\w*",
        ),
        explicit=True,
    ),
    Topic(
        key="demographic",
        label="Demographic data",
        pattern=_words(
            r"gender",
            r"sex",
            r"race",
            r"racial",
            r"ethnic\w*",
            r"pronouns?",
            r"sexual orientation",
            r"veterans?",
            r"age",
            r"date of birth",
            r"birth ?date",
            r"nationality",
            r"citizenship",
            r"religio\w*",
            r"plec",
            r"wiek",
            r"data urodzenia",
            r"narodowosc",
            r"obywatelstw\w*",
        ),
        explicit=True,
    ),
    Topic(
        key="consent",
        label="Consent",
        pattern=_words(
            r"consent\w*",
            r"i agree",
            r"agree to",
            r"privacy (?:policy|notice)",
            r"gdpr",
            r"rodo",
            r"zgod\w*",
            r"(?:data|personal data) processing",
            r"process(?:ing)? (?:of )?my (?:personal )?data",
            r"przetwarzani\w*",
        ),
        explicit=True,
    ),
    Topic(
        key="legal",
        label="Legal attestation",
        pattern=_words(
            r"i (?:hereby )?(?:certify|confirm|declare|attest|acknowledge)",
            r"true (?:and|,) (?:complete|correct|accurate)",
            r"non ?compete",
            r"non ?solicitation",
            r"security clearance",
            r"oswiadczam",
            r"zakaz\w* konkurencji",
        ),
        explicit=True,
    ),
    Topic(
        key="contact.email",
        label="Email",
        pattern=_words(r"e ?mail", r"email address"),
        fact_keys=("contact.email",),
    ),
    Topic(
        key="contact.phone",
        label="Phone",
        pattern=_words(r"phone", r"mobile", r"telefon\w*", r"komork\w*"),
        fact_keys=("contact.phone",),
    ),
    Topic(
        key="contact.links.linkedin",
        label="LinkedIn profile",
        pattern=_words(r"linkedin"),
        fact_keys=("contact.links.linkedin",),
    ),
    Topic(
        key="contact.links.github",
        label="GitHub profile",
        pattern=_words(r"github"),
        fact_keys=("contact.links.github",),
    ),
    Topic(
        key="contact.links.website",
        label="Website or portfolio",
        pattern=_words(r"portfolio", r"(?:personal )?website", r"strona www"),
        fact_keys=("contact.links.website",),
    ),
    Topic(
        key="identity.name",
        label="Name",
        pattern=re.compile(
            r"^(?:your )?(?:full |legal )?name$|\bfull name\b|\bfirst and last name\b"
            r"|\bimie i nazwisko\b"
        ),
        fact_keys=("identity.name",),
    ),
    Topic(
        key="contact.location",
        label="Current location",
        pattern=_words(
            r"where are you (?:currently )?(?:based|located)",
            r"current (?:location|city)",
            r"city of residence",
            r"place of residence",
            r"miejsce zamieszkania",
            r"miasto zamieszkania",
        ),
        fact_keys=("contact.location",),
    ),
    Topic(
        key="start_date",
        label="Start date or notice period",
        pattern=_words(
            r"notice period",
            r"start date",
            r"earliest (?:possible )?start",
            r"when (?:can|could|would) you start",
            r"available to start",
            r"availability",
            r"okres\w* wypowiedzenia",
            r"dostepnosc\w*",
            r"kiedy (?:mozesz|moglbys|moglabys) (?:zaczac|rozpoczac)",
        ),
        preference_key="search.start_date",
    ),
    Topic(
        key="relocation",
        label="Relocation",
        pattern=_words(r"relocat\w*", r"przeprowadz\w*", r"relokac\w*"),
        preference_key="search.relocation",
    ),
    Topic(
        key="work_arrangement",
        label="Remote, hybrid or on-site",
        pattern=_words(r"remote", r"hybrid", r"on ?site", r"in the office", r"zdaln\w*"),
        preference_key="search.work_arrangement",
    ),
    Topic(
        key="travel",
        label="Travel",
        pattern=_words(r"travel\w*", r"podroz\w*", r"delegac\w*"),
        preference_key="search.travel",
    ),
)

CUSTOM_PREFIX: Final = "custom."
_MAX_SLUG: Final = 60


def find_topic(text: str) -> Topic | None:
    """The first topic whose pattern matches the folded question, if any."""
    folded = fold(text)
    return next((t for t in TOPICS if t.pattern.search(folded)), None)


def topic_for_key(key: str) -> Topic | None:
    """The topic a question key belongs to (`work_authorization.…` → work authorization)."""
    return next(
        (t for t in TOPICS if key == t.key or key.startswith(t.key + ".")),
        None,
    )


def _slug(text: str) -> str:
    folded = fold(text)
    slug = folded.replace(" ", "_")
    if len(slug) <= _MAX_SLUG:
        return slug or "blank"
    # Long questions keep a readable prefix plus a digest, so two long questions that start
    # the same way still get different keys.
    digest = hashlib.sha256(folded.encode()).hexdigest()[:10]
    return f"{slug[:_MAX_SLUG].rstrip('_')}_{digest}"


def question_key(text: str) -> str:
    """The key answers to this question are saved under.

    Ordinary topics share one key, so an answer to "Notice period?" is reused for "When can
    you start?". Explicit topics get one key per wording, because "authorized to work in
    Poland?" and "authorized to work in the US?" need different answers.
    """
    topic = find_topic(text)
    if topic is None:
        return CUSTOM_PREFIX + _slug(text)
    if topic.explicit:
        return f"{topic.key}.{_slug(text)}"
    return topic.key


def is_explicit_key(key: str) -> bool:
    """True if answers under this key need explicit input unless configured as reusable."""
    topic = topic_for_key(key)
    return topic is not None and topic.explicit


def is_sensitive_question_key(key: str) -> bool:
    """Sensitive answers are masked in listings: explicit topics and sensitive keys."""
    return is_explicit_key(key) or is_sensitive_key(key)
