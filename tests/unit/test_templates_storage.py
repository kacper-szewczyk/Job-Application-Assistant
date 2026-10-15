"""Uploaded templates, versions, defaults and resolution (ADR 0029)."""

import logging
import sqlite3

import pytest

from jaa.core.errors import TemplateError
from jaa.core.models import TemplateFamily, TemplateKind, TemplateOrigin
from jaa.documents.contract import TemplateFiles
from jaa.documents.latex import DEFAULT_LETTER_TEMPLATE, DEFAULT_TEMPLATE
from jaa.storage import templates as service

PERSONAL = "Zofia Sekretna-Testowa"
MAIN = (
    "\\documentclass{moderncv}\n\\moderncvstyle{classic}\n\\hyphenpenalty=10000\n"
    "%%jaa:babel%%\n%%jaa:header%%\n\\begin{document}\n\\makecvtitle\n"
    f"% made by {PERSONAL}\n%%jaa:body%%\n%%jaa:consent%%\n\\end{{document}}\n"
)


def _files(main: str = MAIN, logo: bytes = b"\x89PNG-1") -> TemplateFiles:
    return TemplateFiles(
        main_file="cv.tex",
        texts={"cv.tex": main, "cv.cfg": "% config"},
        assets={"img/logo.png": logo},
        origin=TemplateOrigin.ZIP,
    )


def test_add_versions_and_resolve(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    first, created = service.add_user_template(conn, _files(), name="mine", title="Mine")
    assert created
    assert first.version_label == "user-mine@1"
    assert first.family is TemplateFamily.MODERNCV
    assert first.files["img/logo.png"] == {"base64": "iVBORy0x"}

    same, created = service.add_user_template(conn, _files(), name="user-mine")
    assert not created and same.id == first.id

    second, created = service.add_user_template(conn, _files(logo=b"\x89PNG-2"), name="mine")
    assert created and second.version_label == "user-mine@2"
    assert second.name == "Mine"  # kept from the first version
    assert second.content_sha256 != first.content_sha256

    template = service.resolve_template(conn, "user-mine")
    assert template.version_label == "user-mine@2"
    assert template.uploaded
    assert template.sha256 == second.content_sha256
    assert template.source == MAIN
    assert dict(template.assets) == {"img/logo.png": b"\x89PNG-2", "cv.cfg": b"% config"}
    assert service.resolve_template(conn, "user-mine@1").assets["img/logo.png"] == b"\x89PNG-1"
    assert [t.version for t in service.latest_user_templates(conn)] == [2]
    assert PERSONAL not in caplog.text


def test_add_refuses_templates_that_break_the_contract(conn: sqlite3.Connection) -> None:
    with pytest.raises(TemplateError, match="missing slot line %%jaa:consent%%"):
        service.add_user_template(conn, _files(MAIN.replace("%%jaa:consent%%", "")), name="x")
    with pytest.raises(TemplateError, match="lowercase letters"):
        service.add_user_template(conn, _files(), name="Bad Name")
    assert service.user_templates(conn) == []


def test_kind_is_fixed_per_id(conn: sqlite3.Connection) -> None:
    service.add_user_template(conn, _files(), name="mine")
    letter = (
        "\\documentclass{moderncv}\n%%jaa:babel%%\n%%jaa:header%%\n%%jaa:letter%%\n"
        "\\begin{document}\n%%jaa:body%%\n\\end{document}\n"
    )
    with pytest.raises(TemplateError, match="is a cv template"):
        service.add_user_template(conn, _files(letter), name="mine", kind=TemplateKind.COVER_LETTER)
    with pytest.raises(TemplateError, match="not a cover_letter template"):
        service.resolve_template(conn, "user-mine", TemplateKind.COVER_LETTER)


def test_rows_are_immutable(conn: sqlite3.Connection) -> None:
    stored, _ = service.add_user_template(conn, _files(), name="mine")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE user_templates SET files = '{}' WHERE id = ?", (stored.id,))


def test_defaults(conn: sqlite3.Connection) -> None:
    assert service.default_template_id(conn, TemplateKind.CV) == DEFAULT_TEMPLATE
    assert service.resolve_template(conn, None).id == DEFAULT_TEMPLATE
    assert (
        service.resolve_template(conn, None, TemplateKind.COVER_LETTER).id
        == DEFAULT_LETTER_TEMPLATE
    )

    service.set_default_template(conn, "moderncv-banking")
    assert service.resolve_template(conn, None).id == "moderncv-banking"
    service.add_user_template(conn, _files(), name="mine")
    service.set_default_template(conn, "user-mine")
    assert service.resolve_template(conn, None).version_label == "user-mine@1"
    with pytest.raises(TemplateError, match="unknown cv template"):
        service.set_default_template(conn, "nope")
    with pytest.raises(TemplateError, match="has version 1 only"):
        service.set_default_template(conn, "moderncv-casual@2")

    assert service.delete_user_template(conn, "user-mine") == 1
    assert service.default_template_id(conn, TemplateKind.CV) == DEFAULT_TEMPLATE
    assert conn.execute("SELECT count(*) FROM template_defaults").fetchone()[0] == 3
    with pytest.raises(TemplateError, match="no uploaded template 'user-mine'"):
        service.resolve_template(conn, "user-mine")
    with pytest.raises(TemplateError, match="no uploaded template"):
        service.delete_user_template(conn, "user-mine")
