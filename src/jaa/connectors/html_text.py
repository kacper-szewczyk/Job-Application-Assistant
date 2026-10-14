"""Plain text from the HTML snippets job sources put in descriptions.

Matching and review work on text, and HTML from a source must never be rendered. This keeps
paragraph and list structure as line breaks and drops everything else: tags, attributes,
styles, scripts and comments.
"""

import re
from html.parser import HTMLParser
from typing import Final, override

_BLOCK: Final = frozenset(
    {"p", "div", "section", "article", "ul", "ol", "table", "tr", "h1", "h2", "h3", "h4", "h5",
     "h6", "blockquote", "pre", "header", "footer"}
)  # fmt: skip
_SKIP: Final = frozenset({"script", "style", "template", "noscript", "head"})


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skipping += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK:
            self.parts.append("\n\n")

    @override
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCK:
            # List items end where the next one starts, so a list has no blank lines inside.
            self.parts.append("\n")

    @override
    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


_SPACES: Final = re.compile(r"[^\S\n]+")
_BLANK_LINES: Final = re.compile(r"\n{3,}")


def html_to_text(markup: str) -> str:
    """Readable text: lines for paragraphs and list items, single spaces, no markup."""
    parser = _TextParser()
    parser.feed(markup)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()
