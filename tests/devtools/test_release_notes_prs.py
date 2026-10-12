import json

from devtools.release_notes_prs import gather_component_prs, in_window, previous_tag, upgrade_facts


def _pr(number, merged_at, base="main", component="tools"):
    return {"component": component, "number": number, "title": f"pr {number}", "merged_at": merged_at, "base": base}


def test_previous_tag_is_none_when_no_tag_is_below():
    assert previous_tag(["v0.2.0", "v0.3.0"], "0.2.0") is None
    assert previous_tag([], "0.2.0") is None


def test_previous_tag_returns_the_highest_tag_below_and_keeps_the_v():
    assert previous_tag(["v0.1.0", "v0.9.0", "v0.10.0", "v0.12.0"], "0.11.0") == "v0.10.0"


def test_previous_tag_accepts_a_tag_without_a_v():
    assert previous_tag(["0.1.0", "0.2.0"], "v0.3.0") == "0.2.0"


def test_previous_tag_sorts_a_prerelease_below_its_release():
    assert previous_tag(["v0.2.0-beta.1", "v0.1.0"], "0.2.0") == "v0.2.0-beta.1"
    assert previous_tag(["v0.2.0-beta.1", "v0.2.0-beta.2"], "0.2.0") == "v0.2.0-beta.2"
    assert previous_tag(["v0.2.0"], "0.2.0-beta.1") is None


def test_previous_tag_ignores_a_non_semver_tag():
    assert previous_tag(["latest", "v1", "release-9", "v0.1.0"], "0.2.0") == "v0.1.0"


def test_in_window_drops_before_the_tag_at_the_tag_and_off_main_and_keeps_one_second_later():
    after = "2026-05-01T12:00:00+00:00"
    prs = [
        _pr(9, "2026-05-01T12:00:01Z"),
        _pr(3, "2026-05-01T11:59:59Z"),
        _pr(4, "2026-05-01T12:00:00Z"),
        _pr(5, "2026-05-02T00:00:00Z", base="release/0.2"),
    ]
    assert in_window(prs, after) == [_pr(9, "2026-05-01T12:00:01Z")]


def test_in_window_without_a_cutoff_keeps_every_main_pr_sorted_by_number():
    prs = [_pr(7, "2026-01-02T00:00:00Z"), _pr(2, "2025-01-01T00:00:00Z"), _pr(5, "2026-01-01T00:00:00Z", base="dev")]
    assert [p["number"] for p in in_window(prs, None)] == [2, 7]


def test_in_window_does_not_mutate_its_input():
    prs = [_pr(2, "2026-01-02T00:00:00Z"), _pr(1, "2026-01-01T00:00:00Z")]
    before = [dict(p) for p in prs]
    in_window(prs, None)
    assert prs == before


def test_upgrade_facts_without_migrations():
    assert upgrade_facts({"coxswain": {"version": "0.36.0"}}, []) == {
        "schema_version": "0.36.0",
        "has_migrations": False,
    }


def test_upgrade_facts_with_migrations():
    facts = upgrade_facts({"coxswain": {"version": "0.36.0"}}, ["migrations/0002_add_runs.sql"])
    assert facts == {"schema_version": "0.36.0", "has_migrations": True}


def test_gather_component_prs_runs_git_and_gh_through_the_runner():
    gh_json = json.dumps([
        {"number": 12, "title": "late", "mergedAt": "2026-05-01T12:00:01Z", "baseRefName": "main"},
        {"number": 11, "title": "at the tag", "mergedAt": "2026-05-01T12:00:00Z", "baseRefName": "main"},
        {"number": 10, "title": "early", "mergedAt": "2026-04-01T00:00:00Z", "baseRefName": "main"},
    ])
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[3] == "tag":
            return "v0.1.0\nv0.2.0\nlatest\n"
        if argv[3] == "log":
            return "2026-05-01T14:00:00+02:00\n"
        return gh_json

    previous, prs = gather_component_prs(run, "tools", "ppfenning/coxswain-tools", "/w/tools", "0.3.0")

    assert previous == "v0.2.0"
    assert prs == [
        {"component": "tools", "number": 12, "title": "late", "merged_at": "2026-05-01T12:00:01Z", "base": "main"}
    ]
    assert calls[1] == ["git", "-C", "/w/tools", "log", "-1", "--format=%cI", "v0.2.0"]
    assert calls[2][:6] == ["gh", "pr", "list", "--repo", "ppfenning/coxswain-tools", "--state"]
    assert "number,title,mergedAt,baseRefName" in calls[2]


def test_gather_component_prs_with_no_previous_tag_keeps_every_main_pr():
    gh_json = json.dumps([{"number": 1, "title": "first", "mergedAt": "2026-01-01T00:00:00Z", "baseRefName": "main"}])
    calls = []

    def run(argv):
        calls.append(argv)
        return "" if argv[3] == "tag" else gh_json

    previous, prs = gather_component_prs(run, "crew", "ppfenning/coxswain-crew", "/w/crew", "0.1.0")

    assert previous is None
    assert [p["number"] for p in prs] == [1]
    assert len(calls) == 2
