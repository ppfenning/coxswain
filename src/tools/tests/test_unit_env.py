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
    assert unit_env.unit_environment_files(text, "/srv/hh") == ["/etc/a.env", "/srv/hh/.config/b.env", "/c%.env"]


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


def test_read_env_files_lets_a_later_file_win_as_systemd_does() -> None:
    values, skipped = unit_env.read_env_files(["A=1\nB=1\n", None, "B=2\n"], "linux")
    assert (values, skipped) == ({"A": "1", "B": "2"}, ())


def test_plan_load_takes_only_what_the_shell_lacks_and_names_what_it_overrides() -> None:
    process = {"GARAGE_ACCESS_KEY_ID": "old", "EMPTY": "", "SAME": "v", "QUOTED": "q"}
    values, skipped = unit_env.read_env_files(
        ['GARAGE_ACCESS_KEY_ID=new\nGARAGE_SECRET_ACCESS_KEY=s\nEMPTY=x\nSAME=v\nQUOTED="q"\n'], "linux")
    load = unit_env.plan_load(process, values, skipped)
    assert load.loaded == {"GARAGE_SECRET_ACCESS_KEY": "s"}
    # an empty shell value overrides too; one equal to the file's value as the unit reads it (quotes stripped) does not
    assert load.overridden == ("GARAGE_ACCESS_KEY_ID", "EMPTY")
    assert load.skipped == ()


SKIP_REASONS = {unit_env.SH_EXPANDS, unit_env.SH_SYNTAX, unit_env.SH_COMMAND, unit_env.SH_LEADING, unit_env.SH_CR,
                unit_env.SH_OPEN_QUOTE}

# Each row: an env file, then what the units read for K on linux (systemd's EnvironmentFile=) and on darwin (launchd's
# `sh .`): a value, None for not set at all, or the reason a line did not set it (noted). A darwin value is what sh
# leaves K at, an earlier line's value included. Every linux value was measured against a transient
# `systemd-run --user` unit reading the same file; every darwin value against bash --posix and zsh's sh emulation.
VALUE_ROWS = [
    ("K=a$HOME%hb\n", "a$HOME%hb", unit_env.SH_EXPANDS),
    ("K=x%%y\n", "x%%y", "x%%y"),
    ('K="q v"\n', "q v", "q v"),
    ("K=a\\b\n", "ab", "ab"),
    ("K=\n", "", ""),
    ("K=   \n", "", ""),
    ("K=pg pass word\n", "pg pass word", unit_env.SH_COMMAND),  # a Postgres password with spaces loads on linux
    ("K=trail   \n", "trail", "trail"),
    ("K=end\\\nJ=after\n", "endJ=after", "endJ=after"),  # a trailing backslash joins the next line on both
    ('K="a\\nb"\n', "a\\nb", "a\\nb"),
    ("K=\"x\"y'z'\n", "xy'z'", "xyz"),
    ("K='a''b'\n", "ab", "ab"),
    ('K=x"y z"w\n', 'x"y z"w', "xy zw"),
    ("K=  lead\n", "lead", unit_env.SH_LEADING),
    ("K='a;b'\n", "a;b", "a;b"),
    ('K="p`w"\n', "p`w", unit_env.SH_EXPANDS),
    ("K=a\\$b\n", "a$b", "a$b"),
    ("K='$x'\n", "$x", "$x"),
    ('K="open\nJ=x\n', "open\nJ=x\n", unit_env.SH_OPEN_QUOTE),  # systemd runs an open quote to the end of the file
    ("export K=v\n", None, "v"),
    ("K=a # b\n", "a # b", "a"),
    # sh: an assignment followed by a command applies to that command only, so K keeps its earlier value
    ("K=good\nK=a b\n", "a b", "good"),
    ("K=good\nK=  x\n", "x", "good"),
    # sh assigns the part before `;`, which this does not load, so the earlier value goes too
    ("K=good\nK=a;b\n", "a;b", unit_env.SH_SYNTAX),
    ("K=abc\r\n", "abc", unit_env.SH_CR),  # sh keeps a CRLF line's \r; systemd drops it as trailing whitespace
    ("K=1 W=2\n", "1 W=2", "1"),
    # sh expands a ~ only at the value's start or after a `:`; elsewhere it is literal and loads
    ("K=x~y\n", "x~y", "x~y"),
    ("K=ab~\n", "ab~", "ab~"),
    ("K=a=~/b\n", "a=~/b", "a=~/b"),
    ("K=~/a\n", "~/a", unit_env.SH_SYNTAX),
    ("K=a:~/b\n", "a:~/b", unit_env.SH_SYNTAX),
    # sh reads past `;` into another assignment and sets both; a command after it is still skipped
    ("K=1;W=2\n", "1;W=2", "1"),
    ("W=0;K=1\n", None, "1"),  # systemd sets W to `0;K=1` and never K
    ("K=1;\n", "1;", "1"),
    ("K=good\nK=1;true\n", "1;true", unit_env.SH_SYNTAX),
    ("K='ab\ncd'\n", "ab\ncd", "ab\ncd"),
]


def _read_k(text: str, platform: str) -> str | None:
    values, skipped = unit_env.read_env_files([text], platform)
    if "K" in values:
        return values["K"]
    return dict(skipped).get("K")


@pytest.mark.parametrize(("text", "linux", "darwin"), VALUE_ROWS)
def test_a_value_loads_as_the_units_on_each_platform_read_it(text: str, linux: str | None, darwin: str | None) -> None:
    assert _read_k(text, "linux") == linux
    assert _read_k(text, "darwin") == darwin
    assert linux not in SKIP_REASONS  # systemd reads every value; only darwin's sh skips


@pytest.mark.parametrize("meta", list(";&|<>()"))
def test_an_unquoted_sh_metacharacter_is_skipped_on_darwin_and_loads_on_linux(meta: str) -> None:
    assert _read_k(f"K=a{meta}b\n", "linux") == f"a{meta}b"
    assert _read_k(f"K=a{meta}b\n", "darwin") == unit_env.SH_SYNTAX
    assert _read_k(f"K='a{meta}b'\n", "darwin") == f"a{meta}b"


def test_a_line_followed_by_a_command_keeps_the_earlier_value_and_says_so() -> None:
    values, skipped = unit_env.read_env_files(["V=good\nV=a b\n"], "darwin")
    assert values == {"V": "good"} and skipped == (("V", unit_env.SH_COMMAND),)
    values, skipped = unit_env.read_env_files(["V=good\n", "V=a b\n"], "darwin")  # across files too
    assert values == {"V": "good"} and skipped == (("V", unit_env.SH_COMMAND),)


def test_a_joined_line_sets_the_first_name_only() -> None:
    for platform in ("linux", "darwin"):
        values, _ = unit_env.read_env_files(["V=abc\\\nW=1\n"], platform)
        assert values == {"V": "abcW=1"}, platform


def test_an_open_quote_on_darwin_sets_nothing_from_its_line_on() -> None:
    values, skipped = unit_env.read_env_files(['A=1\nC=3 K="open\nB=2\n'], "darwin")
    assert values == {"A": "1"}
    assert skipped == (("C", unit_env.SH_OPEN_QUOTE), ("K", unit_env.SH_OPEN_QUOTE))


def test_a_name_that_is_not_a_plain_identifier_is_never_read() -> None:
    for platform in ("linux", "darwin"):
        values, skipped = unit_env.read_env_files(["QUJD+/ZXo=\nOK=1\n"], platform)
        assert (values, skipped) == ({"OK": "1"}, ())


def test_a_semicolon_then_assignments_sets_every_one_on_darwin() -> None:
    values, skipped = unit_env.read_env_files(["V=1;W=2;X=3\nY=4\n"], "darwin")
    assert (values, skipped) == ({"V": "1", "W": "2", "X": "3", "Y": "4"}, ())
    values, skipped = unit_env.read_env_files(["V=1; W=2\n"], "darwin")
    assert (values, skipped) == ({"V": "1", "W": "2"}, ())


def test_the_base_declared_names_include_the_forge_settings_the_upgrade_reads() -> None:
    from agent_tools import forge_forgejo

    assert frozenset(
        {*unit_env.ENV_KEYS, forge_forgejo.BASE_URL_ENV, forge_forgejo.DEFAULT_TOKEN_ENV}) == unit_env.DECLARED_BASE
    assert unit_env.declared_names() == unit_env.DECLARED_BASE


def test_a_kept_earlier_value_gets_one_note_that_says_both() -> None:
    values, skipped = unit_env.read_env_files(["V=good\nV=a b\nW=x y\n"], "darwin")
    load = unit_env.plan_load({}, values, skipped)
    assert load.loaded == {"V": "good"}
    notes = unit_env.load_notes(load, ["/u.env"], [], [], frozenset({"V", "W"}))
    assert notes == [
        f"env: loaded V from /u.env as an earlier line sets it; a later line: {unit_env.SH_COMMAND}",
        f"env: a line in /u.env did not set W: {unit_env.SH_COMMAND}",
    ]


def test_declared_names_are_env_keys_and_every_env_name_a_profile_declares() -> None:
    routing = {"forgejo_token_env": "MY_FORGE_TOKEN", "nested": [{"auth_env": "AUTH"}], "store": {"mode": "shared"}}
    provider = {"object_store": {"access_key_env": "GARAGE_ACCESS_KEY_ID", "secret_key_env": "GARAGE_SECRET_ACCESS_KEY",
                                 "endpoint": "http://x"}, "bad_env": "not a name"}
    assert unit_env.declared_names(routing, provider) == frozenset(
        {*unit_env.DECLARED_BASE, "MY_FORGE_TOKEN", "AUTH", "GARAGE_ACCESS_KEY_ID", "GARAGE_SECRET_ACCESS_KEY"})


def test_printable_names_prints_only_declared_names_and_counts_the_rest() -> None:
    declared = frozenset({"GARAGE_ACCESS_KEY_ID", "_X"})
    assert unit_env.printable_names(["GARAGE_ACCESS_KEY_ID", "_X"], declared) == "GARAGE_ACCESS_KEY_ID, _X"
    assert unit_env.printable_names(["GARAGE_ACCESS_KEY_ID", "MZXW6YTBOI", "lower"], declared) == (
        "GARAGE_ACCESS_KEY_ID and 2 other names")
    assert unit_env.printable_names(["MZXW6YTBOI"], declared) == "1 name"


@pytest.mark.parametrize("secret", ["MZXW6YTBOI", "dGVzdHNlY3JldEtleQ"])  # base32 upper case, base64 mixed case
def test_a_secret_pasted_on_its_own_line_is_counted_never_printed(secret: str) -> None:
    for platform in ("linux", "darwin"):
        values, skipped = unit_env.read_env_files([f"GARAGE_ACCESS_KEY_ID=k\n{secret}======\n"], platform)
        load = unit_env.plan_load({}, values, skipped)
        assert load.loaded == {"GARAGE_ACCESS_KEY_ID": "k", secret: "====="}
        notes = unit_env.load_notes(load, ["/u.env"], [], [], frozenset({"GARAGE_ACCESS_KEY_ID"}))
        assert notes == ["env: loaded GARAGE_ACCESS_KEY_ID and 1 other name from /u.env, as the units read it"]


def test_load_notes_name_files_and_variables_and_never_a_value() -> None:
    text = "K=file-secret\nN=n-secret\nQ=$q-secret\nM=a;secret\n"
    values, skipped = unit_env.read_env_files([text], "darwin")
    load = unit_env.plan_load({"K": "shell-secret"}, values, skipped)
    notes = unit_env.load_notes(load, ["/u.env"], ["/gone.env"], ["/bad.env"], frozenset({"K", "N", "Q", "M"}))
    assert notes == [
        "env: skipped /bad.env: not UTF-8 text",
        "env: /gone.env, which a unit names, does not exist",
        "env: loaded N from /u.env, as the units read it",
        "env: the shell's K override /u.env; unset them to use the file's",
        f"env: a line in /u.env did not set Q: {unit_env.SH_EXPANDS}",
        f"env: a line in /u.env did not set M: {unit_env.SH_SYNTAX}",
    ]
    assert not any("secret" in line for line in notes)
    assert unit_env.load_notes(unit_env.plan_load({}, {}), ["/d.env"], [], []) == []
