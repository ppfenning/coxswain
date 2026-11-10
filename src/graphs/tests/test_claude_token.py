"""The token reader's literals. tools/agent_tools tests must pin the same ones."""

from __future__ import annotations

from pathlib import Path

import pytest

from runner.claude_token import TOKEN_ENV, parse_token_text, read_token, token_path

NAME = "CLAUDE_CODE_OAUTH_TOKEN"


def test_token_env_name() -> None:
    assert TOKEN_ENV == NAME


def test_token_path_ends_in_the_agent_tools_file() -> None:
    assert token_path("/h") == "/h/.config/agent-tools/claude-token"
    assert token_path("/h").endswith(".config/agent-tools/claude-token")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tok\n", "tok"),
        ("  tok  ", "tok"),
        (f"{NAME}=tok", "tok"),
        (f"export {NAME}=tok\n", "tok"),
        (f"export   {NAME}=tok", "tok"),
        (f'{NAME}="tok"', "tok"),
        (f"export {NAME}='tok'", "tok"),
        ('"tok"', "tok"),
        (f"# saved by setup\n{NAME}=tok\n\n", "tok"),
        (f"{NAME}=\"tok'", "\"tok'"),
    ],
)
def test_parse_accepts(text: str, expected: str) -> None:
    assert parse_token_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n",
        "# only a comment\n",
        "a\nb\n",
        f"{NAME}=a\n{NAME}=b\n",
        "two words",
        f'{NAME}="two words"',
        f"{NAME}=",
        f'{NAME}=""',
        "OTHER_NAME=tok",
        "export OTHER_NAME=tok",
        "export tok",
    ],
)
def test_parse_rejects(text: str) -> None:
    assert parse_token_text(text) is None


def test_read_token_reads_the_file(tmp_path: Path) -> None:
    path = tmp_path / "claude-token"
    path.write_text(f"export {NAME}=tok\n", encoding="utf-8")
    assert read_token(str(path)) == "tok"


def test_read_token_is_none_for_a_missing_file(tmp_path: Path) -> None:
    assert read_token(str(tmp_path / "absent")) is None


def test_read_token_is_none_for_a_directory(tmp_path: Path) -> None:
    assert read_token(str(tmp_path)) is None
