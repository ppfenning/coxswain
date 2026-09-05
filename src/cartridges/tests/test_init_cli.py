"""The `cartridge init` CLI: dispatch, apply the plan, print the profile lines.

These exercise `_main` end to end against a real filesystem under `tmp_path`,
including that the cartridge it scaffolds actually resolves — the point of
`init` is a team that can load, not just files that exist.
"""

from __future__ import annotations

from pathlib import Path

from core.cartridge import _DEFAULT_CARTRIDGES_DIR, _main, load
from core.skills import index_from_roots

REPO = Path(__file__).resolve().parent.parent
SKILLS_ROOT = REPO / "skills-plugins"


def test_fresh_init_creates_team_dir_context_symlinks_yaml_and_prints_profile_lines(tmp_path, capsys):
    cartridges_dir = tmp_path / "cartridges"

    exit_code = _main(["init", "acme", "--cartridges-dir", str(cartridges_dir)])
    out = capsys.readouterr().out

    team_dir = cartridges_dir / "acme"
    assert exit_code == 0
    assert (team_dir / "context").is_dir()
    assert (cartridges_dir / "local").resolve() == (REPO / "cartridges" / "local").resolve()
    assert (cartridges_dir / "base").resolve() == (REPO / "cartridges" / "base").resolve()
    assert "team: acme" in (team_dir / "cartridge.yaml").read_text(encoding="utf-8").splitlines()
    assert out == f"team: acme\ncartridges_dir: {cartridges_dir}\n"


def test_the_scaffolded_cartridge_resolves(tmp_path):
    """A minimal cartridge.yaml (team/extends/description/version only) binds
    nothing of its own, so it must resolve against the repository's REAL
    skills root with no stand-in plugin — this is the test that would have
    caught a scaffold that inherited a template's fictional bindings."""
    cartridges_dir = tmp_path / "cartridges"
    assert _main(["init", "acme", "--cartridges-dir", str(cartridges_dir)]) == 0

    index = index_from_roots([SKILLS_ROOT])

    resolved = load("acme", cartridges_dir, skill_index=index)

    assert resolved["team"] == "acme"
    assert resolved["cartridge_sha"]


def test_dry_run_writes_nothing(tmp_path, capsys):
    cartridges_dir = tmp_path / "cartridges"

    exit_code = _main(["init", "acme", "--cartridges-dir", str(cartridges_dir), "--dry-run"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert not cartridges_dir.exists()
    assert "mkdir" in out


def test_dry_run_shows_the_minimal_yaml_write_with_no_skills_bindings(tmp_path, capsys):
    """The ticket's own Done criterion: a dry run names the generated
    cartridge.yaml's write step and its line count, and the plan it renders
    names no skill binding anywhere."""
    cartridges_dir = tmp_path / "cartridges"

    exit_code = _main(["init", "demo", "--cartridges-dir", str(cartridges_dir), "--dry-run"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert f"write {cartridges_dir / 'demo' / 'cartridge.yaml'} (7 lines)" in out
    assert "skills" not in out


def test_second_init_without_force_exits_2_and_changes_nothing(tmp_path):
    cartridges_dir = tmp_path / "cartridges"
    assert _main(["init", "acme", "--cartridges-dir", str(cartridges_dir)]) == 0
    yaml_path = cartridges_dir / "acme" / "cartridge.yaml"
    before = yaml_path.read_text(encoding="utf-8")

    exit_code = _main(["init", "acme", "--cartridges-dir", str(cartridges_dir)])

    assert exit_code == 2
    assert yaml_path.read_text(encoding="utf-8") == before


def test_force_rewrites_the_yaml(tmp_path):
    cartridges_dir = tmp_path / "cartridges"
    assert _main(["init", "acme", "--cartridges-dir", str(cartridges_dir)]) == 0
    yaml_path = cartridges_dir / "acme" / "cartridge.yaml"
    yaml_path.write_text("mutated\n", encoding="utf-8")

    exit_code = _main(["init", "acme", "--cartridges-dir", str(cartridges_dir), "--force"])

    assert exit_code == 0
    rewritten = yaml_path.read_text(encoding="utf-8")
    assert rewritten != "mutated\n"
    assert "team: acme" in rewritten.splitlines()


def test_existing_correct_symlink_is_accepted_silently(tmp_path):
    cartridges_dir = tmp_path / "cartridges"
    cartridges_dir.mkdir()
    (cartridges_dir / "local").symlink_to(REPO / "cartridges" / "local", target_is_directory=True)
    (cartridges_dir / "base").symlink_to(REPO / "cartridges" / "base", target_is_directory=True)

    exit_code = _main(["init", "acme", "--cartridges-dir", str(cartridges_dir)])

    assert exit_code == 0
    assert (cartridges_dir / "acme" / "cartridge.yaml").is_file()


def test_flat_team_invocation_still_works_unchanged(capsys):
    """The dispatch guard only wraps the flat parser; it must not change a
    byte of what an unmodified `--team` call prints. Proof, not a smoke test:
    compute the same resolution independently via `load` and require an exact
    match, not just a nonempty line."""
    expected_sha = load("local", "cartridges", skill_index=index_from_roots(["skills-plugins"]))["cartridge_sha"]

    exit_code = _main(["--team", "local", "--cartridges-dir", "cartridges", "--skills-root", "skills-plugins"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert out == f"{expected_sha}\n"


def test_the_installed_entry_point_with_argv_none_reaches_the_init_path(tmp_path, capsys, monkeypatch):
    """`cartridge` (`pyproject.toml`'s `[project.scripts]`) calls `_main()`
    with no argument, so `_main` reads `sys.argv` itself. A test that only
    ever passes an explicit argv list would never exercise that read; this
    one drives the entry point exactly as the installed command does."""
    monkeypatch.setattr(
        "sys.argv",
        ["cartridge", "init", "acme", "--cartridges-dir", str(tmp_path), "--dry-run"],
    )

    exit_code = _main()
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "mkdir" in out


def test_init_cartridges_dir_default_matches_the_flat_parsers_default():
    """Both parsers build `--cartridges-dir`'s default from one constant, so
    this is a structural guarantee, not just a read of matching literals."""
    assert _DEFAULT_CARTRIDGES_DIR == REPO / "cartridges"


def test_a_reserved_team_name_exits_2_before_any_step_runs(tmp_path, capsys):
    """`init_plan`'s own validation (a reserved team name) raises ValueError
    before any step exists, so the `except` has to wrap the plan call itself,
    not just the apply loop — this exercises that path, not an apply conflict."""
    cartridges_dir = tmp_path / "cartridges"

    exit_code = _main(["init", "base", "--cartridges-dir", str(cartridges_dir)])
    err = capsys.readouterr().err

    assert exit_code == 2
    assert "reserved" in err
    assert not cartridges_dir.exists()
