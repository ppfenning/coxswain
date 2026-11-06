"""The pure rules a land uses to skip a cherry-pick that is already on the base."""
from agent_tools import land

EMPTY = ("The previous cherry-pick is now empty, possibly due to conflict resolution.\n"
         "If you wish to commit it anyway, use:\n\n    git commit --allow-empty\n")
CONFLICT = "CONFLICT (content): Merge conflict in x.py\nerror: could not apply 1234abcd... change x\n"
SHA = "0123456789abcdef0123456789abcdef01234567"


def test_empty_pick_text_is_empty():
    assert land.is_empty_pick(EMPTY, "", unmerged=False) is True


def test_empty_pick_with_an_unmerged_file_is_not_empty():
    assert land.is_empty_pick(EMPTY, "", unmerged=True) is False


def test_conflict_text_is_not_empty():
    assert land.is_empty_pick(CONFLICT, "Auto-merging x.py\n", unmerged=False) is False


def test_nothing_to_commit_on_stdout_is_empty():
    assert land.is_empty_pick("", "On branch b\nnothing to commit, working tree clean\n", unmerged=False) is True
    assert land.is_empty_pick("hook failed: nothing to commit was seen\n", "", unmerged=False) is False


def test_skip_note_for_an_empty_pick():
    assert land.skip_note(SHA, "add x", land.SKIP_EMPTY) == "land: skipped 01234567 add x: empty pick"


def test_skip_note_for_a_patch_id_match():
    assert land.skip_note(SHA, "add x", land.SKIP_PATCH_ID) == "land: skipped 01234567 add x: patch-id match on base"


def test_all_skipped_detail_round_trips():
    detail = land.skipped_all_detail(3, "main")
    assert detail == "skipped all 3 commits, already on main"
    assert land.is_skipped_all(detail) is True
    assert land.is_skipped_all("cherry-picked 3 commits onto pr/t") is False
