from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("agent_tools", reason="commands render reads coxswain-tools' command table")

import agent_tools
from agent_tools import cli as tools_cli
from agent_tools.commands import Arg, Command, Group

from devtools import cli
from devtools.commands_render import (
    BEGIN,
    END,
    arg_usage,
    render_page,
    render_pages,
    render_readme_block,
    splice,
    usage,
)

README = Path(agent_tools.__file__).resolve().parent.parent / "README.md"


def _row(name: str, args: tuple[Arg, ...] = (), slash: bool = False, examples: tuple[str, ...] = ()) -> Command:
    return Command(name, "g", f"does {name}", args, lambda a: 0, slash, examples)


def _sub(name: str, args: tuple[Arg, ...] = (), slash: bool = False) -> SimpleNamespace:
    return SimpleNamespace(name=name, group="g", summary=f"does {name}", args=args, fn=lambda a: 0, slash=slash, examples=(), subcommands=())


def _nested(name: str, subs: tuple[SimpleNamespace, ...], slash: bool = False) -> SimpleNamespace:
    return SimpleNamespace(name=name, group="g", summary=f"does {name}", args=(), fn=lambda a: 0, slash=slash, examples=(), subcommands=subs)


def _group(name: str = "g", args: tuple[Arg, ...] = ()) -> Group:
    return Group(name=name, help=f"the {name} group", description="", epilog="", args=args, fn=lambda a: 0)


def test_arg_usage_writes_each_argument_kind_as_a_usage_line_does() -> None:
    assert arg_usage(Arg(("run",))) == "RUN"
    assert arg_usage(Arg(("text",), {"nargs": "?"})) == "[TEXT]"
    assert arg_usage(Arg(("--json",), {"action": "store_true"})) == "[--json]"
    assert arg_usage(Arg(("--repo",), {"required": True})) == "--repo REPO"
    assert arg_usage(Arg(("--runs-dir",))) == "[--runs-dir RUNS_DIR]"
    assert arg_usage(Arg(("--checkout",), {"metavar": "NAME=PATH"})) == "[--checkout NAME=PATH]"
    assert arg_usage(Arg(("verb",), {"choices": ("render",)})) == "render"
    assert arg_usage(Arg(("--hidden",), {"help": argparse.SUPPRESS})) is None


def test_usage_joins_the_command_and_its_shown_arguments() -> None:
    assert usage("cox g x", (Arg(("run",)), Arg(("--hidden",), {"help": argparse.SUPPRESS}), Arg(("--json",), {"action": "store_true"}))) == "cox g x RUN [--json]"


def test_readme_block_is_one_fenced_line_per_command_in_table_order() -> None:
    table = [
        (_group("g"), [_row("a", (Arg(("run",)),)), _row("b", examples=("cox g b --now",))]),
        (_group("leaf", (Arg(("--root",)),)), []),
    ]
    assert render_readme_block(table) == (
        "```\n"
        "cox g a RUN   does a\n"
        "cox g b --now   does b\n"
        "cox leaf [--root ROOT]   the leaf group\n"
        "```\n"
    )


def test_splice_replaces_only_the_span_between_the_markers() -> None:
    text = f"before\n{BEGIN}\nold\n{END}\nafter\n"
    assert splice(text, "new\n") == f"before\n{BEGIN}\nnew\n{END}\nafter\n"


def test_splice_is_none_when_a_marker_is_missing_or_out_of_order() -> None:
    assert splice(f"{BEGIN}\nold\n", "new\n") is None
    assert splice(f"{END}\n{BEGIN}\n", "new\n") is None


def test_render_page_carries_the_summary_the_argument_hint_and_the_examples() -> None:
    row = _row("a", (Arg(("run",)), Arg(("--json",), {"action": "store_true"})), slash=True, examples=("cox g a R1",))
    assert render_page(row) == (
        "---\n"
        'description: "does a"\n'
        'argument-hint: "RUN [--json]"\n'
        "---\n"
        "\n"
        "Run `cox g a $ARGUMENTS` and report what it prints.\n"
        "\n"
        "does a\n"
        "\n"
        "Examples:\n"
        "\n"
        "```\n"
        "cox g a R1\n"
        "```\n"
    )


def test_render_pages_names_one_file_per_slash_row_only() -> None:
    table = [(_group(), [_row("a", slash=True), _row("b")])]
    assert list(render_pages(table)) == ["a.md"]


def test_cli_render_rewrites_the_readme_block_and_a_second_run_changes_nothing(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(f"intro\n{BEGIN}\nstale\n{END}\noutro\n", encoding="utf-8")
    assert cli.main(["commands", "render", "--readme", str(readme)]) == 0
    first = readme.read_text(encoding="utf-8")
    assert first == f"intro\n{BEGIN}\n{render_readme_block(tools_cli.COMMAND_TABLE)}{END}\noutro\n"
    assert cli.main(["commands", "render", "--readme", str(readme)]) == 0
    assert readme.read_text(encoding="utf-8") == first


def test_cli_render_refuses_a_readme_without_markers_and_leaves_it_alone(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("no markers\n", encoding="utf-8")
    assert cli.main(["commands", "render", "--readme", str(readme)]) == 2
    assert readme.read_text(encoding="utf-8") == "no markers\n"


def test_cli_render_pages_target_needs_a_pages_dir(tmp_path: Path) -> None:
    assert cli.main(["commands", "render", "--target", "pages"]) == 2


def test_cli_render_writes_pages_into_pages_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tools_cli, "COMMAND_TABLE", [(_group(), [_row("a", slash=True)])])
    a = argparse.Namespace(target="pages", pages_dir=str(tmp_path / "commands"), readme="unused")
    assert cli._dev_commands(a) == 0
    assert (tmp_path / "commands" / "a.md").read_text(encoding="utf-8") == render_page(_row("a", slash=True))


def test_the_committed_readme_commands_block_is_what_render_produces() -> None:
    if not README.exists():
        pytest.skip("coxswain-tools is not a checkout here")
    text = README.read_text(encoding="utf-8")
    rendered = render_readme_block(tools_cli.COMMAND_TABLE)
    assert splice(text, rendered) == text, f"README Commands block is stale; run `uv run --frozen python -m devtools commands render`. Expected:\n{rendered}"


def test_render_defaults_to_the_sibling_tools_readme() -> None:
    umbrella = Path(__file__).resolve().parent.parent.parent
    a = cli.build_parser().parse_args(["commands", "render"])
    assert Path(a.readme) == umbrella.parent / "coxswain-tools" / "README.md"


def test_splice_keeps_an_older_begin_marker_wording() -> None:
    old = "<!-- commands:begin (generated by `cox dev commands render`; do not edit) -->"
    text = f"intro\n{old}\nstale\n{END}\ntail\n"
    assert splice(text, "fresh\n") == f"intro\n{old}\nfresh\n{END}\ntail\n"


def test_a_row_with_subcommands_renders_one_line_per_subcommand_and_none_for_itself() -> None:
    table = [(_group(), [_nested("r", (_sub("a", (Arg(("run",)),)), _sub("b")))])]
    assert render_readme_block(table) == "```\ncox g r a RUN   does a\ncox g r b   does b\n```\n"


def test_a_row_without_subcommands_renders_as_today_beside_a_nested_row() -> None:
    table = [(_group(), [_row("p"), _nested("r", (_sub("a"),))])]
    assert render_readme_block(table) == "```\ncox g p   does p\ncox g r a   does a\n```\n"


def test_render_pages_names_a_slash_subcommand_after_its_row_and_its_full_command() -> None:
    table = [(_group(), [_nested("r", (_sub("a", slash=True), _sub("b")), slash=True)])]
    pages = render_pages(table)
    assert list(pages) == ["r-a.md"]
    assert "Run `cox g r a $ARGUMENTS`" in pages["r-a.md"]
