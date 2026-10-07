from devtools import release

_TOOLS_REPO_URL = "https://github.com/ppfenning/coxswain-tools"
# Post-cut: the manifest bump landed, so cut components sit at v0.2.0 with zero commits since.
_MANIFEST = {"coxswain": {"version": "0.2.0"},
             "components": {"harness": {"repo": "org/harness", "tag": "v0.1.0"},
                            "cartridges": {"repo": "org/cartridges", "tag": "v0.2.0"},
                            "tools": {"repo": "ppfenning/coxswain-tools", "tag": "v0.2.0"}}}
_ZERO = {"harness": 0, "cartridges": 0, "tools": 0}
_ALL_TAGS = {"harness": ["v0.1.0"], "cartridges": ["v0.2.0"], "tools": ["v0.2.0"], "coxswain": ["v0.2.0"]}


def _plan(existing_tags, pinned_commits=_ZERO, manifest=_MANIFEST, version="0.2.0", **kw):
    return release.release_plan(manifest, version, existing_tags, pinned_commits=pinned_commits,
                                tools_repository_url=_TOOLS_REPO_URL, **kw)


def test_tap_only_after_the_cut_with_every_tag_present_is_exactly_the_tap_step():
    steps = _plan(_ALL_TAGS, tap_only=True)
    assert [s["kind"] for s in steps] == ["tap_formula_pr"]
    assert (steps[0]["version"], steps[0]["branch"]) == ("0.2.0", "release/0.2.0")


def test_a_normal_plan_with_the_tags_present_is_still_refused():
    steps = _plan(_ALL_TAGS, pinned_commits={"harness": 0, "cartridges": 1, "tools": 1})
    assert [s["kind"] for s in steps] == ["refuse"]
    assert "already exists" in steps[0]["detail"]


def test_tap_only_with_a_missing_tools_tag_is_refused_naming_it():
    steps = _plan({**_ALL_TAGS, "tools": ["v0.1.0"]}, tap_only=True)
    assert [s["kind"] for s in steps] == ["refuse"]
    assert "missing on tools" in steps[0]["detail"]


def test_tap_only_with_a_missing_umbrella_tag_is_refused_naming_it():
    steps = _plan({**_ALL_TAGS, "coxswain": []}, tap_only=True)
    assert "missing on coxswain" in steps[0]["detail"]


def test_tap_only_with_no_umbrella_entry_refuses_as_unknown_rather_than_passing():
    steps = _plan({k: v for k, v in _ALL_TAGS.items() if k != "coxswain"}, tap_only=True)
    assert [s["kind"] for s in steps] == ["refuse"]
    assert "tags unknown for coxswain" in steps[0]["detail"]


def test_tap_only_requires_the_tag_on_a_component_that_has_commits_even_when_not_the_tools_repo():
    steps = _plan({**_ALL_TAGS, "harness": ["v0.1.0"]}, pinned_commits={**_ZERO, "harness": 1}, tap_only=True)
    assert [s["kind"] for s in steps] == ["refuse"]
    assert "missing on harness" in steps[0]["detail"]


def test_tap_only_requires_the_tools_tag_by_slug_when_the_manifest_does_not_pin_it_at_the_new_tag():
    manifest = {**_MANIFEST, "components": {**_MANIFEST["components"],
                                            "tools": {"repo": "ppfenning/coxswain-tools", "tag": "v0.1.0"}}}
    steps = _plan({**_ALL_TAGS, "tools": ["v0.1.0"]}, manifest=manifest, tap_only=True)
    assert [s["kind"] for s in steps] == ["refuse"]
    assert "missing on tools" in steps[0]["detail"]


def test_tap_only_for_a_version_below_the_manifests_is_refused_like_a_normal_plan():
    steps = _plan({k: ["v0.1.0"] for k in _ALL_TAGS}, version="0.1.0", tap_only=True)
    assert [s["kind"] for s in steps] == ["refuse"]
    assert "is not greater than the current version 0.2.0" in steps[0]["detail"]


def test_tap_only_with_an_unreadable_remote_says_unknown_not_missing():
    steps = _plan({**_ALL_TAGS, "cartridges": None}, tap_only=True)
    assert [s["kind"] for s in steps] == ["refuse"]
    assert "tags unknown for cartridges" in steps[0]["detail"]


def test_tap_only_with_a_dirty_tap_checkout_is_the_tap_refusal():
    steps = _plan(_ALL_TAGS, tap_only=True, tap_state="dirty")
    assert [(s["kind"], s["component"]) for s in steps] == [("refuse", "tap")]
