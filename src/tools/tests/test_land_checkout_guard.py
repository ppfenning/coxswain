import os

from agent_tools.land_checkout_guard import Verdict, is_install_managed, verdict

CLONE = "/work/umbrella"


def test_detached_plain_repo_is_refused_with_clone_hint():
    out = verdict(True, False, CLONE)
    assert "detached HEAD" in out.refusal
    assert CLONE in out.refusal
    assert "release install checkout" not in out.refusal
    assert out.notice is None


def test_detached_install_managed_is_refused_with_clone_hint():
    out = verdict(True, True, CLONE)
    assert "release install checkout" in out.refusal
    assert "detached HEAD" in out.refusal
    assert CLONE in out.refusal
    assert out.notice is None


def test_install_managed_on_a_branch_is_allowed_with_one_notice_line():
    out = verdict(False, True, CLONE)
    assert out.refusal is None
    assert out.notice and "\n" not in out.notice
    assert "the code the chair runs from" in out.notice


def test_ordinary_repo_on_a_branch_returns_neither():
    assert verdict(False, False, CLONE) == Verdict(None, None)


def test_no_clone_names_the_setting():
    assert "umbrella_clone_dir" in verdict(True, False, None).refusal
    assert "umbrella_clone_dir" in verdict(True, True, None).refusal


def test_is_install_managed_matches_exactly():
    assert is_install_managed("/opt/cox", ["/opt/cox", "/srv/other"])


def test_is_install_managed_normalises_trailing_slashes():
    assert is_install_managed("/opt/cox/", ["/opt/cox"])
    assert is_install_managed("/opt/cox", ["/opt/cox//"])


def test_is_install_managed_expands_home():
    assert is_install_managed("~/cox", [os.path.expanduser("~") + "/cox"])


def test_is_install_managed_rejects_other_paths():
    assert not is_install_managed("/opt/cox-dev", ["/opt/cox"])
    assert not is_install_managed("/opt/cox", [])
