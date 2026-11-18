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


def test_unit_environment_files_reads_every_line_indented_or_not_and_expands_h() -> None:
    text = "[Service]\nEnvironmentFile=-/etc/a.env\n  EnvironmentFile=%h/.config/b.env\nEnvironmentFile=-/c%%.env\n"
    assert unit_env.unit_environment_files(text, "/srv/home") == ["/etc/a.env", "/srv/home/.config/b.env", "/c%.env"]


def test_unit_environment_files_reads_the_launchd_agents_file_and_nothing_from_a_unit_without_one() -> None:
    plist = upgrade_edge.plist_text("/bin/cox", "/p.yaml", "/usr/bin", 20, "/srv/a&b/garage.env")
    assert unit_env.unit_environment_files(plist, "/srv/home") == ["/srv/a&b/garage.env"]
    assert unit_env.unit_environment_files(upgrade_edge.service_text("/bin/cox", "/p", "/usr/bin", None), "/h") == []


def test_unit_environment_file_is_the_first_as_written_which_chair_service_reads_back() -> None:
    text = upgrade_edge.service_text("/bin/cox", "/p.yaml", "/usr/bin", "%h/x.env") + "EnvironmentFile=-/y.env\n"
    assert unit_env.unit_environment_file(text) == "%h/x.env"
    unit = chair_service.unit_text("/bin/cox", "/ws", "chair", 60, "/srv/e.env", "/p.yaml", "/usr/bin")
    assert (chair_service.unit_args(unit) or {})["env_file"] == "/srv/e.env"


def test_units_environment_files_takes_the_first_unit_that_names_any_else_the_default() -> None:
    first, second = "EnvironmentFile=-/a.env\nEnvironmentFile=-/a2.env\n", "EnvironmentFile=-/b.env\n"
    assert unit_env.units_environment_files([None, "[Service]\n", first, second], "/d.env", "/h") == ["/a.env", "/a2.env"]
    assert unit_env.units_environment_files([None, "[Service]\n"], "/d.env", "/h") == ["/d.env"]


def test_bytes_that_are_not_utf8_read_as_no_file_and_are_named() -> None:
    assert unit_env.decode_text(b"A=1\n") == "A=1\n"
    assert unit_env.decode_text(b"\xff\xfeA=1") is None
    assert unit_env.decode_text(None) is None
    reads = [("/u.service", b"\xff"), ("/gone", None), ("/ok.env", b"A=1\n")]
    assert unit_env.undecodable(reads) == ["/u.service"]


def test_merged_values_lets_a_later_file_win_as_systemd_does() -> None:
    assert unit_env.merged_values(["A=1\nB=1\n", None, "B=2\n"]) == {"A": "1", "B": "2"}


def test_plan_load_takes_only_what_the_shell_lacks_and_names_what_it_overrides() -> None:
    process = {"GARAGE_ACCESS_KEY_ID": "old", "EMPTY": "", "SAME": "v"}
    file_values = {"GARAGE_ACCESS_KEY_ID": "new", "GARAGE_SECRET_ACCESS_KEY": "s", "EMPTY": "x", "SAME": "v"}
    load = unit_env.plan_load(process, file_values)
    assert load.loaded == {"GARAGE_SECRET_ACCESS_KEY": "s"}
    assert load.overridden == ("GARAGE_ACCESS_KEY_ID", "EMPTY")  # an empty shell value overrides too; an equal one does not
    assert load.unsafe == ()


def test_plan_load_skips_a_value_a_unit_would_read_differently() -> None:
    values = unit_env.parse_env('Q="abc"\nS=a b\nOK=plain\n')
    load = unit_env.plan_load({}, values)
    assert load.loaded == {"OK": "plain"}
    assert load.unsafe == ("Q", "S")


def test_load_notes_name_files_and_variables_and_never_a_value() -> None:
    load = unit_env.plan_load({"K": "shell-secret"}, {"K": "file-secret", "N": "n-secret", "Q": '"q-secret"'})
    notes = unit_env.load_notes(load, ["/u.env"], ["/gone.env"], ["/bad.env"])
    assert notes == [
        "env: skipped /bad.env: not UTF-8 text",
        "env: /gone.env, which a unit names, does not exist",
        "env: loaded N from /u.env, as the units read it",
        "env: the shell's K override /u.env; unset them to use the file's",
        "env: not loaded Q from /u.env: a unit would read the value differently (quotes, spaces, $, % or empty); "
        "rewrite it as NAME=value with a plain value",
    ]
    assert not any("secret" in line for line in notes)
    assert unit_env.load_notes(unit_env.plan_load({}, {}), ["/d.env"], [], []) == []
