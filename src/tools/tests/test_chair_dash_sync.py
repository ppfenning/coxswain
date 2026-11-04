from agent_tools import chair_dash_sync


def test_dash_landing_plans_pull_build_link():
    assert chair_dash_sync.rebuild_argvs("/r/coxswain-dash", "/r/coxswain-dash", "/b", True) == [
        ["git", "-C", "/r/coxswain-dash", "pull", "--ff-only"],
        ["cargo", "build", "--release", "--manifest-path", "/r/coxswain-dash/Cargo.toml"],
        ["ln", "-sf", "/r/coxswain-dash/target/release/towpath", "/b/towpath"],
        ["ln", "-sf", "/r/coxswain-dash/target/release/coxtop", "/b/coxtop"],
    ]


def test_trailing_slash_still_matches():
    argvs = chair_dash_sync.rebuild_argvs("/r/coxswain-dash/", "/x/coxswain-dash", "/b", True)
    assert argvs[0] == ["git", "-C", "/x/coxswain-dash", "pull", "--ff-only"]


def test_coxswain_landing_plans_nothing():
    assert chair_dash_sync.rebuild_argvs("/r/coxswain", "/r/coxswain-dash", "/b", True) == []


def test_no_toolchain_plans_nothing():
    assert chair_dash_sync.rebuild_argvs("/r/coxswain-dash", "/r/coxswain-dash", "/b", False) == []


def test_missing_dash_dir_plans_nothing():
    assert chair_dash_sync.rebuild_argvs("/r/coxswain-dash", None, "/b", True) == []


def test_run_stops_at_first_failure():
    seen: list[list[str]] = []
    codes = iter([(0, ""), (2, "boom"), (0, "")])

    def run(argv: list[str]) -> tuple[int, str]:
        seen.append(argv)
        return next(codes)

    assert chair_dash_sync.run_argvs([["a"], ["b"], ["c"]], run) == 2
    assert seen == [["a"], ["b"]]


def test_run_returns_zero_on_success_and_on_empty():
    assert chair_dash_sync.run_argvs([["a"], ["b"]], lambda argv: (0, "")) == 0
    assert chair_dash_sync.run_argvs([], lambda argv: (1, "")) == 0
