import pytest

from devtools.release_notes_index import add_index_rows, add_nav_line

INDEX = """# Releases

## How a release works

Prose.

## `0.36.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| tools | `o/tools` | `v0.36.0` | required |

See the [0.36.0 release notes](0.36.0.md) for what landed in each component.

## `0.35.0`

| Component | Repository or path | Tag | Required or flag |
| --- | --- | --- | --- |
| tools | `o/tools` | `v0.35.0` | required |

See the [0.35.0 release notes](0.35.0.md) for what landed in each component.
"""

COMPONENTS = [("tools", "v0.37.0"), ("crew", "v0.32.0")]

NAV = """  - Releases:
      - Releases: releases/index.md
      - 0.36.0: releases/0.36.0.md
      - 0.35.0: releases/0.35.0.md
"""


def test_index_rows_land_above_the_newest_section_and_leave_the_rest_alone():
    out = add_index_rows(INDEX, "0.37.0", COMPONENTS)
    head, tail = out.split("## `0.37.0`")
    assert head == INDEX.split("## `0.36.0`")[0]
    assert tail.endswith("\n\n## `0.36.0`" + INDEX.split("## `0.36.0`")[1])


def test_index_rows_use_the_existing_row_format():
    out = add_index_rows(INDEX, "0.37.0", COMPONENTS)
    assert "| tools |  | `v0.37.0` |  |" in out
    assert "| crew |  | `v0.32.0` |  (pinned) |" in out


def test_index_rows_second_call_is_unchanged():
    once = add_index_rows(INDEX, "0.37.0", COMPONENTS)
    assert add_index_rows(once, "0.37.0", COMPONENTS) == once


def test_index_rows_treat_a_bare_heading_as_present():
    text = INDEX + "\n## 0.37.0\n\nhand written\n"
    assert add_index_rows(text, "0.37.0", COMPONENTS) == text


def test_nav_line_goes_directly_above_the_latest_release():
    assert add_nav_line(NAV, "0.37.0") == NAV.replace(
        "      - 0.36.0:", "      - 0.37.0: releases/0.37.0.md\n      - 0.36.0:"
    )


def test_nav_line_second_call_is_unchanged():
    once = add_nav_line(NAV, "0.37.0")
    assert add_nav_line(once, "0.37.0") == once


@pytest.mark.parametrize("indent", ["  ", "      ", "\t\t"])
def test_nav_line_keeps_the_anchor_indentation(indent):
    nav = f"{indent}- 0.36.0: releases/0.36.0.md\n"
    assert add_nav_line(nav, "0.37.0") == f"{indent}- 0.37.0: releases/0.37.0.md\n{nav}"


def test_nav_line_never_anchors_on_the_index_page():
    nav = "  - Releases: releases/index.md\n  - 0.36.0: releases/0.36.0.md\n"
    assert add_nav_line(nav, "0.37.0").splitlines()[:2] == [
        "  - Releases: releases/index.md",
        "  - 0.37.0: releases/0.37.0.md",
    ]


def test_nav_line_prerelease_does_not_count_as_present():
    nav = "  - 0.37.0-beta.1: releases/0.37.0-beta.1.md\n"
    assert add_nav_line(nav, "0.37.0") == f"  - 0.37.0: releases/0.37.0.md\n{nav}"


@pytest.mark.parametrize("nav", ["  - Releases: releases/index.md\n", ""])
def test_nav_line_without_a_release_entry_raises(nav):
    with pytest.raises(ValueError, match="anchor"):
        add_nav_line(nav, "0.37.0")
