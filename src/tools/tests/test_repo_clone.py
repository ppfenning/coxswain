from agent_tools import repo_clone, route

PROFILE = {"umbrella_dir": "/opt/umbrella/", "umbrella_clone_dir": "/work/umbrella/"}


def test_install_checkout_maps_to_the_clone():
    assert repo_clone.filing_repo("/opt/umbrella", PROFILE) == "/work/umbrella"


def test_install_checkout_without_a_clone_is_refused():
    out = repo_clone.filing_repo("/opt/umbrella", {"umbrella_dir": "/opt/umbrella"})
    assert out.startswith(repo_clone.REFUSAL_PREFIX)
    assert "umbrella_clone_dir" in out


def test_unrelated_repo_is_unchanged():
    assert repo_clone.filing_repo("/work/other", PROFILE) == "/work/other"


def test_tilde_and_trailing_slash_compare_equal(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    profile = {"umbrella_dir": "~/umbrella/", "umbrella_clone_dir": "~/clone"}
    assert repo_clone.filing_repo(str(tmp_path / "umbrella"), profile) == str(tmp_path / "clone")
    assert repo_clone.working_clone({}) is None


def test_parse_profile_keeps_the_clone_key():
    assert route.parse_profile("umbrella_clone_dir: ~/x\n")["umbrella_clone_dir"] == "~/x"
