from pathlib import Path

import pytest
from typer.testing import CliRunner

from jaa.cli import app
from jaa.core.models import PreferenceStatus
from jaa.core.preferences import QUESTIONS
from jaa.storage import preferences as prefs
from jaa.storage.db import open_database

runner = CliRunner()
SALARY_TEXT = "123457-133457 EUR per year"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    env = {"JAA_DATA_DIR": str(tmp_path / "data"), "JAA_LOG_LEVEL": "DEBUG", "COLUMNS": "200"}
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    return env


def _run(env: dict[str, str], *args: str, input: str | None = None) -> str:
    result = runner.invoke(app, list(args), env=env, input=input)
    assert result.exit_code == 0, result.output
    return result.output


def _stored(env: dict[str, str]) -> dict[str, prefs.Preference]:
    conn = open_database(Path(env["JAA_DATA_DIR"]) / "jaa.sqlite3")
    try:
        return prefs.list_preferences(conn)
    finally:
        conn.close()


def test_onboard_skip_everything_then_answer_later(env: dict[str, str]) -> None:
    out = _run(env, "onboard", input="\n" * len(QUESTIONS))
    assert f"0 answered, {len(QUESTIONS)} skipped" in out
    stored = _stored(env)
    assert len(stored) == len(QUESTIONS)
    assert all(p.status is PreferenceStatus.SKIPPED for p in stored.values())

    # Skipped questions are asked again; a bad answer re-prompts instead of failing.
    answers = ["Widget Engineer", "senior", "Berlin", "remote, hybrid", SALARY_TEXT, "lots"]
    answers += ["1", "", "1", "1", "1", "immediately", "", ""]
    out = _run(env, "onboard", input="\n".join(answers) + "\n")
    assert "salary.range: expected" not in out  # salary was valid
    assert "search.employment_types: expected one of" in out  # "lots" was not
    assert "10 answered, 3 skipped" in out

    stored = _stored(env)
    assert stored["search.employment_types"].value == ["full_time"]
    assert stored["search.industries"].status is PreferenceStatus.SKIPPED
    assert stored["salary.range"].is_sensitive


def test_onboard_all_keeps_answers_on_enter(env: dict[str, str]) -> None:
    _run(env, "preferences", "set", "salary.range", SALARY_TEXT)
    out = _run(env, "onboard", "--all", input="\n" * len(QUESTIONS))
    assert "Current: •••• (sensitive)" in out
    assert "123457" not in out
    stored = _stored(env)
    assert stored["salary.range"].status is PreferenceStatus.ANSWERED


def test_onboard_with_nothing_left(env: dict[str, str]) -> None:
    answers = ["Widget Engineer", "1", "Berlin", "1", SALARY_TEXT, "1", "climate"]
    answers += ["1", "1", "1", "2026-12-01", "1", "1"]
    assert len(answers) == len(QUESTIONS)
    assert "13 answered, 0 skipped" in _run(env, "onboard", input="\n".join(answers) + "\n")
    out = _run(env, "onboard")
    assert "All preference questions are answered." in out


def test_list_set_skip_history(env: dict[str, str]) -> None:
    out = _run(env, "preferences", "list")
    assert "not asked yet" in out

    out = _run(env, "preferences", "set", "salary.range", SALARY_TEXT)
    assert "Saved salary.range: •••• (sensitive)" in out
    assert "123457" not in out
    _run(env, "preferences", "set", "search.target_roles", "Widget Engineer, Gadget Engineer")
    _run(env, "preferences", "set", "search.start_date", '{"type": "immediately"}', "--json")

    out = _run(env, "preferences", "list")
    assert "Widget Engineer, Gadget Engineer" in out
    assert "Immediately" in out
    assert "123457" not in out
    assert "Sensitive values are hidden" in out
    assert "123,457\N{EN DASH}133,457 EUR per year" in _run(env, "preferences", "list", "--reveal")

    _run(env, "preferences", "skip", "search.target_roles")
    out = _run(env, "preferences", "history", "search.target_roles")
    assert "Widget Engineer, Gadget Engineer" in out
    assert "skipped" in out
    assert "123457" not in _run(env, "preferences", "history", "salary.range")


@pytest.mark.parametrize(
    "args",
    [
        ["preferences", "set", "search.favourite_colour", "blue"],
        ["preferences", "set", "search.travel", "sometimes"],
        ["preferences", "set", "search.travel", "{", "--json"],
        ["preferences", "set", "search.travel", '"sometimes"', "--json"],
        ["preferences", "skip", "search.favourite_colour"],
    ],
)
def test_invalid_input_is_reported(env: dict[str, str], args: list[str]) -> None:
    result = runner.invoke(app, args, env=env)
    assert result.exit_code == 1
    assert "Error:" in result.output


def test_values_never_reach_logs(env: dict[str, str]) -> None:
    # The CLI's log handler writes to the (captured) stderr, with DEBUG enabled by `env`.
    logs = ""
    for args, input in [
        (["preferences", "set", "salary.range", SALARY_TEXT], None),
        (["preferences", "set", "search.target_roles", "Synthetic Role Zeta"], None),
        (["onboard", "--all"], "\n" * len(QUESTIONS)),
    ]:
        result = runner.invoke(app, args, env=env, input=input)
        assert result.exit_code == 0, result.output
        logs += result.stderr
    assert "preference salary.range answered" in logs
    assert "123457" not in logs
    assert "Synthetic Role Zeta" not in logs
