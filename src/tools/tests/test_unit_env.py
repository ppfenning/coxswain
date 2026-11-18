from __future__ import annotations

import pytest

from agent_tools import chair_service, claude_token, unit_env, upgrade_edge


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


def test_unit_environment_file_reads_a_systemd_units_environment_file_line() -> None:
    text = upgrade_edge.service_text("/bin/cox", "/p.yaml", "/usr/bin", "/srv/unit/garage.systemd.env")
    assert unit_env.unit_environment_file(text) == "/srv/unit/garage.systemd.env"


def test_unit_environment_file_reads_the_file_a_launchd_agent_sources() -> None:
    text = upgrade_edge.plist_text("/bin/cox", "/p.yaml", "/usr/bin", 20, "/srv/a&b/garage.env")
    assert unit_env.unit_environment_file(text) == "/srv/a&b/garage.env"


def test_unit_environment_file_is_none_for_a_unit_that_names_no_file() -> None:
    assert unit_env.unit_environment_file(upgrade_edge.service_text("/bin/cox", "/p.yaml", "/usr/bin", None)) is None


def test_units_environment_file_takes_the_first_unit_that_names_one_else_the_default() -> None:
    first, second = "EnvironmentFile=-/a.env\n", "EnvironmentFile=-/b.env\n"
    assert unit_env.units_environment_file([None, "[Service]\n", first, second], "/default.env") == "/a.env"
    assert unit_env.units_environment_file([None, "[Service]\n"], "/default.env") == "/default.env"


def test_missing_values_keeps_only_names_the_process_lacks_so_the_process_wins() -> None:
    process = {"GARAGE_ACCESS_KEY_ID": "from-shell", "EMPTY": ""}
    file_values = {"GARAGE_ACCESS_KEY_ID": "from-file", "GARAGE_SECRET_ACCESS_KEY": "s", "EMPTY": "x"}
    assert unit_env.missing_values(process, file_values) == {"GARAGE_SECRET_ACCESS_KEY": "s"}


def test_a_file_parsed_then_merged_fills_only_the_gaps() -> None:
    text = unit_env.render_env({"GARAGE_ACCESS_KEY_ID": "k", "GARAGE_SECRET_ACCESS_KEY": "s", "PATH": "/unit/bin"})
    merged = unit_env.missing_values({"PATH": "/shell/bin"}, unit_env.parse_env(text))
    assert merged == {"GARAGE_ACCESS_KEY_ID": "k", "GARAGE_SECRET_ACCESS_KEY": "s"}
