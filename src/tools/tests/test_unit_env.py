from __future__ import annotations

import pytest

from agent_tools import chair_service, claude_token, unit_env


def test_render_env_writes_the_known_keys_first_in_order_then_the_rest() -> None:
    values = {"B": "2", "A": "1", "CLAUDE_CODE_OAUTH_TOKEN": "abc"}
    assert unit_env.render_env(values) == "CLAUDE_CODE_OAUTH_TOKEN=abc\nB=2\nA=1\n"


def test_render_env_refuses_a_value_systemd_would_split_or_expand() -> None:
    with pytest.raises(ValueError, match="Y, X"):
        unit_env.render_env({"Y": "a b", "OK": "/x", "X": "$HOME"})


def test_render_env_refuses_a_name_that_is_not_an_identifier() -> None:
    with pytest.raises(ValueError, match="not a plain environment name: A B"):
        unit_env.render_env({"A B": "1"})


def test_parse_env_round_trips_render_env() -> None:
    values = {"CLAUDE_CODE_OAUTH_TOKEN": "abc", "U": "postgresql://h:5432/db", "X": "1"}
    assert unit_env.parse_env(unit_env.render_env(values)) == values


def test_parse_env_skips_blanks_comments_and_export_lines_and_splits_on_the_first_equals() -> None:
    text = "# note\n\nA=1\nexport B=2\n  C = x=y \nnot a pair\n"
    assert unit_env.parse_env(text) == {"A": "1", "C": "x=y"}


def test_environment_file_line_tolerates_a_missing_file() -> None:
    assert unit_env.environment_file_line("/etc/coxswain/chair.env") == "EnvironmentFile=-/etc/coxswain/chair.env"


def test_env_keys_are_names_an_existing_module_defines() -> None:
    assert unit_env.ENV_KEYS == (claude_token.TOKEN_ENV,)


def test_unsafe_values_names_what_systemd_would_split_or_expand() -> None:
    assert unit_env.unsafe_values({"--label": "a b", "cox": "/bin/cox", "x": "50%", "y": "$HOME", "z": ""}) == [
        "--label", "x", "y", "z",
    ]


def test_export_lines_numbers_the_shell_style_assignments() -> None:
    assert unit_env.export_lines("A=1\nexport B=2\n# c\n  export C=3\n") == [2, 4]


def test_chair_service_still_exposes_the_moved_rules() -> None:
    assert chair_service.unsafe_values is unit_env.unsafe_values
    assert chair_service.export_lines is unit_env.export_lines
