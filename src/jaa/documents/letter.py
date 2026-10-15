"""Cover letters: the checked paragraphs of a tailoring plan set as a letter (ADR 0024).

Pure: `LetterContent` holds only text the caller passes in. The sender comes from confirmed
facts, the recipient from the job or the user, the opening and closing only from the user, and
the paragraphs from the plan. Nothing is generated except the date and the template's layout,
so the letter never greets an invented person. Fields hold personal text, so they use
`repr=False`.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Final

from jaa.core.errors import DocumentError
from jaa.documents.content import CvContent, CvLanguage
from jaa.documents.latex import CvTemplate, babel, escape, fill_slots, sender_header

_MONTHS: Final[dict[CvLanguage, tuple[str, ...]]] = {
    CvLanguage.EN: (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ),
    # Genitive, as Polish dates are written: "15 października 2026".
    CvLanguage.PL: (
        "stycznia",
        "lutego",
        "marca",
        "kwietnia",
        "maja",
        "czerwca",
        "lipca",
        "sierpnia",
        "września",
        "października",
        "listopada",
        "grudnia",
    ),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class LetterContent:
    # Only the name and contact details are used.
    sender: CvContent = field(repr=False)
    # The first line is the recipient's name (by default the employer); the rest is the address.
    recipient: tuple[str, ...] = field(repr=False)
    date: date
    paragraphs: tuple[str, ...] = field(repr=False)
    opening: str | None = field(default=None, repr=False)
    closing: str | None = field(default=None, repr=False)


def format_date(day: date, language: CvLanguage) -> str:
    """`15 October 2026` or `15 października 2026`."""
    return f"{day.day} {_MONTHS[language][day.month - 1]} {day.year}"


def _optional(text: str | None) -> str:
    # moderncv ends the opening and closing with a line break, which fails on an empty line.
    return escape(text) if text and text.strip() else "\\mbox{}"


def render_letter(template: CvTemplate, letter: LetterContent, language: CvLanguage) -> str:
    """LaTeX source for `letter`. Every piece of text is escaped."""
    if not letter.paragraphs:
        raise DocumentError("the cover letter has no paragraphs")
    if not letter.recipient or not letter.recipient[0].strip():
        raise DocumentError("the cover letter needs a recipient")
    name, *rest = letter.recipient
    address = [a for a in rest if a.strip()]
    address_lines = "\\\\".join(escape(a) for a in address)
    letter_setup = "\n".join(
        [
            f"\\recipient{{{escape(name)}}}{{{address_lines}}}",
            f"\\date{{{escape(format_date(letter.date, language))}}}",
            f"\\opening{{{_optional(letter.opening)}}}",
            f"\\closing{{{_optional(letter.closing)}}}",
        ]
    )
    return fill_slots(
        template,
        {
            "babel": babel(language),
            "header": sender_header(letter.sender),
            "letter": letter_setup,
            "body": "\n\n".join(escape(p) for p in letter.paragraphs),
        },
    )


def expected_letter_text(letter: LetterContent, language: CvLanguage) -> list[str]:
    """Every piece of text the letter's PDF must contain, in document order."""
    sender = letter.sender
    texts: list[str | None] = [sender.name, sender.location, sender.phone, sender.email]
    texts += [url for _, url in sender.links]
    texts += [*letter.recipient, format_date(letter.date, language), letter.opening]
    texts += [*letter.paragraphs, letter.closing]
    return [t.strip() for t in texts if t and t.strip()]
