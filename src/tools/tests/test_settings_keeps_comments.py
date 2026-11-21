"""Settings-set edits a hand-kept cartridge by line splice and must keep everything it does not change."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_tools import cox_settings
from agent_tools.settings_model import REGISTRY

# One commented cartridge in the shapes an operator keeps: header block, comment lines between sections,
# trailing comments, a comment inside a multi-line list, blank-line sections, flow mappings, quoted keys,
# and a non-ASCII character in a comment.
CARTRIDGE = """\
# Équipe cartridge, kept by hand ☕
# Second header line.

extends: [base]   # parent cartridge

# ── dispatch ──
policy:
  # dispatch knobs
  dispatch:
    max_in_flight: 2     # chair cap
    local_lanes: any     # one of any, decompose
    lane_prereqs: on     # lane-host gate
    lane_model: task     # task or phase

  # ── pacing ──
  pacing:
    hard_stop_fraction: 0.9   # stop spending here
    weekly_hard_stop_fraction: 0.95   # weekly stop
    min_elapsed_fraction: 0.1   # not before this much of the window
    tier_ladder: [deep, standard, cheap]   # degradation order
    effort_ladder: [high, low]             # degradation order
  build_budget_usd_max: 5   # dollars per build
  "review_tier": 2          # quoted key
  plan_competition: {min_tier: 1}   # flow mapping, trailing comment
  notes:
    # a comment inside a multi-line list
    - one
    # and one between items
    - two

# a section after a blank line
"quoted key": {a: 1, b: two}   # flow mapping, trailing comment

epic_threshold: {phases_min: 2, tickets_min: 3, multi_repo: true}   # when work is an epic

labels:
  - alpha
  # inside the list
  - beta
"""

COMMENTED_LIST = """\
# header
policy:
  pacing:
    tier_ladder:
      - deep
      # middle
      - cheap
"""

# key -> (raw value, text the changed line must hold, text the changed line must start with)
CASES = {
    "policy.dispatch.max_in_flight": ("4", "max_in_flight: 4", "    max_in_flight"),
    "policy.dispatch.local_lanes": ("decompose", "local_lanes: decompose", "    local_lanes"),
    "policy.dispatch.lane_prereqs": ("off", "lane_prereqs: false", "    lane_prereqs"),
    "policy.dispatch.lane_model": ("phase", "lane_model: phase", "    lane_model"),
    "policy.pacing.hard_stop_fraction": ("0.8", "hard_stop_fraction: 0.8", "    hard_stop_fraction"),
    "policy.pacing.weekly_hard_stop_fraction": ("0.85", "weekly_hard_stop_fraction: 0.85", "    weekly_hard"),
    "policy.pacing.min_elapsed_fraction": ("0.2", "min_elapsed_fraction: 0.2", "    min_elapsed"),
    "policy.pacing.tier_ladder": ("[deep, cheap]", "tier_ladder: [deep, cheap]", "    tier_ladder"),
    "policy.pacing.effort_ladder": ("[low]", "effort_ladder: [low]", "    effort_ladder"),
    "policy.build_budget_usd_max": ("8", "build_budget_usd_max: 8", "  build_budget"),
    "policy.review_tier": ("3", '"review_tier": 3', '  "review_tier"'),
    "policy.plan_competition.min_tier": ("2", "plan_competition: {min_tier: 2}", "  plan_competition"),
    "epic_threshold": (
        "{phases_min: 4, tickets_min: 3, multi_repo: true}",
        "epic_threshold: {phases_min: 4, tickets_min: 3, multi_repo: true}",
        "epic_threshold",
    ),
}


def comment_and_blank_lines(text: str) -> list[str]:
    """The lines that are blank or start with `#`, in order."""
    return [line for line in text.splitlines() if not line.strip() or line.lstrip().startswith("#")]


def keys_at(text: str, path: str) -> list[str]:
    """The keys of the mapping at dotted `path` in `text`, in file order; the top level for an empty path."""
    node = yaml.safe_load(text)
    for seg in filter(None, path.split(".")):
        node = node[seg]
    return list(node)


def changed_lines(old: str, new: str) -> list[int]:
    """Indexes of the lines that differ; the two texts must hold the same number of lines."""
    before, after = old.splitlines(), new.splitlines()
    assert len(before) == len(after)
    return [i for i, (a, b) in enumerate(zip(before, after)) if a != b]


def _workspace(tmp_path: Path, text: str) -> tuple[Path, Path]:
    cartridge_dir = tmp_path / "cartridges" / "example-team"
    cartridge_dir.mkdir(parents=True)
    cartridge = cartridge_dir / "cartridge.yaml"
    cartridge.write_text(text, encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text("team: example-team\ncartridges_dir: " + str(tmp_path / "cartridges") + "\n", encoding="utf-8")
    return profile, cartridge


def test_every_cartridge_registry_key_has_a_case():
    registered = {s.path for s in REGISTRY if s.scope == "cartridge"}
    assert registered == set(CASES)


@pytest.mark.parametrize("key", sorted(CASES))
def test_set_keeps_comments_and_changes_one_line(tmp_path, key):
    raw, expected, prefix = CASES[key]
    profile, cartridge = _workspace(tmp_path, CARTRIDGE)
    parent = key.rpartition(".")[0]

    code = cox_settings.run_set(profile, "cartridge", key, raw, dry_run=False)

    new = cartridge.read_text(encoding="utf-8")
    assert code == 0
    assert comment_and_blank_lines(new) == comment_and_blank_lines(CARTRIDGE)
    assert keys_at(new, "") == keys_at(CARTRIDGE, "")
    assert keys_at(new, parent) == keys_at(CARTRIDGE, parent)
    (at,) = changed_lines(CARTRIDGE, new)
    old_line, new_line = CARTRIDGE.splitlines()[at], new.splitlines()[at]
    assert old_line.startswith(prefix)
    assert expected in new_line
    assert new_line.partition("#")[1:] == old_line.partition("#")[1:]


@pytest.mark.parametrize("key", sorted(CASES))
def test_second_identical_run_is_a_no_op(tmp_path, capsys, key):
    raw, _, _ = CASES[key]
    profile, cartridge = _workspace(tmp_path, CARTRIDGE)
    cox_settings.run_set(profile, "cartridge", key, raw, dry_run=False)
    once = cartridge.read_bytes()
    stamp = cartridge.stat().st_mtime_ns
    capsys.readouterr()

    code = cox_settings.run_set(profile, "cartridge", key, raw, dry_run=False)

    assert code == 0
    assert "no change" in capsys.readouterr().out
    assert cartridge.read_bytes() == once
    assert cartridge.stat().st_mtime_ns == stamp


def test_multiline_entry_with_a_comment_is_still_refused(tmp_path, capsys):
    profile, cartridge = _workspace(tmp_path, COMMENTED_LIST)

    code = cox_settings.run_set(profile, "cartridge", "policy.pacing.tier_ladder", "[deep]", dry_run=False)

    assert code == 1
    assert "spans lines and holds a comment" in capsys.readouterr().out
    assert cartridge.read_text(encoding="utf-8") == COMMENTED_LIST
