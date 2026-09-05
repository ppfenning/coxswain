import json

import pytest

from core.init import init_plan, render_plan

TEMPLATE = {
    "cartridge.yaml": (
        "team: example-team\n"
        "extends: base\n"
        "description: Worked example showing how a team binds the base contract\n"
        "version: 1\n"
        "\n"
        "# a comment that must survive verbatim\n"
        "role: some-skill\n"
    ),
    "context/code-style.md": "# Code-style charter\n\nPlaceholder.\n",
}


def _plan(**kwargs):
    return init_plan(
        "acme",
        "/repo/cartridges",
        package_cartridges_dir="/pkg/cartridges",
        template=TEMPLATE,
        **kwargs,
    )


def test_extends_local_links_both_local_and_base():
    steps = _plan(extends="local")
    links = {s["path"]: s["target"] for s in steps if s["op"] == "symlink"}
    assert links == {
        "/repo/cartridges/local": "/pkg/cartridges/local",
        "/repo/cartridges/base": "/pkg/cartridges/base",
    }


def test_extends_local_has_mkdirs_for_team_and_context():
    steps = _plan(extends="local")
    mkdirs = [s["path"] for s in steps if s["op"] == "mkdir"]
    assert mkdirs == ["/repo/cartridges/acme", "/repo/cartridges/acme/context"]


def test_extends_local_yaml_is_the_minimal_generated_template():
    steps = _plan(extends="local")
    yaml_step = next(s for s in steps if s["path"] == "/repo/cartridges/acme/cartridge.yaml")
    assert yaml_step["text"] == (
        "# acme's cartridge. Everything the graphs need is inherited from `local`;\n"
        "# add `crew:` bindings for this team's installed plugins, and `context/` files\n"
        "# the reviewers should hold work to, here.\n"
        "team: acme\n"
        "extends: local\n"
        "description: acme's overlay on the local cartridge\n"
        "version: 1\n"
    )
    assert "skills" not in yaml_step["text"]


def test_extends_local_writes_every_template_file():
    steps = _plan(extends="local")
    written = {s["path"] for s in steps if s["op"] == "write"}
    assert written == {
        "/repo/cartridges/acme/cartridge.yaml",
        "/repo/cartridges/acme/context/code-style.md",
    }
    charter = next(s for s in steps if s["path"] == "/repo/cartridges/acme/context/code-style.md")
    assert charter["text"] == TEMPLATE["context/code-style.md"]


def test_write_steps_get_their_own_mkdir_even_when_nested():
    template = {**TEMPLATE, "docs/sub/extra.md": "notes\n"}
    steps = init_plan("acme", "/repo/cartridges", package_cartridges_dir="/pkg/cartridges", template=template)
    mkdirs = [s["path"] for s in steps if s["op"] == "mkdir"]
    assert "/repo/cartridges/acme/docs" in mkdirs
    assert "/repo/cartridges/acme/docs/sub" in mkdirs
    assert mkdirs.index("/repo/cartridges/acme/docs") < mkdirs.index("/repo/cartridges/acme/docs/sub")


def test_symlinks_are_dropped_when_root_and_package_root_are_the_same_directory():
    # This is the layout every cartridge in THIS repo already uses: base,
    # local and example-team sit beside each other in one cartridges/, with
    # no symlink at all.
    steps = init_plan(
        "acme",
        "/repo/cartridges",
        package_cartridges_dir="/repo/cartridges",
        template=TEMPLATE,
    )
    assert [s for s in steps if s["op"] == "symlink"] == []
    assert {s["path"] for s in steps if s["op"] == "mkdir"} == {
        "/repo/cartridges/acme",
        "/repo/cartridges/acme/context",
    }


def test_print_step_is_last_and_names_the_profile():
    steps = _plan(extends="local")
    assert steps[-1] == {
        "op": "print",
        "text": "team: acme\ncartridges_dir: /repo/cartridges",
    }


def test_extends_base_links_only_base():
    steps = _plan(extends="base")
    links = [s["path"] for s in steps if s["op"] == "symlink"]
    assert links == ["/repo/cartridges/base"]


@pytest.mark.parametrize("team", ["Acme", "-acme", "ac me", ""])
def test_invalid_slug_raises(team):
    with pytest.raises(ValueError, match="valid team slug"):
        init_plan(team, "/repo/cartridges", package_cartridges_dir="/pkg/cartridges", template=TEMPLATE)


def test_team_named_local_raises():
    with pytest.raises(ValueError, match="reserved"):
        init_plan("local", "/repo/cartridges", package_cartridges_dir="/pkg/cartridges", template=TEMPLATE)


def test_team_equal_to_extends_raises():
    # extends="acme" is itself unknown, so a match= is required to prove this
    # is caught by the team-cannot-extend-itself check and not by some other
    # validation that would also refuse an unknown extends value.
    with pytest.raises(ValueError, match="cannot extend itself"):
        init_plan(
            "acme",
            "/repo/cartridges",
            extends="acme",
            package_cartridges_dir="/pkg/cartridges",
            template=TEMPLATE,
        )


def test_unknown_extends_raises():
    with pytest.raises(ValueError, match="must be one of"):
        init_plan(
            "acme",
            "/repo/cartridges",
            extends="nope",
            package_cartridges_dir="/pkg/cartridges",
            template=TEMPLATE,
        )


def test_a_template_without_cartridge_yaml_is_fine():
    steps = init_plan(
        "acme",
        "/repo/cartridges",
        package_cartridges_dir="/pkg/cartridges",
        template={"context/code-style.md": "x"},
    )
    written = {s["path"] for s in steps if s["op"] == "write"}
    assert written == {
        "/repo/cartridges/acme/cartridge.yaml",
        "/repo/cartridges/acme/context/code-style.md",
    }


def test_render_plan_produces_the_pinned_dry_run_format():
    steps = _plan(extends="local")
    rendered = render_plan(steps)
    assert rendered == (
        "mkdir /repo/cartridges/acme\n"
        "mkdir /repo/cartridges/acme/context\n"
        "link /repo/cartridges/local -> /pkg/cartridges/local\n"
        "link /repo/cartridges/base -> /pkg/cartridges/base\n"
        "write /repo/cartridges/acme/cartridge.yaml (7 lines)\n"
        "write /repo/cartridges/acme/context/code-style.md (3 lines)\n"
        "print team: acme\\ncartridges_dir: /repo/cartridges"
    )


def test_render_plan_emits_exactly_one_line_per_step():
    steps = _plan(extends="local")
    rendered = render_plan(steps)
    assert len(rendered.splitlines()) == len(steps)


def test_steps_are_json_serialisable():
    steps = _plan(extends="local")
    json.dumps(steps)
