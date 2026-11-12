import pytest

from graphs.delivery.repo_layout import LAYOUT, foreign_repo, repo_key, repo_of, repo_path


@pytest.mark.parametrize(
    "path",
    ["src/tools/x.py", "src/graphs/x.md", "src/cartridges/x.py", "devtools/x.py", "crew/x.md"],
)
def test_coxswain_roots(path):
    assert repo_of(path) == "coxswain"


@pytest.mark.parametrize("path", ["Cargo.toml", "Cargo.lock", "themes/x.toml", "src/ui/x.rs", "src/main.rs"])
def test_dash_roots(path):
    assert repo_of(path) == "coxswain-dash"


def test_new_marker_is_stripped():
    assert repo_of("src/tools/x.py (new)") == "coxswain"
    assert repo_of("src/ui/x.rs (new)") == "coxswain-dash"


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/x.md",
        "tests/x.py",
        "src/nested/x.rs",
        "src/x.py",
        "",
        " (new)",
        "/abs/src/tools/x.py",
        "~/src/tools/x.py",
        "<surface>",
        "$HOME/src/tools/x.py",
        "{a,b}/x.py",
        "src/tools/x y.py",
    ],
)
def test_unknown_and_rooted_paths_give_none(path):
    assert repo_of(path) is None


def test_repo_key():
    assert repo_key("~/repos/coxswain") == "coxswain"
    assert repo_key("/srv/work/repos/coxswain-dash") == "coxswain-dash"
    assert repo_key("coxswain") == "coxswain"
    assert repo_key("~/repos/coxswain/") == "coxswain"
    assert repo_key("~/repos/other") is None
    assert repo_key("") is None


def test_repo_path():
    assert repo_path("coxswain") == "~/repos/coxswain"
    assert repo_path("coxswain-dash") == "~/repos/coxswain-dash"
    with pytest.raises(KeyError):
        repo_path("other")


def test_foreign_repo():
    assert foreign_repo("src/tools/x.py", "coxswain") is None
    assert foreign_repo("src/ui/x.rs", "coxswain") == "coxswain-dash"
    assert foreign_repo("Cargo.toml", "coxswain-dash") is None
    assert foreign_repo("devtools/x.py", "coxswain-dash") == "coxswain"
    assert foreign_repo("docs/x.md", "coxswain") is None
    assert foreign_repo("/abs/Cargo.toml", "coxswain") is None


def test_layout_keys_round_trip():
    assert all(repo_key(repo_path(row.key)) == row.key for row in LAYOUT)
