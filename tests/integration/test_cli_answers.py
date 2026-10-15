import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.core import models as m
from jaa.storage import answers as service
from jaa.storage.db import open_database
from jaa.storage.jobs import add_job
from jaa.storage.repository import profile_facts
from tests.conftest import SYNTHETIC_ANSWER, SYNTHETIC_EMAIL, T0, ident

runner = CliRunner()
AUTH = "Are you authorized to work in Poland?"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    env = {"JAA_DATA_DIR": str(tmp_path / "data"), "JAA_LOG_LEVEL": "DEBUG", "COLUMNS": "220"}
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    return env


@pytest.fixture
def db(env: dict[str, str]) -> Iterator[sqlite3.Connection]:
    conn = open_database(Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3")
    yield conn
    conn.close()


@pytest.fixture
def job(db: sqlite3.Connection) -> int:
    stored = add_job(
        db,
        m.Job(
            source="example_board",
            source_job_id="w-1",
            canonical_url="https://boards.example.test/jobs/1",
            employer="Example Co",
            title="Widget Engineer",
            first_seen_at=T0,
            last_seen_at=T0,
            requirements={
                "questions": [
                    {
                        "key": "q1",
                        "label": "Email",
                        "kind": "text",
                        "required": True,
                        "options": [],
                    }
                ]
            },
            status_changed_at=T0,
        ),
    )
    profile_facts.insert(
        db,
        m.ProfileFact(
            field_key="contact.email",
            value_raw=SYNTHETIC_EMAIL,
            value_normalized=SYNTHETIC_EMAIL,
            state=m.FactState.CONFIRMED,
            origin=m.FactOrigin.USER,
            is_sensitive=True,
            created_at=T0,
            updated_at=T0,
            confirmed_at=T0,
        ),
    )
    db.commit()
    return ident(stored.id)


def _run(env: dict[str, str], *args: str, input: str | None = None, code: int = 0) -> str:
    result = runner.invoke(app, list(args), env=env, input=input)
    assert result.exit_code == code, result.output
    return result.output


def test_prepare_review_and_reuse(env: dict[str, str], job: int, db: sqlite3.Connection) -> None:
    _run(env, "answers", "add-question", str(job), AUTH, "--required")
    _run(env, "answers", "add-question", str(job), "Why widgets?")
    out = _run(env, "answers", "questions", str(job))
    assert "listing" in out and AUTH in out

    out = _run(env, "answers", "prepare", str(job))
    assert "profile" in out and "fact #" in out
    assert SYNTHETIC_EMAIL not in out  # masked without --reveal
    assert "2 need your input" in out
    assert SYNTHETIC_EMAIL in _run(env, "answers", "prepare", str(job), "--reveal")

    # The email is filled from the profile; the two others need input.
    out = _run(env, "answers", "review", str(job), input=f"{SYNTHETIC_ANSWER}\n\n")
    assert "1 saved, 1 skipped" in out
    # The runner echoes typed input; nothing else (logs included) may repeat it.
    assert out.count(SYNTHETIC_ANSWER) == 1

    current = service.current_answers(db)
    (auth,) = current.values()
    assert auth.value == SYNTHETIC_ANSWER
    assert auth.job_id == job
    assert auth.reuse_policy is m.ReusePolicy.ALWAYS_ASK

    # Saved sensitive answers are still asked every time; accepting confirms them again.
    out = _run(env, "answers", "prepare", str(job))
    assert "sensitive: needs your explicit answer" in out
    out = _run(env, "answers", "review", str(job), input="y\n\n")
    assert "1 saved" in out
    history = service.answer_history(db, auth.question_key)
    assert [a.origin for a in history] == [m.AnswerOrigin.USER, m.AnswerOrigin.RECONFIRMED]


def test_safe_reuse_needs_confirmation(env: dict[str, str], job: int) -> None:
    _run(env, "answers", "add-question", str(job), AUTH)
    _run(env, "answers", "set", AUTH, SYNTHETIC_ANSWER)
    out = _run(env, "answers", "list")
    assert SYNTHETIC_ANSWER not in out and "always ask" in out
    key = out.split("work_authorization.")[1].split()[0]
    key = "work_authorization." + key

    _run(env, "answers", "policy", key, "reuse", input="n\n", code=1)
    _run(env, "answers", "policy", key, "reuse", input="y\n")
    out = _run(env, "answers", "prepare", str(job))
    assert "you set this answer to be reused without asking" in out
    assert "0 need your input" in out

    out = _run(env, "answers", "show", key, "--reveal")
    assert "policy_changed" in out and SYNTHETIC_ANSWER in out


def test_set_reconfirm_forget_and_errors(env: dict[str, str]) -> None:
    out = _run(env, "answers", "set", "Notice period?", "1 month", "--expires", "never")
    assert "start_date" in out
    _run(env, "answers", "set", AUTH, "yes", "--expires", "never", code=1)
    _run(env, "answers", "set", "Notice period?", "x", "--policy", "sometimes", code=1)
    out = _run(env, "answers", "reconfirm", "start_date", "--expires", "2099-01-01")
    assert "review by 2099-01-01" in out
    out = _run(env, "answers", "forget", "start_date", "--yes")
    assert "Deleted 2 version(s)" in out
    assert "No saved answers yet." in _run(env, "answers", "list")
    _run(env, "answers", "prepare", "99", code=1)
