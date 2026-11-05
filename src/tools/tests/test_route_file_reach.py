"""`route file --intake` and `--from-intake` report the reach notes the decompose records, and refuse only what it refuses."""

from pathlib import Path

import pytest
import yaml

from agent_tools import route, route_reach
from agent_tools.cli import main
from graphs.delivery import initiative_decompose
from graphs.delivery.ticket_lint import heal_idea_reach, idea_reach_problems, idea_reach_refusal

REACHING = "Ship it with ~/deploy/release.sh once the tests pass."
PLAIN = "Fix the parser in `src/parser.py` so a short row returns None."
ONLY_A_PATH = "~/deploy/release.sh"
CARTRIDGE = {
    "team": "acme",
    "cartridge_sha": "sha-fixture",
    "cartridge_dir": "/fake/cartridges/acme",
    "context": [],
    "skills": {"decompose": "acme-skills:decompose"},
    "write_kinds": {
        "item_create": {"risk": "low", "ramp": "deferred"},
        "state_move": {"risk": "low", "ramp": "deferred"},
    },
    "work_routing": {"states": {"active": "work", "planned": "work", "future": "backlog"}},
    "landing_areas": {"worktree_root": "~/worktrees"},
}
DECOMPOSITION = {
    "phases": [{"id": "p1", "goal": "g"}],
    "tasks": [{"id": "t1", "phase": "p1", "title": "a", "body": "b", "needs": [], "surfaces": []}],
    "rationale": "r",
}


class _Decomposer:
    """Answers the decompose role with a one-task plan; any other role is a failure."""

    def run(self, *, role, **_k):
        assert role == "decompose", role
        return DECOMPOSITION


@pytest.fixture(autouse=True)
def _no_gh_sync_on_file(monkeypatch):
    monkeypatch.setattr("agent_tools.cli._sync_filed_items", lambda *_a, **_k: None)


def _profile(tmp_path: Path):
    (tmp_path / "widget" / ".git").mkdir(parents=True)
    ws = tmp_path / "workspace"
    ws.mkdir()
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        f"team: acme\nworkspace_dir: {ws}\nharness_dir: /opt/h\ncartridges_dir: /opt/c\nprovider_profile: /opt/p.yaml\n"
    )
    return profile, ws


def _file_intake(profile: Path, body: str | None, tmp_path: Path, title: str = "Ship it") -> int:
    """`route file --intake`; a `body` of None passes no --body, so the title is what gets written."""
    argv = [
        "route",
        "file",
        "--profile",
        str(profile),
        "--repo",
        str(profile.parent / "widget"),
        "--title",
        title,
        "--intake",
    ]
    if body is not None:
        (tmp_path / "body.txt").write_text(body)
        argv += ["--body", str(tmp_path / "body.txt")]
    return main(argv)


def _write_intake(profile: Path, ws: Path, frontmatter: str, body: str, repo: str | None = None) -> Path:
    (ws / "intake").mkdir(exist_ok=True)
    path = ws / "intake" / "ship-it.md"
    path.write_text(f"---\ntitle: Ship it\nrepo: {repo or profile.parent / 'widget'}\n{frontmatter}---\n\n{body}\n")
    return path


def _from_intake(profile: Path, ws: Path, frontmatter: str, body: str) -> tuple[int, Path]:
    path = _write_intake(profile, ws, frontmatter, body)
    return main(["route", "file", "--profile", str(profile), "--from-intake", str(path)]), path


def _written(ws: Path) -> list[str]:
    return sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file())


def _decompose(idea: str, intake: dict | None = None) -> tuple[tuple[str, ...], str | None]:
    """(lint notes, refusal) the decompose records for `idea` with launch-time inputs: no tree, no target repo."""
    args = {"run_id": "r", "date": "d", "cartridge": CARTRIDGE, "idea": idea, **({"intake": intake} if intake else {})}
    try:
        result = initiative_decompose.run(args, _Decomposer())
    except initiative_decompose.LintRefusal as refusal:
        return (), str(refusal)
    action = next(p for p in result["proposals"] if p["target"] == "initiative")["suggested_action"]
    return tuple(yaml.safe_load(action.split("---\n")[1]).get("lint", ())), None


def _lint_lines(notes) -> list[str]:
    return [f"routing: lint: {note}" for note in notes]


def _said(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("routing: lint: ")]


def test_a_tilde_path_is_filed_with_a_lint_note_and_the_file_is_written(tmp_path, capsys):
    profile, ws = _profile(tmp_path)
    assert _file_intake(profile, REACHING, tmp_path) == 0
    notes = _decompose(REACHING)[0]
    assert notes != ()
    assert _said(capsys.readouterr().out) == _lint_lines(notes)
    assert any(name.startswith("intake/") for name in _written(ws))


def test_a_body_that_is_only_a_path_is_refused_at_file_intake_and_nothing_is_written(tmp_path, capsys):
    profile, ws = _profile(tmp_path)
    assert _file_intake(profile, ONLY_A_PATH, tmp_path) == 2
    assert capsys.readouterr().out == f"routing: {heal_idea_reach(ONLY_A_PATH, [], '').refusal}\n"
    assert _written(ws) == []


def test_a_body_with_no_rooted_path_files_as_before(tmp_path, capsys):
    profile, ws = _profile(tmp_path)
    assert idea_reach_problems(PLAIN, [], "") == []
    assert _file_intake(profile, PLAIN, tmp_path) == 0
    assert _said(capsys.readouterr().out) == []
    assert any(name.startswith("intake/") for name in _written(ws))


def test_a_reaching_title_with_no_body_is_noted_because_the_title_is_what_gets_written(tmp_path, capsys):
    profile, ws = _profile(tmp_path)
    assert _file_intake(profile, None, tmp_path, "Move ~/deploy/release.sh") == 0
    assert _said(capsys.readouterr().out) != []
    assert any(name.startswith("intake/") for name in _written(ws))


def test_a_title_that_is_only_a_path_with_no_body_is_refused(tmp_path, capsys):
    profile, ws = _profile(tmp_path)
    assert _file_intake(profile, None, tmp_path, ONLY_A_PATH) == 2
    assert capsys.readouterr().out.startswith("routing: lint-refused: idea reach")
    assert _written(ws) == []


def test_from_intake_files_a_reaching_body_with_lint_notes_and_archives_the_intake(tmp_path, capsys):
    profile, ws = _profile(tmp_path)
    code, path = _from_intake(profile, ws, "", REACHING)
    assert code == 0
    assert _said(capsys.readouterr().out) == _lint_lines(heal_idea_reach(REACHING, [], "").notes)
    assert not path.exists()
    assert (ws / "intake/done/ship-it.md").exists()


def test_from_intake_refuses_a_body_that_is_only_a_path_and_leaves_the_intake_in_place(tmp_path, capsys):
    profile, ws = _profile(tmp_path)
    code, path = _from_intake(profile, ws, "", ONLY_A_PATH)
    assert code == 2
    assert capsys.readouterr().out == f"routing: {heal_idea_reach(ONLY_A_PATH, [], '').refusal}\n"
    assert _written(ws) == ["intake/ship-it.md"]
    assert path.exists()


def test_a_reach_allow_entry_files_through_from_intake_and_the_decompose_accepts_it(tmp_path):
    profile, ws = _profile(tmp_path)
    allow = ["~/deploy"]
    assert idea_reach_refusal(REACHING, [], "", allow) is None
    assert _decompose(REACHING, {"frontmatter": {"reach_allow": allow}}) == ((), None)
    code, path = _from_intake(profile, ws, "reach_allow: [~/deploy]\n", REACHING)
    assert code == 0
    assert not path.exists()
    initiative, _ = route.parse_frontmatter((ws / "work/ship-it/initiative.md").read_text())
    archived, _ = route.parse_frontmatter((ws / "intake/done/ship-it.md").read_text())
    assert archived["reach_allow"] == allow
    assert initiative["intake"] == "intake/ship-it.md"


def test_frontmatter_writes_a_list_value_as_a_list():
    text = route._frontmatter([("title", "t"), ("reach_allow", ["~/deploy", "/opt/x"])], "b")
    fields, body = route.parse_frontmatter(text)
    assert fields["reach_allow"] == ["~/deploy", "/opt/x"]
    assert body == "b"


@pytest.mark.parametrize(
    "frontmatter",
    [
        "",
        "reach_allow: [~/deploy]\n",
        'reach_allow: ["~/deploy"]\n',
        "reach_allow:\n  - ~/deploy\n",
        "reach_allow: [~/other]\n",
        "reach_allow: ~/deploy\n",
        "reach_allow: [~/deploy, 3]\n",
    ],
)
@pytest.mark.parametrize("body", [PLAIN, REACHING, ONLY_A_PATH], ids=["clean", "outside-paths", "only-a-path"])
def test_from_intake_reads_reach_allow_the_way_the_decompose_reads_the_same_file(frontmatter, body, tmp_path, capsys):
    profile, ws = _profile(tmp_path)
    path = _write_intake(profile, ws, frontmatter, body)
    # The launch reads the intake file as its idea, so the decompose is given the file as it was before filing.
    notes, refusal = _decompose(path.read_text())
    code = main(["route", "file", "--profile", str(profile), "--from-intake", str(path)])
    out = capsys.readouterr().out
    if refusal is None:
        assert (code, _said(out)) == (0, _lint_lines(notes))
    else:
        assert (code, out, _written(ws)) == (2, f"routing: {refusal}\n", ["intake/ship-it.md"])


@pytest.mark.parametrize(
    ("frontmatter", "noted"),
    [('reach_allow: ["~/deploy"]\n', False), ('reach_allow: ["~/other"]\n', True)],
)
def test_a_redirected_repo_is_checked_on_the_file_as_authored_not_the_rewritten_copy(
    frontmatter, noted, tmp_path, capsys
):
    umbrella, clone = tmp_path / "umbrella", tmp_path / "clone"
    for repo in (umbrella, clone):
        (repo / ".git").mkdir(parents=True)
    profile, ws = _profile(tmp_path)
    profile.write_text(profile.read_text() + f"umbrella_dir: {umbrella}\numbrella_clone_dir: {clone}\n")
    path = _write_intake(profile, ws, frontmatter, REACHING, repo=str(umbrella))
    notes, refusal = _decompose(path.read_text())
    assert (bool(notes), refusal) == (noted, None)
    code = main(["route", "file", "--profile", str(profile), "--from-intake", str(path)])
    assert (code, _said(capsys.readouterr().out)) == (0, _lint_lines(notes))
    assert f"repo: {clone}" in (ws / "intake/done/ship-it.md").read_text()


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("Ship it", REACHING),
        ("Ship it", ONLY_A_PATH),
        ("Ship it", PLAIN),
        ("Ship it", "Use /opt/tools/run.sh and `src/a.py`."),
        ("Move ~/deploy/release.sh", None),
        ("Fix the parser", None),
        (ONLY_A_PATH, None),
    ],
)
def test_the_filing_check_says_what_the_launched_decompose_says_of_the_text_written(title, body, tmp_path, capsys):
    profile, ws = _profile(tmp_path)
    code = _file_intake(profile, body, tmp_path, title)
    out = capsys.readouterr().out
    filed = [ws / name for name in _written(ws) if name.startswith("intake/")]
    # The launch reads the intake file: the one just written, or the one `intake_file` would have written.
    would_write = route.intake_file(title, body or "", str(profile.parent / "widget"), "2026-11-04")
    text = filed[0].read_text() if filed else next(iter(would_write.values()))
    notes, refusal = _decompose(text)
    if refusal is None:
        assert (code, len(filed), _said(out)) == (0, 1, _lint_lines(notes))
        assert "lint-refused" not in out
    else:
        assert (code, out, _written(ws)) == (2, f"routing: {refusal}\n", [])


@pytest.mark.parametrize(
    ("frontmatter", "refused"),
    [
        ({}, True),
        ({"reach_allow": ["~/deploy"]}, False),
        ({"reach_allow": ["~/other"]}, True),
        ({"reach_allow": "~/deploy"}, True),
        ({"reach_allow": ["~/deploy", 3]}, True),
    ],
)
def test_reach_refusal_reads_reach_allow_as_a_list_of_strings_else_empty(frontmatter, refused):
    refusal = route_reach.reach_refusal(REACHING, frontmatter, [], "")
    assert (refusal is not None) is refused
    assert refusal == route_reach.launch_reach_refusal(REACHING, frontmatter)
    assert bool(route_reach.launch_reach_heal(REACHING, frontmatter).notes) is refused
