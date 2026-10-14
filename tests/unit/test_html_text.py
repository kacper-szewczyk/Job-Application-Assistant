import pytest

from jaa.connectors.html_text import html_to_text


@pytest.mark.parametrize(
    ("markup", "text"),
    [
        ("<p>One</p><p>Two</p>", "One\n\nTwo"),
        ("<ul><li>a</li><li>b <b>bold</b></li></ul>", "- a\n- b bold"),
        ("Line<br>break<br/>again", "Line\nbreak\nagain"),
        ("A&nbsp;&amp;&nbsp;B &lt;tag&gt;", "A & B <tag>"),
        ("<style>p {}</style><script>alert(1)</script><p>Kept</p>", "Kept"),
        ("<!-- note --><p>  lots   of\tspace  </p>", "lots of space"),
        ("<div><p>x</p></div><div></div><div><p>y</p></div>", "x\n\ny"),
        ("plain text", "plain text"),
        ("", ""),
    ],
)
def test_html_to_text(markup: str, text: str) -> None:
    assert html_to_text(markup) == text


def test_markup_never_survives() -> None:
    text = html_to_text('<a href="javascript:x()" onclick="y()">link</a><img src=x onerror=z()>')
    assert text == "link"
