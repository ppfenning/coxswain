"""`route file --intake` and `--from-intake` refuse a body the decompose would refuse for reach, in the decompose's words."""
from pathlib import Path

import pytest

from agent_tools import route, route_reach
from agent_tools.cli import main
from graphs.delivery import initiative_decompose
from graphs.delivery.ticket_lint import idea_reach_problems, idea_reach_refusal

REACHING = "Ship it with ~/deploy/release.sh once the tests pass."
PLAIN = "Fix the parser in `src/parser.py` so a short row returns None."
CARTRIDGE = {"cartridge_sha": "sha-fixture", "skills": {"decompose": "acme-skills:decompose"}, "context": []}


class _Reached(Exception):
    """Raised by the runner: the decompose got past its reach check and asked a model."""


class _NoModel:
    def run(self, *_a, **_k):
        raise _Reached

    def __getattr__(self, _name):
        return self.run


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
    argv = ["route", "file", "--profile", str(profile), "--repo", str(profile.parent / "widget"), "--title", title, "--intake"]
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


def _decompose_refusal(idea: str, intake: dict | None = None) -> str | None:
    """What the decompose says for `idea` with launch-time inputs: no tree, no target repo."""
    args = {"run_id": "r", "date": "d", "cartridge": CARTRIDGE, "idea": idea, **({"intake": intake} if intake else {})}
    try:
        initiative_decompose.run(args, _NoModel())
    except initiative_decompose.LintRefusal as refusal:
        return str(refusal)
    except _Reached:
        return None
    raise AssertionError("the decompose neither refused nor reached a model")


def test_a_tilde_path_is_refused_at_file_intake_and_nothing_is_written(tmp_path, capsys):
    profile, ws = _profile(tmp_path)
    assert _file_intake(profile, REACHING, tmp_path) == 2
    out = capsys.readouterr().out
    assert out.startswith("routing: lint-refused: idea reach")
    assert _written(ws) == []


def test_a_body_with_no_rooted_path_files_as_before(tmp_path):
    profile, ws = _profile(tmp_path)
    assert idea_reach_problems(PLAIN, [], "") == []
    assert _file_intake(profile, PLAIN, tmp_path) == 0
    assert any(name.startswith("intake/") for name in _written(ws))


def test_a_reaching_title_with_no_body_is_refused_because_the_title_is_what_gets_written(tmp_path, capsys):
    profile, ws = _profile(tmp_path)
    assert _file_intake(profile, None, tmp_path, "Move ~/deploy/release.sh") == 2
    assert capsys.readouterr().out.startswith("routing: lint-refused: idea reach")
    assert _written(ws) == []


def test_from_intake_refuses_a_reaching_body_and_leaves_the_intake_in_place(tmp_path, capsys):
    profile, ws = _profile(tmp_path)
    code, path = _from_intake(profile, ws, "", REACHING)
    assert code == 2
    assert capsys.readouterr().out == f"routing: {idea_reach_refusal(REACHING, [], '')}\n"
    assert _written(ws) == ["intake/ship-it.md"]
    assert path.exists()


def test_a_reach_allow_entry_files_through_from_intake_and_the_decompose_accepts_it(tmp_path):
    profile, ws = _profile(tmp_path)
    allow = ["~/deploy"]
    assert idea_reach_refusal(REACHING, [], "", allow) is None
    assert _decompose_refusal(REACHING, {"frontmatter": {"reach_allow": allow}}) is None
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
def test_from_intake_reads_reach_allow_the_way_the_decompose_reads_the_same_file(frontmatter, tmp_path, capsys):
    profile, ws = _profile(tmp_path)
    path = _write_intake(profile, ws, frontmatter, REACHING)
    # The launch reads the intake file as its idea, so the decompose is given the file as it was before filing.
    expected = _decompose_refusal(path.read_text())
    code = main(["route", "file", "--profile", str(profile), "--from-intake", str(path)])
    out = capsys.readouterr().out
    if expected is None:
        assert code == 0
    else:
        assert (code, out, _written(ws)) == (2, f"routing: {expected}\n", ["intake/ship-it.md"])


@pytest.mark.parametrize(
    ("frontmatter", "filed"),
    [('reach_allow: ["~/deploy"]\n', True), ('reach_allow: ["~/other"]\n', False)],
)
def test_a_redirected_repo_is_checked_on_the_file_as_authored_not_the_rewritten_copy(frontmatter, filed, tmp_path, capsys):
    umbrella, clone = tmp_path / "umbrella", tmp_path / "clone"
    for repo in (umbrella, clone):
        (repo / ".git").mkdir(parents=True)
    profile, ws = _profile(tmp_path)
    profile.write_text(profile.read_text() + f"umbrella_dir: {umbrella}\numbrella_clone_dir: {clone}\n")
    path = _write_intake(profile, ws, frontmatter, REACHING, repo=str(umbrella))
    expected = _decompose_refusal(path.read_text())
    assert (expected is None) is filed
    code = main(["route", "file", "--profile", str(profile), "--from-intake", str(path)])
    out = capsys.readouterr().out
    if filed:
        assert code == 0
        assert f"repo: {clone}" in (ws / "intake/done/ship-it.md").read_text()
    else:
        assert (code, out, _written(ws)) == (2, f"routing: {expected}\n", ["intake/ship-it.md"])


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("Ship it", REACHING),
        ("Ship it", PLAIN),
        ("Ship it", "Use /opt/tools/run.sh and `src/a.py`."),
        ("Move ~/deploy/release.sh", None),
        ("Fix the parser", None),
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
    expected = _decompose_refusal(text)
    if expected is None:
        assert (code, len(filed)) == (0, 1)
        assert "lint-refused" not in out
    else:
        assert (code, out, _written(ws)) == (2, f"routing: {expected}\n", [])


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
