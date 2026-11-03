from agent_tools import route, sources

REPOS = {"acme/tools": "tools"}
HELD = {("acme/tools", "1647"): "row-a"}


def _issue(number: int, title: str, repo: str = "acme/tools") -> sources.Candidate:
    return sources.Candidate(title, "body", repo, f"https://github.com/{repo}/issues/{number}")


def _plan(candidates, known_issues=None, repos=REPOS):
    return route.pull_plan(
        candidates, frozenset(), repos, date="2026-10-16", source="github", known_issues=known_issues or {}
    )


def test_known_issue_is_planned_nowhere_and_not_refused():
    files, reviews, refusals = _plan([_issue(1647, "Pull dedupe")], HELD)
    assert (files, reviews, refusals) == ([], [], [])


def test_unknown_issue_plans_one_intake_file():
    files, _, refusals = _plan([_issue(1647, "Pull dedupe")])
    assert refusals == []
    assert [list(f) for f in files] == [["intake/2026-10-16-pull-dedupe.md"]]


def test_other_issue_is_still_planned_beside_a_known_one():
    files, _, refusals = _plan([_issue(1647, "Pull dedupe"), _issue(1648, "Other idea")], HELD)
    assert refusals == []
    assert [list(f) for f in files] == [["intake/2026-10-16-other-idea.md"]]


def test_same_number_from_another_repository_is_planned():
    repos = {"acme/tools": "tools", "acme/coxswain": "coxswain"}
    candidate = _issue(1647, "Pull dedupe", "acme/coxswain")
    files, _, refusals = _plan([candidate], HELD, repos)
    assert refusals == []
    assert [list(f) for f in files] == [["intake/2026-10-16-pull-dedupe.md"]]
    assert route.held_candidates([candidate], HELD) == []


def test_same_repository_and_number_is_held_with_its_label():
    candidate = _issue(1647, "Pull dedupe")
    assert route.held_candidates([candidate, _issue(1648, "Other idea")], HELD) == [(candidate, "row-a")]


def test_held_candidates_leaves_a_pr_alone():
    pr = sources.Candidate("Fix", "body", "acme/tools", "https://github.com/acme/tools/pull/1647", kind="pr")
    assert route.held_candidates([pr], HELD) == []
    assert _plan([pr], HELD)[1] == [pr.link]


def test_row_with_a_number_but_no_resolvable_repository_has_no_key_and_skips_nothing():
    row = {"issue_number": 1647, "repo": "~/elsewhere", "link": ""}
    key = route.row_issue_key(row, {"acme/tools": "~/repos/tools"})
    assert key is None
    held = {} if key is None else {key: "row-b"}
    files, _, refusals = _plan([_issue(1647, "Pull dedupe")], held)
    assert refusals == []
    assert len(files) == 1


def test_row_without_a_number_has_no_key():
    assert route.row_issue_key({"link": "https://github.com/acme/tools/issues/5"}, {}) is None


def test_link_derived_key_resolves():
    row = {"issue_number": 1647, "link": "https://github.com/acme/tools/issues/1647", "repo": ""}
    assert route.row_issue_key(row, {}) == ("acme/tools", "1647")


def test_repo_derived_key_resolves_through_tilde_and_absolute_forms():
    repo_map = {"acme/tools": "~/repos/tools"}
    tilde = {"issue_number": 1647, "link": "", "repo": "~/repos/tools"}
    absolute = {"issue_number": 1647, "link": "", "repo": "/home/user/repos/tools/"}
    assert route.row_issue_key(tilde, repo_map) == ("acme/tools", "1647")
    assert route.row_issue_key(absolute, repo_map) == ("acme/tools", "1647")
    assert route.row_issue_key(tilde, {"acme/tools": "/home/user/repos/tools"}) == ("acme/tools", "1647")
