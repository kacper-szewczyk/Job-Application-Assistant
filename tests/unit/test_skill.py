"""The Claude Code skill (T17) names only tools and commands that exist, and backs its rules."""

import json
import re
from pathlib import Path
from typing import Final

import anyio
import pytest
from mcp import Client
from typer.testing import CliRunner

from jaa.cli import app
from jaa.config import Settings, load_settings
from jaa.mcp.server import build_server

ROOT: Final = Path(__file__).resolve().parents[2]
SKILL_DIR: Final = ROOT / "skills" / "job-application-assistant"
SKILL: Final = SKILL_DIR / "SKILL.md"
SETTINGS: Final = ROOT / ".claude" / "settings.json"

runner = CliRunner()


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _frontmatter() -> dict[str, str]:
    match = re.match(r"---\n(.*?)\n---\n", _text(), re.DOTALL)
    assert match, "SKILL.md starts with YAML frontmatter"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _commands(text: str) -> set[str]:
    """`jaa …` command paths quoted in the text, without their arguments and options."""
    found: set[str] = set()
    for span in re.findall(r"`(?:uv run )?(jaa(?: [^`]*)?)`", text):
        words: list[str] = []
        for word in span.split()[1:]:
            if not re.fullmatch(r"[a-z][a-z-]*", word):
                break
            words.append(word)
        found.add(" ".join(["jaa", *words]))
    return found


def _section(heading: str) -> str:
    """The paragraph that starts with a bold heading, up to the next blank line."""
    match = re.search(rf"\*\*{re.escape(heading)}\*\*(.*?)\n\n", _text(), re.DOTALL)
    assert match, heading
    return match.group(1)


def _user_only() -> set[str]:
    return _commands(_section("Commands only the user runs."))


def _deny_rules() -> list[str]:
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    return settings["permissions"]["deny"]


def test_frontmatter_names_the_skill_and_says_when_to_use_it() -> None:
    fields = _frontmatter()
    assert fields["name"] == SKILL_DIR.name
    assert re.fullmatch(r"[a-z0-9-]{1,64}", fields["name"])
    assert 0 < len(fields["description"]) <= 1024
    assert "Use when" in fields["description"]


def test_claude_code_loads_the_skill_from_the_repository() -> None:
    linked = ROOT / ".claude" / "skills" / SKILL_DIR.name
    assert linked.is_symlink()
    assert linked.resolve() == SKILL_DIR


def test_every_mcp_tool_is_described(tmp_path: Path) -> None:
    settings: Settings = load_settings({"JAA_DATA_DIR": str(tmp_path / "data")})

    async def names() -> set[str]:
        async with Client(build_server(settings)) as client:
            return {t.name for t in (await client.list_tools()).tools}

    tools = anyio.run(names)
    table = set(re.findall(r"^\| `([a-z_]+)` \|", _text(), re.MULTILINE))
    assert table == tools


def test_every_command_it_names_exists() -> None:
    commands = _commands(_text())
    assert len(commands) > 20
    for command in sorted(commands):
        result = runner.invoke(app, [*command.split()[1:], "--help"])
        assert result.exit_code == 0, f"{command}: {result.output}"


@pytest.mark.parametrize(
    "rule",
    [
        "Never invent facts.",
        "Imported facts are candidates.",
        "No LinkedIn scraping.",
        "The user submits applications.",
        "Sensitive questions are the user's.",
    ],
)
def test_hard_rules_are_stated(rule: str) -> None:
    hard_rules = _text().split("## Hard rules", 1)[1].split("\n## ", 1)[0]
    assert f"**{rule}**" in hard_rules


def test_confirmations_go_through_the_users_dialog() -> None:
    checkpoints = _text().split("## Checkpoints", 1)[1].split("\n## ", 1)[0]
    for tool in ("decide_facts", "update_application_status"):
        assert f"`{tool}`" in checkpoints
    assert "jaa cv tailor approve" in _user_only()


def test_commands_that_record_the_users_decisions_are_denied_to_claude() -> None:
    user_only = _user_only()
    assert {"jaa profile confirm", "jaa apply submitted", "jaa apply status"} <= user_only
    deny = set(_deny_rules())
    for command in user_only:
        for prefix in ("", "uv run "):
            assert f"Bash({prefix}{command}:*)" in deny, command


def test_commands_claude_may_run_are_not_user_only() -> None:
    steps = _text().split("## Command line steps", 1)[1].split("\n## ", 1)[0]
    runnable = _commands(steps) - {"jaa"}
    assert runnable
    assert not runnable & _user_only()
    denied = {re.sub(r"^Bash\((?:uv run )?(.*):\*\)$", r"\1", r) for r in _deny_rules()}
    for command in runnable:
        assert not any(command.startswith(d) for d in denied), command


def test_linkedin_is_never_fetched() -> None:
    deny = _deny_rules()
    assert "WebFetch(domain:linkedin.com)" in deny
    assert "WebFetch(domain:www.linkedin.com)" in deny
