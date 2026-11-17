"""The rerun decisions of `devtools release`, as plain facts in and a decision out."""

from devtools import release_progress as rp

_V = "2026.11.9"
_BRANCH = f"release/{_V}"
_SHA = "a" * 40
_FORMULA_AT = ('class Cox < Formula\n  url "https://files.example/coxswain-{v}.tar.gz"\n  sha256 "{s}"\n'
               '  resource "dep" do\n    url "https://files.example/dep-1.0.tar.gz"\n  end\nend\n')


def _formula(version):
    return _FORMULA_AT.format(v=version, s="0" * 64)


def _tap(**facts):
    return rp.Tap(**{"version": _V, "default": "main", "branch": _BRANCH, "base_formula": _formula("2026.11.8"),
                     **facts})


# ---- merge-back ----


def test_a_branch_origin_no_longer_lists_is_merged_back():
    decided = rp.merge_back_next(rp.MergeBack(_BRANCH, "main", None, checked_out="main"))
    assert decided == rp.Next("done", f"{_BRANCH} already merged back and deleted", False)


def test_a_branch_the_default_branch_already_holds_is_done_and_opens_no_pull_request():
    decided = rp.merge_back_next(rp.MergeBack(_BRANCH, "main", _SHA, merged=True, open_prs=0, checked_out="main"))
    assert decided.action == "done"
    assert decided.detail == f"{_BRANCH} already merged into main at {_SHA}"


def test_a_checkout_left_on_the_merged_branch_is_put_back():
    assert rp.merge_back_next(rp.MergeBack(_BRANCH, "main", None, checked_out=_BRANCH)).restore is True
    assert rp.merge_back_next(rp.MergeBack(_BRANCH, "main", _SHA, merged=True, checked_out=_BRANCH)).restore is True


def test_an_unmerged_branch_with_an_open_pull_request_only_merges():
    assert rp.merge_back_next(rp.MergeBack(_BRANCH, "main", _SHA, open_prs=1)).action == "merge"


def test_an_unmerged_branch_without_a_pull_request_opens_one_then_merges():
    assert rp.merge_back_next(rp.MergeBack(_BRANCH, "main", _SHA, open_prs=0)).action == "open_and_merge"


def test_an_unreadable_pull_request_list_fails_rather_than_open_a_second():
    decided = rp.merge_back_next(rp.MergeBack(_BRANCH, "main", _SHA, open_prs=None))
    assert decided.action == "fail" and _BRANCH in decided.detail


# ---- a repo's whole cut ----


def test_a_cut_is_over_only_with_no_branch_and_the_tag_on_the_default_branch():
    assert rp.merged_back(None, _SHA, True) is True
    assert rp.merged_back(_SHA, _SHA, True) is False
    assert rp.merged_back(None, None, True) is False
    assert rp.merged_back(None, _SHA, False) is False


def test_a_finished_repo_resumes_even_once_the_umbrella_branch_is_gone():
    assert rp.resuming(set(), {"coxswain"}, {"coxswain"}, "coxswain") == {"coxswain"}


def test_a_tag_with_no_branch_and_no_path_into_the_default_branch_is_not_resuming():
    assert rp.resuming(set(), {"coxswain"}, set(), "coxswain") == set()


def test_a_tagged_component_resumes_while_the_umbrella_branch_is_listed():
    assert rp.resuming({"coxswain"}, {"coxswain", "dash"}, set(), "coxswain") == {"coxswain", "dash"}


def test_only_the_cut_steps_of_a_finished_repo_are_skipped():
    finished = {"coxswain"}
    assert rp.skips_cut_step({"kind": "tag_self", "component": "coxswain"}, finished)
    assert rp.skips_cut_step({"kind": "merge_back", "component": "coxswain"}, finished)
    assert rp.skips_cut_step({"kind": "release_branch", "component": "coxswain"}, finished)
    assert not rp.skips_cut_step({"kind": "github_release", "component": "coxswain"}, finished)
    assert not rp.skips_cut_step({"kind": "tap_formula_pr", "component": "tap"}, finished)
    assert not rp.skips_cut_step({"kind": "tag", "component": "dash"}, finished)


# ---- parsing what git and gh print ----


def test_a_zero_count_is_held_and_anything_else_is_not():
    assert rp.holds("0\n") is True
    assert rp.holds("3\n") is False
    assert rp.holds("") is False
    assert rp.holds("0\t0\n") is False


def test_open_pull_requests_counts_the_listed_rows():
    assert rp.open_pull_requests('[{"number": 7}]') == 1
    assert rp.open_pull_requests("[]") == 0
    assert rp.open_pull_requests("") == 0
    assert rp.open_pull_requests("not json") is None
    assert rp.open_pull_requests('{"number": 7}') is None


def test_the_formula_version_is_its_own_sdist_not_a_resource():
    assert rp.formula_version(_formula(_V)) == _V
    assert rp.formula_version('class Cox < Formula\n  url "https://x/coxswain_tools-0.47.0.tar.gz"\n') == "0.47.0"
    assert rp.formula_version('  resource "dep" do\n    url "https://x/dep-1.0.tar.gz"\n') is None
    assert rp.formula_version(None) is None


# ---- the tap ----


def test_a_formula_already_at_the_version_on_the_default_branch_is_done():
    assert rp.tap_next(_tap(base_formula=_formula(_V))) == ("done", f"the formula on origin/main is already at {_V}")


def test_a_formula_already_past_the_version_is_done_and_never_rolled_back():
    assert rp.tap_next(_tap(base_formula=_formula("2026.12.0")))[0] == "done"


def test_nothing_of_the_version_yet_is_bumped():
    assert rp.tap_next(_tap()) == ("bump", "")


def test_a_local_branch_never_pushed_is_cut_again():
    assert rp.tap_next(_tap(local_branch=True))[0] == "recut"


def test_a_pushed_branch_with_its_pull_request_open_is_only_proved():
    facts = _tap(branch_head=_SHA, branch_formula=_formula(_V), open_prs=1, local_branch=True)
    assert rp.tap_next(facts) == ("prove", f"{_BRANCH} already pushed with its pull request open")


def test_a_pushed_branch_without_a_pull_request_gets_one():
    assert rp.tap_next(_tap(branch_head=_SHA, branch_formula=_formula(_V), open_prs=0))[0] == "open_and_prove"


def test_a_pushed_branch_holding_another_version_fails_naming_it():
    action, detail = rp.tap_next(_tap(branch_head=_SHA, branch_formula=_formula("2026.11.8")))
    assert action == "fail" and "2026.11.8" in detail and _BRANCH in detail


def test_an_unreadable_pull_request_list_on_a_pushed_branch_fails():
    assert rp.tap_next(_tap(branch_head=_SHA, branch_formula=_formula(_V), open_prs=None))[0] == "fail"
