import argparse
import copy
import datetime
import json
from pathlib import Path
from typing import NamedTuple

import pytest

from agent_tools import cli, pacing, route

VALID_PROFILE = """\
team: acme
cartridges_dir: /opt/cartridges
skills_roots: [/opt/skills-a, /opt/skills-b]
provider_profile: /opt/providers/acme.yaml
harness_dir: /opt/coxswain-graphs
workspace_dir: /home/user/workspace
assume: y
"""


@pytest.mark.parametrize(
    "text, key, expected",
    [
        (VALID_PROFILE, "team", "acme"),
        (VALID_PROFILE, "skills_roots", ["/opt/skills-a", "/opt/skills-b"]),
        ("team: acme\ncartridges_dir: /opt/cartridges\n", "assume", "a"),
        ("team: acme\nassume:\n", "assume", "a"),
        # spec §1's own sample line, verbatim.
        ("assume: a          # gate answer for detached runs; drafts only ever land\n", "assume", "a"),
        (VALID_PROFILE + "ids: sequence\n", "ids", "sequence"),
        (VALID_PROFILE + "spend:\n  window_ceiling_usd: 300\n  node_cap_usd: 2.00\n", "window_ceiling_usd", 300.0),
        (VALID_PROFILE + "spend:\n  window_ceiling_usd: 300\n  node_cap_usd: 2.00\n", "node_cap_usd", 2.0),
        (VALID_PROFILE + "chair:\n  housekeeping_hours: 6\n", "chair", {"housekeeping_hours": 6.0}),
        (VALID_PROFILE + "chair:\n  idle_stall_minutes: 20\n", "chair", {"idle_stall_minutes": 20.0}),
    ],
)
def test_parse_profile_reads_key(text, key, expected):
    assert route.parse_profile(text)[key] == expected


@pytest.mark.parametrize(
    "text, key",
    [
        (VALID_PROFILE, "window_ceiling_usd"),
        (VALID_PROFILE, "node_cap_usd"),
        (VALID_PROFILE, "chair"),
        (VALID_PROFILE + "spend:\n  window_ceiling_usd: 300\n", "node_cap_usd"),
    ],
)
def test_parse_profile_omits_a_key_it_was_not_given(text, key):
    assert key not in route.parse_profile(text)


def test_parse_profile_unknown_key_warns_once_and_keeps_the_rest(capsys, monkeypatch):
    monkeypatch.setattr(route, "_WARNED_KEYS", set())
    text = "team: acme\nbogus_key: nope\nassume: y\n"
    first = route.parse_profile(text)
    second = route.parse_profile(text)
    third = route.parse_profile("other_key: 1\nteam: acme\n")
    assert first == second == {"team": "acme", "assume": "y"}
    assert third == {"team": "acme", "assume": "a"}
    assert capsys.readouterr().err == (
        "profile: ignoring unknown key bogus_key at line 2\n"
        "profile: ignoring unknown key other_key at line 1\n"
    )
    assert route.unknown_keys(text) == [("bogus_key", 2)]


def test_parse_profile_drops_an_unknown_key_with_an_indented_block(capsys, monkeypatch):
    monkeypatch.setattr(route, "_WARNED_KEYS", set())
    text = "team: acme\nfuture:\n  child: 1\n  other: x\nassume: y\n"
    assert route.parse_profile(text) == {"team": "acme", "assume": "y"}
    assert capsys.readouterr().err == "profile: ignoring unknown key future at line 2\n"
    assert route.unknown_keys(text) == [("future", 2)]


@pytest.mark.parametrize(
    "text, prefix, offending",
    [
        ("team: acme\nsources: not-json\n", "line 2:", ""),
        ("team: acme\n  nested: bad\n", "line 2:", "nested: bad"),
        (VALID_PROFILE + "spend:\n  bogus: 1\n", "line 9:", "bogus: 1"),
        (VALID_PROFILE + "spend:\n  window_ceiling_usd: 300 USD\n", "line 9:", "window_ceiling_usd: 300 USD"),
        (VALID_PROFILE + "chair:\n  bogus: 1\n", "line 9:", "bogus: 1"),
    ],
)
def test_parse_profile_error_names_the_line(text, prefix, offending):
    with pytest.raises(route.ProfileError) as exc_info:
        route.parse_profile(text)
    message = str(exc_info.value)
    assert message.startswith(prefix)
    assert offending in message


@pytest.mark.parametrize(
    "title, slug",
    [
        ("  Fix the Bug!! ", "fix-the-bug"),
        ("Café Résumé — draft", "caf-r-sum-draft"),
        ("multiple   spaces\tand\ttabs", "multiple-spaces-and-tabs"),
        ("a" * 60, "a" * 48),
        # Pinned, not asserted-away: callers using this as a path segment must
        # guard against the empty result themselves (see route.py docstring).
        ("!!!", ""),
        ("   ", ""),
        (
            "Fix the Loop A Build's Structured Output Was A Placeholder For Something Broken Today Okay",
            "build-s-structured-output-was-a-placeholder-for",
        ),
    ],
)
def test_slugify(title, slug):
    assert route.slugify(title) == slug


def test_surface_candidates_keeps_only_backticked_tokens_that_look_like_paths():
    body = (
        "Touch `agent_tools/schema.py` and `tests/test_release.py`, "
        "not `route status` or `https://example.com/file.py`."
    )
    assert route.surface_candidates(body, "/repos/widget") == [
        "agent_tools/schema.py",
        "tests/test_release.py",
    ]


@pytest.mark.parametrize(
    "existing, prefix, expected",
    [
        (["myinit-1", "myinit-2", "otherinit-1"], "myinit", "myinit-3"),
        (["myinit-1", "myinit-3", "otherinit-9"], "myinit", "myinit-4"),
        (["myinit-1.log", "myinit-7"], "myinit", "myinit-8"),
        ([], "freshinit", "freshinit-1"),
        # spec §4/§5: runs_dir holds <run-id>.log and <run-id>.pid, never a
        # bare id — a fixture of bare ids would hide a collision here.
        (["myinit-1.log", "myinit-1.pid", "myinit-2.log", "myinit-2.pid"], "myinit", "myinit-3"),
    ],
)
def test_next_run_id(existing, prefix, expected):
    assert route.next_run_id(existing, prefix) == expected


def test_initiative_files_returns_the_initiative_and_task_paths_and_text():
    files = route.initiative_files(
        "Fix the Bug", "Do the thing.", "git@example.com:acme/widget.git", phase="build"
    )
    assert set(files) == {
        "work/fix-the-bug/initiative.md",
        "work/fix-the-bug/build/fix-the-bug-task.md",
    }
    assert files["work/fix-the-bug/initiative.md"] == (
        "---\n"
        "id: fix-the-bug\n"
        "title: Fix the Bug\n"
        'repo: "git@example.com:acme/widget.git"\n'
        "---\n"
        "\n"
        "Do the thing.\n"
    )
    assert files["work/fix-the-bug/build/fix-the-bug-task.md"] == (
        "---\n"
        "id: fix-the-bug-task\n"
        "phase: build\n"
        "state: ready\n"
        "needs: []\n"
        "surfaces: []\n"
        "title: Fix the Bug\n"
        "---\n"
        "\n"
        "Do the thing.\n"
    )


@pytest.mark.parametrize(
    "title, line",
    [
        # Colon and `#` in the title: both branches of _yaml_scalar's quoting predicate.
        ("route.py: initiative_files #wip", 'title: "route.py: initiative_files #wip"'),
        # A title that merely looks like a YAML list is quoted, not treated as a raw literal.
        ("[draft] ship it", 'title: "[draft] ship it"'),
    ],
)
def test_initiative_files_quotes_a_title_that_would_read_as_yaml(title, line):
    slug = route.slugify(title)
    files = route.initiative_files(title, "Ship it.", "repo-url")
    assert line in files[f"work/{slug}/initiative.md"]
    assert line in files[f"work/{slug}/build/{slug}-task.md"]


@pytest.mark.parametrize(
    "call",
    [
        lambda: route.initiative_files("!!!", "body", "repo-url"),
        lambda: route.intake_file("!!!", "body", "repo-url", "2026-09-03"),
    ],
    ids=["initiative_files", "intake_file"],
)
def test_a_title_that_slugifies_to_empty_is_refused(call):
    # A bare "work/initiative.md" write is the wrong write into the work store.
    with pytest.raises(ValueError):
        call()


def test_initiative_files_writes_surfaces_budget_attempts_lint_and_a_given_slug():
    files = route.initiative_files(
        "Fix the Bug", "Do the thing.", "repo-url",
        surfaces=["a:b.py", "plain.py"], budget_usd=2.0, slug="custom-slug",
    )
    task_text = files["work/custom-slug/build/custom-slug-task.md"]
    assert 'surfaces: ["a:b.py", plain.py]' in task_text
    assert "budget_usd: 2.0" in task_text
    assert "attempts: []" in task_text
    assert "lint: []" in task_text


def test_initiative_files_defaults_phase_to_build_and_body_to_title():
    files = route.initiative_files("Fix the Bug", "", "repo-url")
    task_text = files["work/fix-the-bug/build/fix-the-bug-task.md"]
    assert "phase: build\n" in task_text
    assert task_text.endswith("\nFix the Bug\n")


def test_initiative_text_writes_id_title_repo_intake_and_body():
    text = route.initiative_text(
        "fix-thing", "Fix thing", "git@example.com:acme/widget.git", "intake/2026-09-04-fix-thing.md", "Do the thing.",
    )
    fields, body = route.parse_frontmatter(text)
    assert fields == {
        "id": "fix-thing",
        "title": "Fix thing",
        "repo": "git@example.com:acme/widget.git",
        "intake": "intake/2026-09-04-fix-thing.md",
    }
    assert body == "Do the thing."


def test_intake_file_with_ids_sequence_writes_the_allocated_id_and_title():
    files = route.intake_file(
        "Fix the Bug",
        "Do the thing.",
        "git@example.com:acme/widget.git",
        "2026-09-03",
        ids="sequence",
        allocate=lambda: "I7",
    )
    assert set(files) == {"intake/I7.md"}
    assert files["intake/I7.md"] == (
        "---\n"
        "id: I7\n"
        "title: Fix the Bug\n"
        "---\n"
        "\n"
        "Do the thing.\n"
    )


def test_intake_file_default_profile_still_slugifies():
    default = route.intake_file(
        "Fix the Bug", "Do the thing.", "git@example.com:acme/widget.git", "2026-09-03"
    )
    explicit_slug = route.intake_file(
        "Fix the Bug", "Do the thing.", "git@example.com:acme/widget.git", "2026-09-03", ids="slug"
    )
    assert default == explicit_slug == {
        "intake/2026-09-03-fix-the-bug.md": (
            "---\n"
            "id: fix-the-bug\n"
            "title: Fix the Bug\n"
            'repo: "git@example.com:acme/widget.git"\n'
            "---\n"
            "\n"
            "Do the thing.\n"
        )
    }


def test_intake_file_refuses_when_allocation_fails():
    def failing_allocate():
        raise ValueError("id allocation failed (exit 1): boom")

    with pytest.raises(ValueError, match="id allocation failed"):
        route.intake_file(
            "Fix the Bug",
            "Do the thing.",
            "git@example.com:acme/widget.git",
            "2026-09-03",
            ids="sequence",
            allocate=failing_allocate,
        )


def test_harness_argv_builds_the_epic_command_line():
    profile = route.parse_profile(VALID_PROFILE)
    argv = route.harness_argv(
        profile, "epic", "myinit-1", initiative="/work/myinit", repo="/repos/widget"
    )
    assert argv == [
        "/opt/coxswain-graphs/.venv/bin/python",
        "/opt/coxswain-graphs/shell.py",
        "epic",
        "--team",
        "acme",
        "--cartridges-dir",
        "/opt/cartridges",
        "--skills-root",
        "/opt/skills-a",
        "--skills-root",
        "/opt/skills-b",
        "--provider-profile",
        "/opt/providers/acme.yaml",
        "--runs-dir",
        "/home/user/workspace/runs",
        "--assume",
        "y",
        "--run-id",
        "myinit-1",
        "--initiative",
        "/work/myinit",
        "--repo",
        "/repos/widget",
        "--workdir",
        "/home/user/workspace",
    ]


def test_harness_argv_builds_the_decompose_command_line():
    profile = route.parse_profile(VALID_PROFILE)
    argv = route.harness_argv(
        profile,
        "decompose",
        "myidea-1",
        idea="/intake/myidea.md",
        initiative_id="myidea",
    )
    assert argv == [
        "/opt/coxswain-graphs/.venv/bin/python",
        "/opt/coxswain-graphs/shell.py",
        "decompose",
        "--team",
        "acme",
        "--cartridges-dir",
        "/opt/cartridges",
        "--skills-root",
        "/opt/skills-a",
        "--skills-root",
        "/opt/skills-b",
        "--provider-profile",
        "/opt/providers/acme.yaml",
        "--runs-dir",
        "/home/user/workspace/runs",
        "--assume",
        "y",
        "--run-id",
        "myidea-1",
        "--idea",
        "/intake/myidea.md",
        "--initiative-id",
        "myidea",
        "--workdir",
        "/home/user/workspace",
    ]


@pytest.mark.parametrize(
    "graph, needs, tail",
    [
        (
            "decompose",
            {"idea": "/intake/myidea.md", "initiative_id": "myidea", "task_ids": "ordinal"},
            ["--task-ids", "ordinal", "--workdir", "/home/user/workspace"],
        ),
        (
            "decompose",
            {"idea": "/intake/myidea.md", "initiative_id": "myidea", "repo": "/repos/widget"},
            ["--repo", "/repos/widget", "--workdir", "/home/user/workspace"],
        ),
        (
            "epic",
            {"initiative": "/work/myinit", "repo": "/repos/widget", "fix_attempts": 3},
            ["--fix-attempts", "3", "--workdir", "/home/user/workspace"],
        ),
        (
            "rescue",
            {"initiative": "/work/myinit", "repo": "/repos/widget", "task": "T1"},
            ["--task", "T1", "--workdir", "/home/user/workspace"],
        ),
    ],
)
def test_harness_argv_ends_with_the_graph_flags_then_workdir(graph, needs, tail):
    profile = route.parse_profile(VALID_PROFILE)
    argv = route.harness_argv(profile, graph, "x-1", **needs)
    assert argv[2] == graph
    assert argv[-4:] == tail


def test_harness_argv_builds_the_cos_command_line():
    profile = route.parse_profile(VALID_PROFILE)
    argv = route.harness_argv(profile, "cos", "cos-1")
    assert "cos" in argv
    assert "--runs-dir" in argv
    assert "--max-parallel" in argv
    assert "--initiative" not in argv


def test_harness_argv_appends_node_cap_usd_when_set_in_the_profile():
    profile = route.parse_profile(VALID_PROFILE + "spend:\n  node_cap_usd: 2.00\n")
    argv = route.harness_argv(profile, "cos", "cos-1")
    assert argv[-2:] == ["--node-cap-usd", "2.0"]


def test_harness_argv_omits_node_cap_usd_when_unset_in_the_profile():
    profile = route.parse_profile(VALID_PROFILE)
    argv = route.harness_argv(profile, "cos", "cos-1")
    assert "--node-cap-usd" not in argv


# The shape providers/claude-code.yaml actually has: tiers map a tier to a model,
# effort maps a tier to a thinking effort. The overlay is tested against it.
_PROFILE = {
    "command": "claude",
    "tiers": {"cheap": "haiku", "standard": "sonnet", "deep": "opus"},
    "effort": {"cheap": "low", "standard": "high", "deep": "high"},
    "budget_usd": {"cheap": 0.15, "standard": 0.35, "deep": 0.80},
}


@pytest.mark.parametrize(
    "tier, effort, tiers, efforts",
    [
        ("standard", None,
         {"cheap": "haiku", "standard": "sonnet", "deep": "sonnet"},
         {"cheap": "low", "standard": "high", "deep": "high"}),
        (None, "low",
         {"cheap": "haiku", "standard": "sonnet", "deep": "opus"},
         {"cheap": "low", "standard": "low", "deep": "low"}),
        ("cheap", "low",
         {"cheap": "haiku", "standard": "haiku", "deep": "haiku"},
         {"cheap": "low", "standard": "low", "deep": "low"}),
        ("deep", "high", _PROFILE["tiers"], _PROFILE["effort"]),
        (None, None, _PROFILE["tiers"], _PROFILE["effort"]),
    ],
)
def test_overlay_ceilings(tier, effort, tiers, efforts):
    before = copy.deepcopy(_PROFILE)
    # A ceiling is about model and effort: budgets and the rest pass through.
    assert route.overlay(_PROFILE, tier, effort) == {**_PROFILE, "tiers": tiers, "effort": efforts}
    assert before == _PROFILE


@pytest.mark.parametrize(
    "profile, tier, effort, fragments",
    [
        ({**_PROFILE, "tiers": {"cheap": "haiku", "standard": "sonnet"}}, "deep", None, ("deep", "standard")),
        ({**_PROFILE, "effort": {"cheap": "low", "standard": "low", "deep": "low"}}, None, "high", ("high", "low")),
        ({"command": "claude"}, "deep", None, ("unset",)),
        ({"tiers": {"ultra": "x"}}, "cheap", None, ("unset",)),
        ({"effort": "high"}, None, "low", ("unset",)),  # a scalar is not the profile's shape
    ],
)
def test_overlay_refuses_a_ceiling_the_profile_cannot_honour(profile, tier, effort, fragments):
    result = route.overlay(profile, tier, effort)
    assert isinstance(result, str)
    assert all(fragment in result for fragment in fragments)


def test_overlay_never_raises_on_off_ladder_values():
    profile = {"tiers": {"cheap": "haiku", "ultra": "x"}, "effort": {"cheap": "max"}}
    assert route.overlay(profile, "cheap", None)["tiers"] == {"cheap": "haiku", "ultra": "x"}
    assert "unset" in route.overlay(profile, None, "low")


@pytest.mark.parametrize(
    "environ, kwargs, expected",
    [
        (
            {"PATH": "/usr/bin:/bin", "HOME": "/home/user", "LANG": "C.UTF-8"},
            {"harness_bin": "/opt/coxswain-graphs/.venv/bin", "repo": "/repos/widget"},
            {
                "PATH": "/repos/widget/.venv/bin:/opt/coxswain-graphs/.venv/bin:/usr/bin:/bin",
                "HOME": "/home/user",
                "LANG": "C.UTF-8",
            },
        ),
        (
            {"PATH": "/usr/bin"},
            {"harness_bin": "/opt/coxswain-graphs/.venv/bin", "repo": ""},
            {"PATH": "/opt/coxswain-graphs/.venv/bin:/usr/bin"},
        ),
        (
            {"PATH": "/usr/bin"},
            {"harness_bin": "/opt/coxswain-graphs/.venv/bin", "trace_dir": "/runs/x-1-trace"},
            {"PATH": "/opt/coxswain-graphs/.venv/bin:/usr/bin", "AGENT_GRAPHS_TRACE_DIR": "/runs/x-1-trace"},
        ),
        ({"PATH": "/usr/bin"}, {}, {"PATH": "/usr/bin"}),
        (
            {"PATH": "/usr/bin", "AGENT_GRAPHS_TRACE_DIR": "/old"},
            {},
            {"PATH": "/usr/bin", "AGENT_GRAPHS_TRACE_DIR": "/old"},
        ),
        (
            {"PATH": "/usr/bin", "AGENT_GRAPHS_TRACE_DIR": "/old"},
            {"trace_dir": "/runs/x-1-trace"},
            {"PATH": "/usr/bin", "AGENT_GRAPHS_TRACE_DIR": "/runs/x-1-trace"},
        ),
        (
            {"PATH": "/usr/bin"},
            {"harness_bin": "/opt/coxswain-graphs/.venv/bin", "chair_model": "sonnet"},
            {"PATH": "/opt/coxswain-graphs/.venv/bin:/usr/bin", "COXSWAIN_CHAIR_MODEL": "sonnet"},
        ),
        (
            {"COXSWAIN_CHAIR_MODEL": "haiku"},
            {"chair_model": ""},
            {"COXSWAIN_CHAIR_MODEL": "haiku"},
        ),
        ({"PATH": "/usr/bin"}, {"chair_model": ""}, {"PATH": "/usr/bin"}),
        (
            {"PATH": "/usr/bin", "COXSWAIN_CHAIR_MODEL": "haiku"},
            {"harness_bin": "/h", "repo": "/r", "trace_dir": "/t", "chair_model": "opus"},
            {"PATH": "/r/.venv/bin:/h:/usr/bin", "AGENT_GRAPHS_TRACE_DIR": "/t", "COXSWAIN_CHAIR_MODEL": "opus"},
        ),
    ],
)
def test_child_env(environ, kwargs, expected):
    before = copy.deepcopy(environ)
    assert route.child_env(environ, **kwargs) == expected
    assert environ == before


def test_child_env_exports_the_variable_name_the_fixture_declares():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "chair_model_env.json").read_text())
    assert route.child_env({}, chair_model="sonnet") == {fixture["env_name"]: "sonnet"}


FIXTURE_PROFILE = {
    "team": "acme",
    "cartridges_dir": "/opt/cartridges",
    "skills_roots": ["/opt/skills-a", "/opt/skills-b"],
    "provider_profile": "/opt/providers/acme.yaml",
    "harness_dir": "/opt/coxswain-graphs",
    "workspace_dir": "/home/user/workspace",
    "assume": "a",
}

FIXTURE_INTAKE = {
    "queued": [{"id": "ship-it", "title": "ship it"}],
    "decomposed": [{"id": "fix-ci", "title": "fix ci"}],
    "landed": [],
}

EMPTY_INTAKE_GROUPS = {"queued": [], "decomposed": [], "landed": []}

FIXTURE_RUNS = [
    {"id": "widget-1", "pid": 4242, "alive": True, "started": "14:02"},
]

FIXTURE_INITIATIVES = [
    {"id": "widget", "phase": "build", "ready": 3},
]


def test_render_context_with_profile_matches_spec_layout():
    text = route.render_context(
        FIXTURE_PROFILE, FIXTURE_INTAKE, FIXTURE_RUNS, FIXTURE_INITIATIVES
    )
    assert text == (
        "routing: team acme; work requests go through the route-work skill, "
        "questions stay inline\n"
        "intake: 1 queued, 1 decomposed, 0 landed\n"
        "lanes: 1 busy — widget-1 (pid 4242, since 14:02)\n"
        "ready: widget (3 tasks ready in phase build)"
    )


def test_render_context_with_profile_and_no_activity():
    text = route.render_context(FIXTURE_PROFILE, EMPTY_INTAKE_GROUPS, [], [])
    assert text == (
        "routing: team acme; work requests go through the route-work skill, "
        "questions stay inline\n"
        "intake: 0 queued, 0 decomposed, 0 landed\n"
        "lanes: all clear\n"
        "ready: none"
    )


def test_render_context_appends_the_gate_line_when_given():
    text = route.render_context(FIXTURE_PROFILE, EMPTY_INTAKE_GROUPS, [], [], gate_level="phase")
    assert text.splitlines()[-1] == "gate: phase"


def test_efficiency_line_states_spend_lands_cost_per_land_and_first_try_rate():
    assert route.efficiency_line(3.0, 4, 0.75) == "efficiency: today $3.00 for 4 landed ($0.75 per landed task), first-try 75%"


def test_efficiency_line_with_no_lands_says_nothing_landed_yet():
    assert route.efficiency_line(1.234, 0, 0.0) == "efficiency: today $1.23, nothing landed yet"


def test_efficiency_line_with_no_store_is_none():
    assert route.efficiency_line(None, 0, 0.0) is None


def test_efficiency_line_with_no_measured_task_says_first_try_unknown():
    assert route.efficiency_line(2.0, 1, None, 0) == "efficiency: today $2.00 for 1 landed ($2.00 per landed task), first-try unknown"


def test_efficiency_line_names_how_many_tasks_the_rate_covers_when_some_are_unmeasured():
    assert route.efficiency_line(3.0, 3, 0.5, 2) == "efficiency: today $3.00 for 3 landed ($1.00 per landed task), first-try 50% of 2 measured"


def test_render_context_puts_the_efficiency_line_after_lanes():
    line = "efficiency: today $0.00, nothing landed yet"
    text = route.render_context(FIXTURE_PROFILE, EMPTY_INTAKE_GROUPS, [], [], efficiency=line)
    assert text.splitlines()[2:5] == ["lanes: all clear", line, "ready: none"]


def test_render_context_without_an_efficiency_line_is_unchanged():
    assert "efficiency" not in route.render_context(FIXTURE_PROFILE, EMPTY_INTAKE_GROUPS, [], [])


LAND_LOG = "\n".join([
    '{"ts": "2026-09-25T01:00:00+00:00", "run": "r-1", "task": "t1", "steps_reached": ["build", "mark_done"], "exit": 0}',
    '{"ts": "2026-09-25T02:00:00+00:00", "run": "r-1", "task": "t2", "steps_reached": ["build"], "exit": 0}',
    '{"ts": "2026-09-25T03:00:00+00:00", "run": "r-1", "task": "t3", "steps_reached": ["mark_done"], "exit": 1}',
    '{"ts": "2026-09-24T23:00:00+00:00", "run": "r-1", "task": "t4", "steps_reached": ["mark_done"], "exit": 0}',
    "not json",
    "[1, 2]",
    '{"ts": "2026-09-25T04:00:00+00:00", "run": "r-2", "task": null, "steps_reached": ["mark_done"], "exit": 0}',
])


def test_landed_today_keeps_only_clean_mark_done_lines_of_the_day_and_skips_garbage():
    assert route.landed_today(LAND_LOG, "2026-09-25") == [("r-1", "t1"), ("r-2", None)]


def test_landed_today_ignores_a_line_whose_steps_reached_is_a_string_not_a_list():
    line = '{"ts": "2026-09-25T01:00:00+00:00", "run": "r-1", "task": "t1", "steps_reached": "mark_done", "exit": 0}'
    assert route.landed_today(line, "2026-09-25") == []


def test_landed_today_counts_a_task_logged_twice_once():
    line = '{"ts": "2026-09-25T01:00:00+00:00", "run": "r-1", "task": "t1", "steps_reached": ["mark_done"], "exit": 0}'
    assert route.landed_today(f"{line}\n{line}", "2026-09-25") == [("r-1", "t1")]


def test_landed_today_counts_two_lines_with_no_task_twice():
    line = '{"ts": "2026-09-25T01:00:00+00:00", "run": "r-1", "task": null, "steps_reached": ["mark_done"], "exit": 0}'
    assert route.landed_today(f"{line}\n{line}", "2026-09-25") == [("r-1", None), ("r-1", None)]


def test_a_composite_with_a_truncated_head_belongs_to_the_one_ticket_it_prefixes():
    assert route.task_owner("epic-x-5:seams:seams-t", "epic-x-5", {"seams-task", "wires-task"}) == "seams-task"


def test_a_bare_task_id_belongs_to_the_ticket_it_names():
    assert route.task_owner("seams-task", "epic-x-5", {"seams-task", "seams-task-two"}) == "seams-task"


def test_a_composite_from_another_run_belongs_to_no_ticket():
    assert route.task_owner("epic-x-4:seams:seams-t", "epic-x-5", {"seams-task"}) is None


def test_a_composite_whose_head_prefixes_two_landed_tickets_belongs_to_neither():
    assert route.task_owner("epic-x-5:seams:seams-task", "epic-x-5", {"seams-task", "seams-task-two"}) is None


def test_a_composite_whose_head_names_only_the_longer_ticket_belongs_to_it():
    assert route.task_owner("epic-x-5:seams:seams-task-two", "epic-x-5", {"seams-task", "seams-task-two"}) == "seams-task-two"


def test_first_try_is_the_share_of_measured_tasks_with_one_build_call_and_leaves_unmeasured_ones_out():
    landed = [("r-1", "alpha"), ("r-1", "beta"), ("r-1", "gamma"), ("r-2", None)]
    assert route.first_try(landed, {"r-1:build:alpha": 1, "r-1:build:bet": 2}) == (0.5, 2)


def test_first_try_never_gives_a_sibling_ticket_the_calls_of_its_shorter_named_neighbour():
    landed = [("r-1", "seams-task"), ("r-1", "seams-task-two")]
    assert route.first_try(landed, {"r-1:p:seams-task": 1, "r-1:p:seams-task-two": 2}) == (0.0, 1)


def test_first_try_with_nothing_measured_is_an_unknown_share():
    assert route.first_try([("r-1", "alpha")], {}) == (None, 0)


def test_render_context_without_profile_is_the_one_liner():
    text = route.render_context(None, [], [], [])
    assert text == (
        "routing: no profile at ~/.config/agent-tools/profile.yaml; the "
        "harness is not configured on this machine"
    )


def test_context_document_keys_on_exactly_the_profile_fields():
    doc = route.context_document(
        FIXTURE_PROFILE, FIXTURE_INTAKE, FIXTURE_RUNS, FIXTURE_INITIATIVES
    )
    assert doc["team"] == "acme"
    assert doc["cartridges_dir"] == "/opt/cartridges"
    assert doc["skills_roots"] == ["/opt/skills-a", "/opt/skills-b"]
    assert doc["provider_profile"] == "/opt/providers/acme.yaml"
    assert doc["harness_dir"] == "/opt/coxswain-graphs"
    assert doc["workspace_dir"] == "/home/user/workspace"
    assert doc["assume"] == "a"
    assert doc["intake"] == FIXTURE_INTAKE
    assert doc["runs"] == FIXTURE_RUNS
    assert doc["initiatives"] == FIXTURE_INITIATIVES
    assert json.loads(json.dumps(doc)) == doc


FIXTURE_RUNS_MIXED = [
    {"id": "widget-1", "pid": 4242, "alive": True, "started": "14:02"},
    {"id": "widget-2", "pid": 4243, "alive": False, "started": "13:02"},
    {"id": "widget-3", "pid": 4244, "alive": False, "started": "12:02"},
]


def test_render_context_counts_only_alive_runs_in_flight():
    text = route.render_context(
        FIXTURE_PROFILE, FIXTURE_INTAKE, FIXTURE_RUNS_MIXED, FIXTURE_INITIATIVES
    )
    lines = text.splitlines()
    assert "lanes: 1 busy — widget-1 (pid 4242, since 14:02)" in lines
    assert "widget-2" not in text
    assert "widget-3" not in text


def test_render_context_all_exited_reports_zero_in_flight():
    runs = [
        {"id": "widget-2", "pid": 4243, "alive": False, "started": "13:02"},
        {"id": "widget-3", "pid": 4244, "alive": False, "started": "12:02"},
    ]
    text = route.render_context(FIXTURE_PROFILE, FIXTURE_INTAKE, runs, FIXTURE_INITIATIVES)
    lines = text.splitlines()
    assert "lanes: all clear" in lines
    assert "widget-2" not in text
    assert "widget-3" not in text


def test_the_docket_lanes_line_for_none_and_for_two_busy_lanes():
    def lanes_line(runs):
        text = route.render_context(FIXTURE_PROFILE, EMPTY_INTAKE_GROUPS, runs, [])
        return next(l for l in text.splitlines() if l.startswith("lanes:"))

    two = [
        {"id": "widget-1", "pid": 4242, "alive": True, "started": "14:02"},
        {"id": "widget-2", "pid": 4243, "alive": True, "started": "13:02"},
    ]
    assert lanes_line([]) == "lanes: all clear"
    assert lanes_line(two) == (
        "lanes: 2 busy — widget-1 (pid 4242, since 14:02), widget-2 (pid 4243, since 13:02)"
    )


def test_context_document_adds_a_live_count_of_alive_runs():
    doc = route.context_document(
        FIXTURE_PROFILE, FIXTURE_INTAKE, FIXTURE_RUNS_MIXED, FIXTURE_INITIATIVES
    )
    assert doc["live"] == 1
    assert doc["runs"] == FIXTURE_RUNS_MIXED


def test_context_document_without_profile_has_the_same_keys_as_none():
    doc = route.context_document(None, [], [], [])
    for field in (
        "team",
        "cartridges_dir",
        "skills_roots",
        "provider_profile",
        "harness_dir",
        "workspace_dir",
        "assume",
    ):
        assert doc[field] is None
    assert doc["intake"] == []
    assert doc["runs"] == []
    assert doc["initiatives"] == []
    assert json.loads(json.dumps(doc)) == doc


def test_status_rows_covers_live_dead_and_no_pidfile_runs_in_order():
    entries = [
        {
            "id": "widget-1",
            "pid": 4242,
            "alive": True,
            "started": "14:02",
            "quarantined": [],
            "reused": [],
            "summary": None,
            "usage": None,
        },
        {
            "id": "widget-2",
            "pid": 4300,
            "alive": False,
            "started": "13:10",
            "quarantined": ["quarantined task foo"],
            "reused": [],
            "summary": "epic done",
            "usage": "usage $0.42",
        },
        {"id": "widget-3", "pid": None, "alive": None, "started": None},
    ]
    rows = route.status_rows(entries)
    assert [r["id"] for r in rows] == ["widget-1", "widget-2", "widget-3"]
    assert rows[0] == {
        "id": "widget-1",
        "pid": 4242,
        "state": "alive",
        "started": "14:02",
        "quarantined": [],
        "reused": [],
        "summary": None,
        "usage": None,
        "short_id": None,
    }
    assert rows[1] == {
        "id": "widget-2",
        "pid": 4300,
        "state": "exited",
        "started": "13:10",
        "quarantined": ["quarantined task foo"],
        "reused": [],
        "summary": "epic done",
        "usage": "usage $0.42",
        "short_id": None,
    }
    assert rows[2] == {
        "id": "widget-3",
        "pid": None,
        "state": "no pidfile",
        "started": None,
        "quarantined": [],
        "reused": [],
        "summary": None,
        "usage": None,
        "short_id": None,
    }


def test_status_rows_carries_short_id_from_the_dict_and_none_when_absent():
    entries = [{"id": "widget-1", "pid": None}, {"id": "widget-2", "pid": None}]
    rows = route.status_rows(entries, {"widget-1": "w1a"})
    assert [r["short_id"] for r in rows] == ["w1a", None]


def test_render_status_shows_the_short_id_beside_the_slug():
    rows = route.status_rows([{"id": "widget-1", "pid": 4242, "alive": True, "started": "14:02"}])
    assert route.render_status(rows, short_ids={"widget-1": "w1a"}) == "widget-1 [w1a]: alive (pid 4242, started 14:02)"


def test_render_status_shows_the_slug_alone_without_a_short_id():
    rows = route.status_rows([{"id": "widget-3", "pid": None}])
    assert route.render_status(rows, short_ids={"widget-1": "w1a"}) == "widget-3: no pidfile"


def test_parse_frontmatter_returns_empty_fields_when_there_is_no_leading_fence():
    text = "Just a plain file.\nNo frontmatter here.\n"
    assert route.parse_frontmatter(text) == ({}, text)


def test_parse_frontmatter_returns_empty_fields_when_the_fence_never_closes():
    text = "---\ntitle: Fix the Bug\nNo closing fence follows.\n"
    assert route.parse_frontmatter(text) == ({}, text)


@pytest.mark.parametrize(
    "title",
    [
        "Fix the Bug",
        "route.py: initiative_files #wip",
        "  Fix the Bug  ",
        "[draft] ship it",
        '"draft" ship it',
    ],
)
def test_parse_frontmatter_round_trips_every_title_shape_initiative_files_can_emit(title):
    files = route.initiative_files(title, "Ship it.", "repo-url")
    task_text = next(v for k, v in files.items() if k.endswith(".md") and "/build/" in k)
    fields, _ = route.parse_frontmatter(task_text)
    assert fields["title"] == title
    assert fields["needs"] == []
    assert fields["surfaces"] == []


@pytest.mark.parametrize(
    "title",
    [
        "Fix the Bug",
        "route.py: initiative_files #wip",
        "  Fix the Bug  ",
        "[draft] ship it",
        '"draft" ship it',
    ],
)
def test_parse_frontmatter_round_trips_every_title_shape_intake_file_can_emit(title):
    files = route.intake_file(title, "Ship it.", "repo-url", "2026-09-04")
    text = next(iter(files.values()))
    fields, _ = route.parse_frontmatter(text)
    assert fields["title"] == title


def test_parse_frontmatter_splits_a_non_empty_list_on_commas():
    # The contract's list form is "flat list of comma-separated bare
    # items", not just the `[]` case every needs/surfaces assertion above
    # exercises. Returning `[inner]` in place of the comma split keeps
    # every other list assertion in this file green, so this is the one
    # test that pins the split and the per-item strip.
    text = "---\nneeds: [a, b]\n---\n\nx\n"
    fields, _ = route.parse_frontmatter(text)
    assert fields["needs"] == ["a", "b"]


def test_parse_frontmatter_block_list_matches_flow_list():
    flow_text = "---\nneeds: [a, b]\nsurfaces: [c]\n---\n\nx\n"
    block_text = "---\nneeds:\n  - a\n  - b\nsurfaces:\n  - c\n---\n\nx\n"
    flow_fields, _ = route.parse_frontmatter(flow_text)
    block_fields, _ = route.parse_frontmatter(block_text)
    assert flow_fields == block_fields


def test_parse_frontmatter_block_list_does_not_swallow_the_next_key():
    text = "---\nneeds:\n  - a\ntitle: x\n---\n\nbody\n"
    fields, _ = route.parse_frontmatter(text)
    assert fields["needs"] == ["a"]
    assert fields["title"] == "x"


def test_parse_frontmatter_empty_block_yields_empty_list():
    text = "---\nneeds:\ntitle: x\n---\n\nbody\n"
    fields, _ = route.parse_frontmatter(text)
    assert fields["needs"] == []
    assert fields["title"] == "x"


def test_parse_frontmatter_round_trips_an_empty_string_field():
    # `title`'s own empty string is unreachable through these builders —
    # `_slug_or_raise` refuses it before any frontmatter is written — but
    # `repo` carries no such guard, so it is what exercises _yaml_scalar's
    # `value == ""` branch end to end here.
    files = route.initiative_files("Fix the Bug", "Ship it.", "")
    initiative_text = files["work/fix-the-bug/initiative.md"]
    fields, _ = route.parse_frontmatter(initiative_text)
    assert fields["repo"] == ""


def test_parse_frontmatter_recovers_the_body_after_the_closing_fence():
    # Exercises the blank-line strip and the trailing-newline strip on
    # their own: swapping either for a bare `after_closing` return still
    # passes every test above (they only check `fields`), so this is the
    # one assertion that would catch that regression.
    files = route.initiative_files("Fix the Bug", "Ship it.", "repo-url")
    task_text = files["work/fix-the-bug/build/fix-the-bug-task.md"]
    _, body = route.parse_frontmatter(task_text)
    assert body == "Ship it."


def test_parse_frontmatter_never_raises_on_a_header_line_with_no_colon_space():
    # A stray line inside the fence with no `": "` separator — a bad hand
    # edit, a YAML block-list item, a bare comment — is not a line this
    # module ever wrote. The contract says parse_frontmatter never raises
    # on any input, not just well-formed frontmatter, and the next phase's
    # list builders will read whatever is actually on disk.
    text = "---\nnotes\nid: x\n---\n\nbody\n"
    fields, body = route.parse_frontmatter(text)
    assert fields == {"id": "x"}
    assert body == "body"


def test_parse_frontmatter_misparses_a_bare_value_that_happens_to_start_with_a_quote():
    # Parser-side property, not a writer guard: this never calls
    # initiative_files or _yaml_scalar. It hand-builds the frontmatter line
    # the writer would emit for a `"`-leading title *if* it did not quote
    # it, to show why the writer must — parse_frontmatter has no way to
    # tell that unquoted value apart from a real quoted scalar, so it
    # slices the wrong substring back out. The actual guard against a
    # writer regression is the '"draft" ship it' case in the two
    # parametrised round-trip tests above: revert the _yaml_scalar
    # addition and the writer emits this same bare line, so that case's
    # `fields["title"] == title` assertion fails.
    bare_line = 'title: "draft" ship it\n'
    text = f"---\nid: x\n{bare_line}---\n\nShip it.\n"
    fields, _ = route.parse_frontmatter(text)
    assert fields["title"] != '"draft" ship it'


def test_intake_entries_sorts_by_filename_and_skips_consumed_prefix():
    # Filenames supplied out of order and one already-consumed file: the
    # sort and the skip are both live rules, not accidents of dict order.
    files = {
        "2026-09-03-later.md": "---\nid: later\ntitle: Later\n---\n\nBody.\n",
        "2026-09-01-earlier.md": "---\nid: earlier\ntitle: Earlier\n---\n\nBody.\n",
        "consumed/2026-08-01-old.md": "---\nid: old\ntitle: Old\n---\n\nBody.\n",
    }
    entries = route.intake_entries(files)
    assert entries == [
        {"id": "earlier", "title": "Earlier", "initiative": None, "done": False, "path": "intake/2026-09-01-earlier.md"},
        {"id": "later", "title": "Later", "initiative": None, "done": False, "path": "intake/2026-09-03-later.md"},
    ]


def test_intake_entries_falls_back_to_stem_and_first_body_line_when_frontmatter_is_absent():
    files = {"2026-09-02-no-frontmatter.md": "\n\nFirst real line.\nSecond line.\n"}
    entries = route.intake_entries(files)
    assert entries == [
        {"id": "2026-09-02-no-frontmatter", "title": "First real line.", "initiative": None, "done": False,
         "path": "intake/2026-09-02-no-frontmatter.md"}
    ]


def test_intake_entries_reads_the_initiative_field_and_the_done_prefix():
    files = {"done/2026-09-01-old.md": "---\nid: old\ntitle: Old\ninitiative: old\n---\n\nBody.\n"}
    entries = route.intake_entries(files)
    assert entries == [
        {"id": "old", "title": "Old", "initiative": "old", "done": True, "path": "intake/2026-09-01-old.md"}
    ]


def test_run_entries_sorts_by_id_and_treats_a_non_integer_pidfile_as_dead():
    # Ids supplied out of order and one pidfile whose text is not an
    # integer — a partial write — must come back pid None, alive False even
    # though the caller's own `alive` map says True.
    pids = {"r2": "222", "r1": "not-a-pid"}
    alive = {"r1": True, "r2": True}
    started = {"r1": "2026-09-04T00:00:00", "r2": "2026-09-04T00:01:00"}
    entries = route.run_entries(pids, alive, started)
    assert entries == [
        {"id": "r1", "pid": None, "alive": False, "started": "2026-09-04T00:00:00", "heartbeat": None,
         "host": None, "remote": False},
        {"id": "r2", "pid": 222, "alive": True, "started": "2026-09-04T00:01:00", "heartbeat": None,
         "host": None, "remote": False},
    ]


def test_run_entries_carries_a_heartbeat_only_for_the_runs_the_caller_names():
    entries = route.run_entries({"r1": "1", "r2": "2"}, {}, {}, {"r2": "2026-09-25T10:38:12Z"})
    assert [e["heartbeat"] for e in entries] == [None, "2026-09-25T10:38:12Z"]


_LEASED_RUN = {"id": "widget-1", "pid": 4242, "alive": True, "started": "2026-09-25T10:38:00+00:00",
               "heartbeat": "2026-09-25T10:40:00Z"}


@pytest.fixture
def utc_plus_2(monkeypatch):
    import time

    monkeypatch.setenv("TZ", "Etc/GMT-2")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _lanes(run, now):
    text = route.render_context(FIXTURE_PROFILE, EMPTY_INTAKE_GROUPS, [run], [], now=now)
    return next(l for l in text.splitlines() if l.startswith("lanes:"))


def test_a_lane_with_a_heartbeat_reads_since_and_seconds_ago(utc_plus_2):
    assert _lanes(_LEASED_RUN, "2026-09-25T10:40:12Z") == "lanes: 1 busy — widget-1 (since 12:38, heartbeat 12s ago)"


def test_a_lane_with_a_heartbeat_reads_minutes_ago_from_sixty_seconds(utc_plus_2):
    assert _lanes(_LEASED_RUN, "2026-09-25T10:43:00Z").endswith("heartbeat 3m ago)")
    assert _lanes(_LEASED_RUN, "2026-09-25T10:40:59Z").endswith("heartbeat 59s ago)")
    assert _lanes(_LEASED_RUN, "2026-09-25T10:41:00Z").endswith("heartbeat 1m ago)")


def test_a_lane_without_a_heartbeat_keeps_the_pid_form_with_local_since(utc_plus_2):
    run = {**_LEASED_RUN, "heartbeat": None}
    assert _lanes(run, "2026-09-25T10:43:00Z") == "lanes: 1 busy — widget-1 (pid 4242, since 12:38)"


class _FakeLane(NamedTuple):
    run: str
    host: str | None
    launched_at: str
    heartbeat_at: str


_REMOTE_LANE = _FakeLane("widget-2", "host-b", "2026-09-25T10:38:00+00:00", "2026-09-25T10:40:00Z")


def test_a_remote_lane_becomes_an_entry_with_its_host():
    assert route.remote_lane_entries([_REMOTE_LANE]) == [
        {"id": "widget-2", "pid": None, "alive": True, "started": "2026-09-25T10:38:00+00:00",
         "heartbeat": "2026-09-25T10:40:00Z", "host": "host-b", "remote": True}
    ]


def test_a_remote_lane_with_no_host_keeps_host_none_and_remote_true():
    entry = route.remote_lane_entries([_REMOTE_LANE._replace(host=None)])[0]
    assert (entry["host"], entry["remote"]) == (None, True)


def test_a_local_entry_reads_remote_false_and_host_none():
    entry = route.run_entries({"r1": "7"}, {"r1": True}, {"r1": "2026-09-25T10:38:00+00:00"})[0]
    assert (entry["host"], entry["remote"]) == (None, False)


def test_local_entries_come_first_then_remote_entries_in_input_order():
    local = route.run_entries({"b": "1", "a": "2"}, {}, {})
    remote = route.remote_lane_entries([_REMOTE_LANE._replace(run="z"), _REMOTE_LANE._replace(run="y")])
    assert [e["id"] for e in local + remote] == ["a", "b", "z", "y"]
    assert [e["remote"] for e in local + remote] == [False, False, True, True]


def test_a_remote_lane_line_names_its_host(utc_plus_2):
    entry = route.remote_lane_entries([_REMOTE_LANE])[0]
    assert _lanes(entry, "2026-09-25T10:40:12Z") == "lanes: 1 busy — widget-2 (on host-b, since 12:38, heartbeat 12s ago)"


def test_a_remote_lane_line_without_a_host_says_another_machine(utc_plus_2):
    entry = route.remote_lane_entries([_REMOTE_LANE._replace(host=None)])[0]
    assert _lanes(entry, "2026-09-25T10:43:00Z") == (
        "lanes: 1 busy — widget-2 (on another machine, since 12:38, heartbeat 3m ago)"
    )


def test_initiative_summaries_picks_the_sorted_first_phase_with_a_ready_task():
    # Two phases both hold a ready, unblocked task; the alphabetically
    # first phase name must win, not first-in-list or last-in-list.
    items = [
        {"id": "a1", "initiative": "alpha", "phase": "zzz-later", "state": "ready", "needs": []},
        {"id": "a2", "initiative": "alpha", "phase": "aaa-first", "state": "ready", "needs": []},
    ]
    summaries = route.initiative_summaries(items)
    assert summaries == [{"id": "alpha", "phase": "aaa-first", "ready": 1}]


def test_initiative_summaries_excludes_a_task_whose_needs_are_not_all_done():
    # The one ready task in this initiative needs a task that is not done,
    # so it must not be counted, and the initiative must not appear at all.
    items = [
        {"id": "b1", "initiative": "beta", "phase": "build", "state": "ready", "needs": ["b0"]},
        {"id": "b0", "initiative": "beta", "phase": "build", "state": "in_progress", "needs": []},
    ]
    assert route.initiative_summaries(items) == []


def test_initiative_summaries_ready_is_a_count_not_a_flag():
    # Two ready, unblocked tasks share the winning phase and a third ready
    # task sits in a losing phase; a literal 1 in place of a real count
    # would pass every other case in this file but fails here.
    items = [
        {"id": "g1", "initiative": "gamma", "phase": "build", "state": "ready", "needs": []},
        {"id": "g2", "initiative": "gamma", "phase": "build", "state": "ready", "needs": []},
        {"id": "g3", "initiative": "gamma", "phase": "later", "state": "ready", "needs": []},
    ]
    summaries = route.initiative_summaries(items)
    assert summaries == [{"id": "gamma", "phase": "build", "ready": 2}]


def test_initiative_summaries_a_dropped_item_satisfies_a_dependents_needs_and_is_never_itself_ready():
    # d0 is dropped, terminal but never "ready"; d2 needs both d0 and d1
    # (done) and must come back ready — the dropped item unblocks it without
    # ever counting as a ready task itself.
    items = [
        {"id": "d0", "initiative": "delta", "phase": "build", "state": "dropped", "needs": []},
        {"id": "d1", "initiative": "delta", "phase": "build", "state": "done", "needs": []},
        {"id": "d2", "initiative": "delta", "phase": "build", "state": "ready", "needs": ["d0", "d1"]},
    ]
    assert route.initiative_summaries(items) == [{"id": "delta", "phase": "build", "ready": 1}]


_HELD = {"id": "cli", "initiative": "sync", "phase": "2-edge", "state": "blocked", "needs": [], "file": "2-edge/cli.md"}
_HOOKS = {"id": "hooks", "initiative": "sync", "phase": "3-hooks", "state": "ready", "needs": [], "file": "3-hooks/hooks.md"}


def test_initiative_summaries_omits_an_initiative_whose_only_ready_task_sits_behind_a_blocked_phase():
    assert route.initiative_summaries([_HELD, _HOOKS]) == []


def test_initiative_summaries_omits_a_ready_task_that_needs_a_blocked_item():
    same_phase = {**_HOOKS, "phase": "2-edge", "needs": ["cli"]}
    assert route.initiative_summaries([_HELD, same_phase]) == []


def test_initiative_summaries_keeps_an_initiative_with_one_ready_task_not_behind_a_blocked_item():
    early = {"id": "e", "initiative": "sync", "phase": "1-first", "state": "ready", "needs": [], "file": "1-first/e.md"}
    assert route.initiative_summaries([_HELD, _HOOKS, early]) == [{"id": "sync", "phase": "1-first", "ready": 1}]


def test_launch_blockers_names_the_blocked_file_a_ready_task_sits_behind():
    assert route.launch_blockers([_HELD, _HOOKS]) == ["2-edge/cli.md"]


def test_launch_blockers_is_empty_with_no_blocked_item_or_no_ready_task():
    assert route.launch_blockers([_HOOKS]) == []
    assert route.launch_blockers([_HELD]) == []


def test_launch_blockers_ignores_a_blocked_item_in_a_later_phase():
    late = {**_HELD, "phase": "4-late", "file": "4-late/cli.md"}
    assert route.launch_blockers([late, _HOOKS]) == []


def test_initiative_summaries_sorts_rows_by_initiative_id():
    # Six initiatives, each with one ready, unblocked task, supplied in
    # reverse order. Grouping them with a bare set (no `sorted`) would come
    # back in whatever order Python's per-process hash randomisation gives
    # a six-element string set, which matches this exact ascending order by
    # chance on 1 run in 720 — this is a real pin, not a coin flip.
    items = [
        {"id": f"{name[0]}1", "initiative": name, "phase": "build", "state": "ready", "needs": []}
        for name in ["zulu", "yankee", "xray", "whiskey", "victor", "alpha"]
    ]
    summaries = route.initiative_summaries(items)
    assert [row["id"] for row in summaries] == [
        "alpha", "victor", "whiskey", "xray", "yankee", "zulu",
    ]


def test_state_problems_names_the_initiative_file_and_bad_value():
    items = [
        {"initiative": "alpha", "file": "1-build/t1.md", "state": "superseded"},
        {"initiative": "alpha", "file": "1-build/t2.md", "state": "ready"},
    ]
    assert route.state_problems(items) == [
        "alpha: 1-build/t1.md: unknown state 'superseded'",
    ]


def test_initiative_summaries_an_unknown_state_makes_the_initiative_unlaunchable():
    # e1 is otherwise ready and unblocked, but e0's bad state must zero out
    # the whole initiative, not just skip e0.
    items = [
        {"id": "e0", "initiative": "epsilon", "phase": "build", "state": "superseded",
         "needs": [], "file": "build/e0.md"},
        {"id": "e1", "initiative": "epsilon", "phase": "build", "state": "ready", "needs": []},
    ]
    assert route.initiative_summaries(items) == [{"id": "epsilon", "phase": None, "ready": 0}]


def test_initiative_summaries_a_quarantined_item_is_known_and_holds_only_its_dependents():
    # The store writes `quarantined` (graphs #699): its sibling stays ready, its dependent waits.
    items = [
        {"id": "q0", "initiative": "quebec", "phase": "build", "state": "quarantined", "needs": []},
        {"id": "q1", "initiative": "quebec", "phase": "build", "state": "ready", "needs": []},
        {"id": "q2", "initiative": "quebec", "phase": "wire", "state": "ready", "needs": ["q0"]},
    ]
    assert route.state_problems([{**i, "file": f"{i['id']}.md"} for i in items]) == []
    assert route.initiative_summaries(items) == [{"id": "quebec", "phase": "build", "ready": 1}]


def test_initiative_summaries_an_approved_item_is_awaiting_merge_not_ready():
    items = [
        {"id": "f0", "initiative": "foxtrot", "phase": "build", "state": "approved", "needs": []},
    ]
    summaries = route.initiative_summaries(items)
    assert summaries == [{"id": "foxtrot", "phase": None, "ready": 0, "awaiting_merge": 1}]


def test_initiative_states_an_approved_item_is_not_complete():
    items = [{"initiative": "foxtrot", "state": "approved"}]
    assert route.initiative_states(["foxtrot"], items) == {"foxtrot": False}


def test_intake_entries_falls_back_to_id_when_frontmatter_has_no_title_and_body_is_empty():
    # Frontmatter with an id but no title, and no body line at all: title
    # must fall back to id, not raise on an empty body_lines list.
    files = {"any-name.md": "---\nid: notitle\n---\n\n"}
    entries = route.intake_entries(files)
    assert entries == [{"id": "notitle", "title": "notitle", "initiative": None, "done": False, "path": "intake/any-name.md"}]


@pytest.mark.parametrize(
    ("text", "pid"),
    [("4821", 4821), ("", None), ("   ", None), ("garbage", None)],
)
def test_parse_pid(text, pid):
    assert route.parse_pid(text) == pid


_QUIET = {"quarantined": [], "reused": [], "summary": None, "usage": None}


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([{"id": "r1", "pid": None, "state": "no pidfile", "started": None, **_QUIET}], "r1: no pidfile"),
        (
            [{"id": "r2", "pid": 123, "state": "alive", "started": "2026-09-04T00:00:00+00:00", **_QUIET}],
            "r2: alive (pid 123, started 2026-09-04T00:00:00+00:00)",
        ),
        (
            [{"id": "r3", "pid": None, "state": "no pidfile", "started": None,
              "quarantined": ["quarantined task: a — reason"], "reused": ["reused b from run-1"],
              "summary": "epic run-2: done", "usage": "usage: 1 call"}],
            "r3: no pidfile quarantined task: a — reason reused b from run-1 epic run-2: done usage: 1 call",
        ),
        (
            [{"id": "a", "pid": None, "state": "no pidfile", "started": None, **_QUIET},
             {"id": "b", "pid": 7, "state": "exited", "started": "t", **_QUIET}],
            "a: no pidfile\nb: exited (pid 7, started t)",
        ),
    ],
    ids=["no-pidfile", "pid-and-started", "extras-appended", "rows-joined-by-newline"],
)
def test_render_status_rows(rows, expected):
    assert route.render_status(rows) == expected


def test_render_status_appends_intake_groups_with_names_when_given():
    groups = {
        "queued": [{"id": "q1", "title": "Q"}],
        "decomposed": [{"id": "d1", "title": "D"}],
        "landed": [],
    }
    assert route.render_status([], groups) == (
        "intake queued: 1 — q1\nintake decomposed: 1 — d1\nintake landed: 0"
    )


def test_render_status_appends_the_gate_line_when_given():
    rows = [{"id": "a", "pid": None, "state": "no pidfile", "started": None, **_QUIET}]
    assert route.render_status(rows, gate_level="ticket") == "a: no pidfile\ngate: ticket"


@pytest.mark.parametrize(
    ("initiative_text", "intake_text", "initiative_fields", "intake_fields"),
    [
        (
            "---\nid: foo\ntitle: Foo\nrepo: acme/foo\n---\n\nBody.\n",
            "---\nid: foo\ntitle: Foo\nrepo: acme/foo\n---\n\nBody.\n",
            {"id": "foo", "title": "Foo", "repo": "acme/foo", "intake": "intake/new.md"},
            {"id": "foo", "title": "Foo", "repo": "acme/foo", "initiative": "foo"},
        ),
        (
            "---\nid: foo\nintake: intake/old.md\n---\n\nBody.\n",
            "---\nid: foo\ninitiative: foo\n---\n\nBody.\n",
            {"id": "foo", "intake": "intake/new.md"},
            {"id": "foo", "initiative": "foo"},
        ),
    ],
    ids=["adds-both-fields-keeps-the-rest", "updates-existing-fields-in-place"],
)
def test_link_intake(initiative_text, intake_text, initiative_fields, intake_fields):
    new_initiative, new_intake = route.link_intake(initiative_text, intake_text, "intake/new.md", "foo")
    assert route.parse_frontmatter(new_initiative) == (initiative_fields, "Body.")
    assert route.parse_frontmatter(new_intake) == (intake_fields, "Body.")
    assert (new_initiative.count("intake:"), new_intake.count("initiative:")) == (1, 1)


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        (
            [{"initiative": "alpha", "state": "done"}, {"initiative": "beta", "state": "ready"}],
            {"alpha": True, "beta": False, "gamma": False},
        ),
        (
            [{"initiative": "alpha", "state": "done"}, {"initiative": "alpha", "state": "dropped"}],
            {"alpha": True, "beta": False, "gamma": False},
        ),
        (
            [{"initiative": "alpha", "state": "done"}, {"initiative": "alpha", "state": "ready"}],
            {"alpha": False, "beta": False, "gamma": False},
        ),
    ],
    ids=["no-items-and-not-all-done-are-false", "done-plus-dropped-is-complete", "done-plus-ready-is-incomplete"],
)
def test_initiative_states(items, expected):
    assert route.initiative_states(["alpha", "beta", "gamma"], items) == expected


_INTAKE_NAMED = "---\nid: a\nintake: intake/a.md\n---\nbody\n"


@pytest.mark.parametrize(
    ("entry", "initiatives", "group"),
    [
        (
            {"id": "d1", "title": "D", "initiative": "alpha", "done": False, "path": "intake/d1.md"},
            [{"id": "alpha", "done": False, "text": ""}],
            "decomposed",
        ),
        (
            {"id": "l1", "title": "L", "initiative": "beta", "done": False, "path": "intake/l1.md"},
            [{"id": "beta", "done": True, "text": ""}],
            "landed",
        ),
        (
            {"id": "legacy-cited", "title": "Legacy cited", "initiative": None, "done": True,
             "path": "intake/2026-08-01-legacy-cited.md"},
            [{"id": "gamma", "done": False, "text": "See intake/2026-08-01-legacy-cited.md for background."}],
            "decomposed",
        ),
        (
            # An uncited legacy intake is landed. Id "ci" is a substring of "decided", but delta never
            # cites this path: an id-substring match would wrongly say decomposed.
            {"id": "ci", "title": "Ci", "initiative": None, "done": True, "path": "intake/2026-08-03-ci-notes.md"},
            [{"id": "delta", "done": False, "text": "The team decided this independently."}],
            "landed",
        ),
        (
            {"id": "a", "title": "A", "initiative": None, "done": False, "path": "intake/a.md"},
            [{"id": "x", "done": False, "text": _INTAKE_NAMED}],
            "decomposed",
        ),
        (
            {"id": "a", "title": "A", "initiative": None, "done": False, "path": "intake/a.md"},
            [{"id": "x", "done": True, "text": _INTAKE_NAMED}],
            "landed",
        ),
        (
            {"id": "a", "title": "A", "initiative": None, "done": False, "path": "intake/a.md"},
            [{"id": "x", "done": False, "text": "---\nintake: intake/b.md\n---\n"}],
            "queued",
        ),
    ],
    ids=[
        "open-initiative-is-decomposed", "done-initiative-is-landed",
        "legacy-cited-path-is-decomposed", "legacy-uncited-is-landed-not-matched-by-id-substring",
        "named-by-open-initiative-text-is-decomposed", "named-by-done-initiative-text-is-landed",
        "named-by-no-initiative-is-queued",
    ],
)
def test_intake_groups_classification(entry, initiatives, group):
    groups = route.intake_groups([entry], initiatives)
    assert {name: found for name, found in groups.items() if found} == {group: [entry]}


_NOW = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.UTC)


@pytest.mark.parametrize(
    ("rows", "kept", "hidden"),
    [
        ([{"id": "old", "state": "exited", "started": "2026-09-24T11:59:00+00:00"}], [], 1),
        ([{"id": "live", "state": "alive", "started": "2026-08-01T00:00:00+00:00"}], ["live"], 0),
        ([{"id": "new", "state": "exited", "started": "2026-09-25T00:00:00+00:00"}], ["new"], 0),
        (
            [{"id": "none", "state": "no pidfile", "started": None},
             {"id": "bad", "state": "exited", "started": "garbage"}],
            [],
            2,
        ),
    ],
    ids=["old-dead-hidden", "live-kept-however-old", "recent-dead-kept", "no-or-bad-started-counts-as-old"],
)
def test_recent_rows(rows, kept, hidden):
    shown, count = route.recent_rows(rows, _NOW)
    assert ([row["id"] for row in shown], count) == (kept, hidden)


@pytest.mark.parametrize(
    ("runs", "summaries", "expected"),
    [
        (
            [{"id": "run1", "pid": 123, "alive": True, "started": "t1"}],
            {},
            [{"id": "run1", "pid": 123, "alive": True, "started": "t1"}],
        ),
        (
            [{"id": "run1", "pid": 123, "alive": True, "started": "t1"}],
            {"run1": {"quarantined": ["quarantined task: a"], "reused": [], "summary": "epic run-1", "usage": None}},
            [{"id": "run1", "pid": 123, "alive": True, "started": "t1",
              "quarantined": ["quarantined task: a"], "reused": [], "summary": "epic run-1", "usage": None}],
        ),
        (
            [],
            {"run2": {"quarantined": [], "reused": [], "summary": "epic run-2", "usage": None}},
            [{"id": "run2", "pid": None, "alive": False, "started": None,
              "quarantined": [], "reused": [], "summary": "epic run-2", "usage": None}],
        ),
    ],
    ids=["run-without-log-untouched", "run-merged-with-log-summary", "log-only-id-gets-pidless-shape"],
)
def test_status_entries(runs, summaries, expected):
    assert route.status_entries(runs, summaries) == expected


def test_status_entries_sorts_the_union_of_run_and_log_ids():
    runs = [{"id": "b", "pid": 1, "alive": True, "started": "t"}]
    ids = [entry["id"] for entry in route.status_entries(runs, {"a": {}, "c": {}})]
    assert ids == ["a", "b", "c"]


_DEFAULT_ITEM = {
    "id": "task", "initiative": "demo", "phase": "1-build", "state": "todo", "needs": [],
    "file": "1-build/task.md",
}


@pytest.mark.parametrize(
    ("fields", "phase_dir", "stem", "expected"),
    [
        (
            {"id": "t1", "phase": "1-build", "state": "ready", "needs": ["t0"]},
            "9-ignored", "ignored",
            {"id": "t1", "initiative": "demo", "phase": "1-build", "state": "ready", "needs": ["t0"],
             "file": "9-ignored/ignored.md"},
        ),
        ({}, "1-build", "task", _DEFAULT_ITEM),
        ({"initiative": "wrong"}, "1-build", "task", _DEFAULT_ITEM),
    ],
    ids=["frontmatter-passes-through", "empty-frontmatter-defaults-from-path", "initiative-argument-wins"],
)
def test_work_item(fields, phase_dir, stem, expected):
    assert route.work_item(fields, initiative="demo", phase_dir=phase_dir, stem=stem) == expected


@pytest.mark.parametrize(
    ("verdict", "reason", "force", "code", "lines"),
    [
        ("stop", "both ladders exhausted", False, 2, ["routing: usage stop: both ladders exhausted"]),
        ("stop", "both ladders exhausted", True,
         None, ["routing: usage stop overridden by --force: both ladders exhausted"]),
        ("hold", "headroom below minimum", False, None, ["routing: usage hold: headroom below minimum"]),
        ("go_degraded", "ceilings tier=cheap effort=low", False,
         None, ["routing: advisory: usage go_degraded: ceilings tier=cheap effort=low"]),
        ("go", "on pace", False, None, []),
    ],
    ids=["stop-refuses-with-exit-2", "stop-forced-continues", "hold-narrates", "go-degraded-advises", "go-is-silent"],
)
def test_launch_gate(verdict, reason, force, code, lines):
    assessment = pacing.Assessment(
        spent_fraction=0.5, elapsed_fraction=0.5, projected_total=1.0, headroom_usd=1.0,
        verdict=verdict, tier_ceiling="deep", effort_ceiling="high", hold_until=None, reason=reason,
    )
    assert route.launch_gate(assessment, force=force) == (code, lines)


_REACH_FIX = "move the artifact into the repository or drop the reference"


@pytest.mark.parametrize(
    ("body", "repo", "expected"),
    [
        (
            "see ~/notes/plan.md for context", "acme/widgets",
            [route.Problem("t1", "reach", "'~/notes/plan.md' is outside 'acme/widgets'", _REACH_FIX)],
        ),
        (
            "see `/tmp/notes.md` for context", "acme/widgets",
            [route.Problem("t1", "reach", "'/tmp/notes.md' is outside 'acme/widgets'", _REACH_FIX)],
        ),
        ("see /home/user/widgets/agent_tools/route.py for the schema", "/home/user/widgets", []),
        ("POST `/v1/chat/completions` on the local server", "acme/widgets", []),
        ("see ~/notes/plan.md for context", None, []),
    ],
    ids=["tilde-path", "path-in-backtick-span", "absolute-path-inside-repo", "url-route-is-notation",
         "stood-down-when-repo-unresolved"],
)
def test_lint_items_reach(body, repo, expected):
    items = [{"task": "t1", "phase": "build", "surfaces": [], "body": body}]
    assert [p for p in route.lint_items(items, repo, ()) if p.rule == "reach"] == expected


_GRANTS = ("pytest", "git status", "git diff")


def _grant_problem(cmd: str) -> route.Problem:
    return route.Problem("t1", "grant", f"command {cmd!r} is not in this role's grant",
                         "name only pytest, git status, git diff")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Add the flag.\n\nChecks:\n- ruff check .\n- pytest", [_grant_problem("ruff")]),
        ("Add the flag.\nDone when `cox route lint` exits 0.", [_grant_problem("cox")]),
        ("Then run gh pr list to see the pull request.", [_grant_problem("gh")]),
        ("The fake runner feeds in gh run list output for the test.", []),
        ("No direct gh invocation; no real git, gh or network call.", []),
        ("Keep cox --version and cox host beat working as they do.", []),
    ],
    ids=["written-as-a-check", "written-in-the-done-line", "imperative-run", "fixture-in-prose",
         "named-in-a-prohibition", "named-as-a-code-path"],
)
def test_lint_items_grant(body, expected):
    items = [{"task": "t1", "phase": "build", "surfaces": [], "body": body}]
    assert route.lint_items(items, "acme/widgets", _GRANTS) == expected


@pytest.mark.parametrize(
    ("tickets", "rules"),
    [
        (
            [("t1", "build", [], ["tests/test_route.py"]), ("t2", "build", ["t1"], ["tests/test_route.py"])],
            ["coupling", "phase_needs"],
        ),
        (
            [("a", "build", ["b"], ["tests/test_route.py"]), ("b", "build", ["c"], []),
             ("c", "build", [], ["tests/test_route.py"])],
            ["coupling", "phase_needs", "phase_needs"],
        ),
        (
            [("t1", "build", [], ["tests/test_route.py"]), ("t2", "ship", [], ["tests/test_route.py"])],
            [],
        ),
        ([("t1", "build", [], []), ("t2", "ship", ["t1"], [])], []),
    ],
    ids=["needs-edge-does-not-excuse-coupling", "former-needs-chain-still-couples",
         "cross-phase-sharing-is-not-coupling", "needs-edge-across-phases"],
)
def test_lint_items_coupling_and_phase_needs(tickets, rules):
    items = [
        {"task": task, "phase": phase, "surfaces": surfaces, "body": task, "needs": needs}
        for task, phase, needs, surfaces in tickets
    ]
    assert [p.rule for p in route.lint_items(items, "acme/widgets", ())] == rules


def test_lint_items_phase_needs_problem_text():
    items = [
        {"task": "t1", "phase": "build", "surfaces": [], "body": "first ticket"},
        {"task": "t2", "phase": "build", "surfaces": [], "body": "second ticket", "needs": ["t1"]},
    ]
    assert route.lint_items(items, "acme/widgets", ()) == [
        route.Problem(
            "t2", "phase_needs", "t2 needs t1 in the same phase 'build'",
            "move it to a later phase that needs this one",
        )
    ]


_SURFACE_PATHS = frozenset({"agent_tools/route.py", "tests/test_route.py", "README.md"})
_SURFACE_OTHERS = {"coxswain-graphs": frozenset({"harness", "runner"}), "coxswain-dash": frozenset({"src"})}


def _surface_item(*surfaces):
    return [{"task": "t1", "phase": "build", "surfaces": list(surfaces), "body": "work"}]


def test_top_dirs_returns_first_components_and_skips_root_files():
    paths = {"a/b.py", "a/c.py", "d/e/f.py", "README.md"}
    assert route.top_dirs(paths) == frozenset({"a", "d"})


@pytest.mark.parametrize(
    ("surfaces", "rules", "mentions"),
    [
        (("agent_tools/nope.py",), ["surface_missing"], ("agent_tools/nope.py", "(new)")),
        (("agent_tools/route.py (new)",), ["surface_exists"], ("agent_tools/route.py",)),
        (("harness/run.py",), ["surface_wrong_repo"], ("coxswain-graphs",)),
        (("agent_tools/route.py", "agent_tools/fresh.py (new)"), [], ()),
        (("agent_tools/",), [], ()),
        (("agent_tools/gone/",), ["surface_missing"], ()),
    ],
    ids=["unmarked-missing", "new-already-exists", "foreign-top-directory-names-other-repo",
         "one-existing-one-new", "directory-found-by-files-under-it", "directory-without-files-is-missing"],
)
def test_surface_problems(surfaces, rules, mentions):
    problems = route.surface_problems(_surface_item(*surfaces), _SURFACE_PATHS, _SURFACE_OTHERS)
    assert [p.rule for p in problems] == rules
    assert all(text in f"{problems[0].detail} {problems[0].fix}" for text in mentions)


def test_lint_items_checks_surfaces_only_when_checkout_is_given():
    items = _surface_item("agent_tools/nope.py")
    assert route.lint_items(items, "acme/widgets", ()) == []
    problems = route.lint_items(items, "acme/widgets", (), checkout=(_SURFACE_PATHS, _SURFACE_OTHERS))
    assert [p.rule for p in problems] == ["surface_missing"]


@pytest.mark.parametrize(
    ("first", "second", "surface"),
    [
        (
            {"task": "t1", "phase": "build", "state": "approved", "surfaces": ["agent_tools/route.py"]},
            {"task": "t2", "phase": "build", "state": "ready", "surfaces": ["agent_tools/route.py"]},
            "agent_tools/route.py",
        ),
        (
            {"task": "t1", "phase": "build", "surfaces": ["agent_tools/route.py"]},
            {"task": "t2", "phase": "build", "surfaces": ["agent_tools/route.py"]},
            "agent_tools/route.py",
        ),
    ],
    ids=["approved-beside-ready", "any-shared-surface-not-only-test-files"],
)
def test_coupling_problems(first, second, surface):
    assert route._coupling_problems([first, second]) == [
        route.Problem(
            "t1", "coupling", f"t1 and t2 both surface {surface!r}",
            "merge them, or move one to a later phase",
        )
    ]


def test_merge_report_lines_names_members_merged_id_and_shared_surface():
    merges = [{"members": ["a", "b"], "into": "a", "surfaces": ["agent_tools/x.py"]}]
    assert route.merge_report_lines(merges) == [
        "merged: a + b -> a (shared agent_tools/x.py)"
    ]


# The shape `initiative_files` files with `budget_usd`, plus one attempt as the chair records it.
_FILED_TICKET = (
    "---\nid: a\nphase: build\nstate: ready\nneeds: []\nsurfaces: [agent_tools/x.py]\ntitle: Add A\n"
    "budget_usd: 2.0\nattempts:\n  - run: demo-1\n    ts: '2026-09-01'\n    body_sha: abc\nlint: []\n"
    "---\n\nBody A\n"
)


def test_merged_ticket_text_keeps_budget_attempts_and_lint_on_a_survivor():
    survivor = {
        "id": "a", "title": "Add A; Add B", "state": "ready", "phase": "build",
        "surfaces": ["agent_tools/x.py", "agent_tools/y.py"], "needs": ["c"],
        "body": "## Add A\nBody A\n\n## Add B\nBody B",
    }
    assert route.merged_ticket_text(_FILED_TICKET, survivor) == (
        "---\nid: a\nphase: build\nstate: ready\nneeds: [c]\nsurfaces: [agent_tools/x.py, agent_tools/y.py]\n"
        "title: Add A; Add B\nbudget_usd: 2.0\nattempts:\n  - run: demo-1\n    ts: '2026-09-01'\n"
        "    body_sha: abc\nlint: []\n---\n\n## Add A\nBody A\n\n## Add B\nBody B\n"
    )


def test_merged_ticket_text_marks_a_dropped_member_and_keeps_everything_else():
    dropped = {"id": "a", "title": "Add A", "state": "dropped", "merged_into": "z"}
    assert route.merged_ticket_text(_FILED_TICKET, dropped) == _FILED_TICKET.replace(
        "state: ready", "state: dropped"
    ).replace("lint: []\n---", "lint: []\nmerged_into: z\n---")


def test_merged_ticket_text_replaces_a_block_list_field_whole():
    original = "---\nid: a\nsurfaces:\n  - agent_tools/x.py\ntitle: A\n---\n\nBody\n"
    survivor = {"id": "a", "title": "A; B", "surfaces": ["agent_tools/x.py", "agent_tools/y.py"], "needs": [],
                "body": "Merged"}
    assert route.merged_ticket_text(original, survivor) == (
        "---\nid: a\nsurfaces: [agent_tools/x.py, agent_tools/y.py]\ntitle: A; B\nneeds: []\n---\n\nMerged\n"
    )


def test_lint_items_flags_a_size_violation():
    # work-shape.md §8: "a 750-word body" is the fixture this rule's test carries.
    items = [{"task": "t1", "phase": "build", "surfaces": [], "body": " ".join(["word"] * 750)}]
    problems = route.lint_items(items, "acme/widgets", ())
    assert problems == [
        route.Problem("t1", "size", "body is 750 words", "point at a spec file in the repository")
    ]


def test_lint_items_returns_empty_list_for_a_clean_dag():
    # work-shape.md §8: "a clean DAG yields `[]`."
    items = [{"task": "t1", "phase": "build", "surfaces": ["agent_tools/route.py"], "body": "plain ticket body"}]
    assert route.lint_items(items, "acme/widgets", ()) == []


def _cross_item(body="", surfaces=()):
    return [{"task": "t1", "phase": "build", "surfaces": list(surfaces), "body": body}]


_CROSS_FIX = "paste the code the build needs into the ticket: a build reads only its own repository"


@pytest.mark.parametrize(
    ("body", "surfaces", "repo", "expected"),
    [
        (
            "edit harness/cli.py", (), "/home/user/coxswain-tools",
            [route.Problem("t1", "cross_repo", "names harness/cli.py, which lives in coxswain-graphs", _CROSS_FIX)],
        ),
        ("edit agent_tools/run_store.py", ("agent_tools/run_store.py",), "/home/user/coxswain-tools", []),
        ("edit harness/cli.py", (), "/home/user/widgets", []),
        ("edit harness/cli.py", (), None, []),
        ("see docs/design/x.md", ("tests/test_x.py",), "/home/user/coxswain-tools", []),
        (
            "edit src/tools/agent_tools/cli.py", (), "/home/user/coxswain-graphs",
            [route.Problem(
                "t1", "cross_repo", "names src/tools/agent_tools/cli.py, which lives in coxswain", _CROSS_FIX,
            )],
        ),
        (
            "edit src/graphs/harness/epic.py and src/tools/agent_tools/cli.py and crew/install.sh",
            (), "/home/user/coxswain", [],
        ),
        (
            "edit `harness/cli.py`, then harness/cli.py again", ("harness/cli.py",), "/home/user/coxswain-tools/",
            [route.Problem("t1", "cross_repo", "names harness/cli.py, which lives in coxswain-graphs", _CROSS_FIX)],
        ),
    ],
    ids=["other-repos-package", "own-repo", "unknown-repo-stands-down", "missing-repo-stands-down",
         "tests-and-docs-never-count", "monorepo-path-from-old-component-repo",
         "monorepo-may-name-any-component", "path-named-twice-reports-once"],
)
def test_lint_items_cross_repo(body, surfaces, repo, expected):
    assert route.lint_items(_cross_item(body, surfaces), repo, ()) == expected


def _ticket(id, title, *, state="ready", phase="build", surfaces=(), needs=(), body=""):
    return {
        "id": id,
        "title": title,
        "state": state,
        "phase": phase,
        "surfaces": list(surfaces),
        "needs": list(needs),
        "body": body,
    }


def test_merge_same_phase_merges_two_ready_tickets_sharing_one_surface():
    items = [
        _ticket("a", "Add A", surfaces=["agent_tools/x.py"], body="Body A"),
        _ticket("b", "Add B", surfaces=["agent_tools/x.py"], body="Body B"),
    ]
    result_items, merges = route.merge_same_phase(items)
    assert result_items == [
        {
            "id": "a", "title": "Add A; Add B", "state": "ready", "phase": "build",
            "surfaces": ["agent_tools/x.py"], "needs": [],
            "body": "## Add A\nBody A\n\n## Add B\nBody B",
        },
        {
            "id": "b", "title": "Add B", "state": "dropped", "phase": "build",
            "surfaces": ["agent_tools/x.py"], "needs": [], "body": "Body B",
            "merged_into": "a",
        },
    ]
    assert merges == [{"members": ["a", "b"], "into": "a", "surfaces": ["agent_tools/x.py"]}]


def test_merge_same_phase_merges_a_chain_through_an_intermediate_ticket():
    items = [
        _ticket("a", "Add A", surfaces=["agent_tools/x.py"]),
        _ticket("b", "Add B", surfaces=["agent_tools/x.py", "agent_tools/y.py"]),
        _ticket("c", "Add C", surfaces=["agent_tools/y.py"]),
    ]
    result_items, merges = route.merge_same_phase(items)
    assert len(result_items) == 3
    merged = result_items[0]
    assert merged["id"] == "a"
    assert merged["title"] == "Add A; Add B; Add C"
    assert merged["surfaces"] == ["agent_tools/x.py", "agent_tools/y.py"]
    assert [item["state"] for item in result_items[1:]] == ["dropped", "dropped"]
    assert [item["merged_into"] for item in result_items[1:]] == ["a", "a"]
    assert merges == [{
        "members": ["a", "b", "c"], "into": "a",
        "surfaces": ["agent_tools/x.py", "agent_tools/y.py"],
    }]


@pytest.mark.parametrize(
    "items",
    [
        [_ticket("a", "Add A", phase="build", surfaces=["agent_tools/x.py"]),
         _ticket("b", "Add B", phase="test", surfaces=["agent_tools/x.py"])],
        [_ticket("a", "Add A", state="done", surfaces=["agent_tools/x.py"]),
         _ticket("b", "Add B", state="ready", surfaces=["agent_tools/x.py"])],
        [_ticket("a", "Add A", surfaces=["agent_tools/x.py"]),
         _ticket("b", "Add B", surfaces=["agent_tools/y.py"])],
    ],
    ids=["different-phases", "done-ticket-never-merged", "disjoint-surfaces"],
)
def test_merge_same_phase_leaves_unmergeable_tickets_untouched(items):
    assert route.merge_same_phase(items) == (items, [])


def test_merge_same_phase_rewrites_an_external_needs_reference_to_the_merged_id():
    items = [
        _ticket("a", "Add A", surfaces=["agent_tools/x.py"]),
        _ticket("b", "Add B", surfaces=["agent_tools/x.py"]),
        _ticket("c", "Add C", phase="test", needs=["b"]),
    ]
    result_items, merges = route.merge_same_phase(items)
    assert result_items[2]["needs"] == ["a"]


def _lint_ns(initiative_dir, repo=None):
    return argparse.Namespace(initiative_dir=str(initiative_dir), repo=repo)


def test_route_lint_cli_resolves_repo_from_initiative_frontmatter(tmp_path, capsys):
    # 2026-09-09 revise: `--repo` defaults to None, and the CLI handler
    # falls back to the filed initiative's own `repo:` field, so the
    # default invocation does not flag every path as unreachable.
    initiative = tmp_path / "widget-fix"
    initiative.mkdir()
    (initiative / "initiative.md").write_text(
        '---\nid: "widget-fix"\ntitle: "Widget fix"\nrepo: "acme/widgets"\n---\nfix the widget\n',
        encoding="utf-8",
    )
    build = initiative / "build"
    build.mkdir()
    (build / "widget-fix.md").write_text(
        '---\nid: "widget-fix"\nphase: "build"\nsurfaces: []\n---\nsee ~/notes/plan.md for context\n',
        encoding="utf-8",
    )
    code = cli._route_lint(_lint_ns(initiative))
    out = capsys.readouterr().out
    assert code == 2
    assert "widget-fix reach: '~/notes/plan.md' is outside 'acme/widgets' -> " in out


def test_route_lint_cli_stands_reach_down_and_says_so_once_with_no_repo_anywhere(tmp_path, capsys):
    initiative = tmp_path / "widget-fix"
    initiative.mkdir()
    (initiative / "initiative.md").write_text(
        '---\nid: "widget-fix"\ntitle: "Widget fix"\n---\nfix the widget\n', encoding="utf-8",
    )
    build = initiative / "build"
    build.mkdir()
    (build / "widget-fix.md").write_text(
        '---\nid: "widget-fix"\nphase: "build"\nsurfaces: []\n---\nsee ~/notes/plan.md for context\n',
        encoding="utf-8",
    )
    code = cli._route_lint(_lint_ns(initiative))
    out = capsys.readouterr().out
    assert code == 0
    assert out == f"routing: no repo found for {initiative}; reach check skipped\n"


def test_route_lint_cli_merges_same_phase_ready_tickets_before_linting(tmp_path, capsys):
    # same-phase-tickets-that-share-a-file-are-merged-wire-merge-into-lint-and-launch:
    # merge_same_phase runs first, so a mergeable pair never reaches the
    # coupling check as two separate tickets.
    initiative = tmp_path / "widget-fix"
    initiative.mkdir()
    (initiative / "initiative.md").write_text(
        '---\nid: "widget-fix"\ntitle: "Widget fix"\nrepo: "acme/widgets"\n---\nfix the widget\n',
        encoding="utf-8",
    )
    build = initiative / "build"
    build.mkdir()
    t1 = build / "t1.md"
    t2 = build / "t2.md"
    attempt = "attempts:\n  - run: widget-fix-1\n    ts: '2026-09-01'\n"
    t1.write_text(
        "---\nid: t1\ntitle: First\nphase: build\nstate: ready\nsurfaces: [tests/test_widget.py]\n"
        f"budget_usd: 2.0\n{attempt}lint: []\n---\nfirst ticket\n",
        encoding="utf-8",
    )
    t2.write_text(
        "---\nid: t2\ntitle: Second\nphase: build\nstate: ready\nsurfaces: [tests/test_widget.py]\n"
        f"budget_usd: 3.0\n{attempt}---\nsecond ticket\n",
        encoding="utf-8",
    )
    code = cli._route_lint(_lint_ns(initiative))
    out = capsys.readouterr().out
    assert code == 0
    assert out == "merged: t1 + t2 -> t1 (shared tests/test_widget.py)\n"
    merged_text = t1.read_text(encoding="utf-8")
    merged_fields, _ = route.parse_frontmatter(merged_text)
    assert merged_fields["state"] == "ready"
    assert merged_fields["title"] == "First; Second"
    assert f"budget_usd: 2.0\n{attempt}lint: []\n" in merged_text
    dropped_text = t2.read_text(encoding="utf-8")
    dropped_fields, _ = route.parse_frontmatter(dropped_text)
    assert dropped_fields["state"] == "dropped"
    assert dropped_fields["merged_into"] == "t1"
    assert f"budget_usd: 3.0\n{attempt}" in dropped_text


def test_route_lint_cli_defaults_a_missing_state_to_todo_so_the_pair_still_merges(tmp_path, capsys):
    # tools-loop-fixes-lint-needs: with no explicit `state:`, `_route_lint`
    # defaults both tickets to `todo`, so they are still merge-eligible and
    # the merge runs before any coupling check would otherwise see them.
    initiative = tmp_path / "widget-fix"
    initiative.mkdir()
    (initiative / "initiative.md").write_text(
        '---\nid: "widget-fix"\ntitle: "Widget fix"\nrepo: "acme/widgets"\n---\nfix the widget\n',
        encoding="utf-8",
    )
    build = initiative / "build"
    build.mkdir()
    (build / "t1.md").write_text(
        "---\nid: t1\nphase: build\nsurfaces: [tests/test_widget.py]\n---\nfirst ticket\n",
        encoding="utf-8",
    )
    (build / "t2.md").write_text(
        "---\nid: t2\nphase: build\nsurfaces: [tests/test_widget.py]\nneeds: [t1]\n---\nsecond ticket\n",
        encoding="utf-8",
    )
    code = cli._route_lint(_lint_ns(initiative))
    out = capsys.readouterr().out
    assert code == 0
    assert out == "merged: t1 + t2 -> t1 (shared tests/test_widget.py)\n"


def test_parse_profile_reads_the_weekly_ceiling_beside_the_window_ceiling():
    text = VALID_PROFILE + "spend:\n  window_ceiling_usd: 50\n  weekly_ceiling_usd: 1043\n"
    profile = route.parse_profile(text)
    assert profile["window_ceiling_usd"] == 50.0
    assert profile["weekly_ceiling_usd"] == 1043.0


def test_parse_profile_reads_sources_and_repo_map_as_json_values():
    text = VALID_PROFILE + 'sources: {"github": {"repos": ["a/b"]}}\nrepo_map: {"a/b": "tools"}\n'
    profile = route.parse_profile(text)
    assert profile["sources"] == {"github": {"repos": ["a/b"]}}
    assert profile["repo_map"] == {"a/b": "tools"}


@pytest.mark.parametrize(
    "reset_line",
    ['  weekly_reset: "Sun 04:00 America/New_York"  # the meter\n', "  weekly_reset: Sun 04:00 America/New_York\n"],
    ids=["quoted-with-trailing-comment", "unquoted"],
)
def test_parse_profile_keeps_spend_weekly_reset_as_text(reset_line):
    profile = route.parse_profile(VALID_PROFILE + "spend:\n" + reset_line)
    assert profile["weekly_reset"] == "Sun 04:00 America/New_York"


def _cand(link: str, repo: str = "a/b"):
    from agent_tools.sources import Candidate

    return Candidate(title="Fix it", body="b", repo=repo, link=link)


def test_pull_plan_writes_a_mapped_candidate_with_source_and_link():
    files, reviews, refusals = route.pull_plan([_cand("l1")], frozenset(), {"a/b": "tools"}, date="2026-09-24", source="github")
    assert refusals == []
    assert files == [route.intake_file("Fix it", "b", "tools", "2026-09-24", source="github", link="l1")]
    assert "link: l1\n" in files[0]["intake/2026-09-24-fix-it.md"]


def test_pull_plan_skips_a_taken_link_and_refuses_an_unmapped_repo_by_name():
    cands = [_cand("l1"), _cand("l2", repo="x/y")]
    files, reviews, refusals = route.pull_plan(cands, frozenset({"l1"}), {"a/b": "tools"}, date="2026-09-24", source="github")
    assert files == []
    assert len(refusals) == 1 and "'Fix it'" in refusals[0] and "l2" in refusals[0] and "x/y" in refusals[0]


def test_pull_plan_routes_a_pr_to_a_review_and_an_issue_to_an_intake_file():
    from agent_tools.sources import Candidate

    pr = Candidate(title="Add retry", body="b", repo="a/b", link="l2", kind="pr")
    files, reviews, refusals = route.pull_plan([_cand("l1"), pr], frozenset(), {"a/b": "tools"}, date="2026-09-24", source="github")
    assert (len(files), reviews, refusals) == (1, ["l2"], [])


def test_pull_plan_with_ids_sequence_writes_the_allocated_id(monkeypatch):
    monkeypatch.setattr(route, "allocate_intake_id", lambda: "I7")
    files, reviews, refusals = route.pull_plan(
        [_cand("l1")], frozenset(), {"a/b": "tools"}, date="2026-09-24", source="github", ids="sequence"
    )
    assert refusals == []
    assert set(files[0]) == {"intake/I7.md"}


def test_parse_allocate_id_returns_the_id_on_a_clean_exit():
    assert route.parse_allocate_id(0, '{"id": "I7"}') == "I7"


def test_parse_allocate_id_raises_naming_the_exit_code_and_output_on_failure():
    with pytest.raises(ValueError, match=r"id allocation failed \(exit 1\): boom"):
        route.parse_allocate_id(1, "boom")


def test_allocate_intake_id_raises_when_the_harness_python_is_unavailable(monkeypatch):
    monkeypatch.setattr(route.run_store, "_harness_python", lambda: None)
    with pytest.raises(ValueError, match="id allocation failed"):
        route.allocate_intake_id()


def test_a_one_task_initiatives_task_id_is_never_the_initiatives_own_id():
    """The store keys rows by (initiative, task id): a shared id overwrote the initiative row (2026-10-23)."""
    files = route.initiative_files("Fix the Bug", "Ship it.", "repo-url")
    initiative = route.parse_frontmatter(files["work/fix-the-bug/initiative.md"])[0]["id"]
    (task_path,) = [p for p in files if p != "work/fix-the-bug/initiative.md"]
    task = route.parse_frontmatter(files[task_path])[0]["id"]
    assert (initiative, task, task_path) == ("fix-the-bug", "fix-the-bug-task", "work/fix-the-bug/build/fix-the-bug-task.md")
