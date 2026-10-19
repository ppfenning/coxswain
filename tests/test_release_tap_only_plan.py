from devtools import release

V = "2026.11.0"
# Post-cut: the version bump landed, so dash (cut this release) sits at v2026.11.0 with zero commits since.
_MANIFEST = {"coxswain": {"version": V},
             "components": {"dash": {"repo": "org/dash", "tag": f"v{V}", "flag": "dash"}}}
_ALL_TAGS = {"dash": [f"v{V}"], "coxswain": [f"v{V}"]}


def _plan(existing_tags, pinned_commits=None, manifest=_MANIFEST, version=V, **kw):
    return release.release_plan(manifest, version, existing_tags, pinned_commits=pinned_commits or {}, **kw)


def test_tap_only_after_the_cut_with_every_tag_present_is_exactly_the_tap_step():
    steps = _plan(_ALL_TAGS, tap_only=True)
    assert [s["kind"] for s in steps] == ["tap_formula_pr"]
    assert (steps[0]["version"], steps[0]["branch"], steps[0]["towpath_version"]) == (V, f"release/{V}", V)


def test_a_normal_plan_with_the_tags_present_is_still_refused():
    steps = _plan(_ALL_TAGS)
    assert [s["kind"] for s in steps] == ["refuse"]
    assert "already exists" in steps[0]["detail"]


def test_tap_only_with_a_missing_tag_on_a_dash_the_manifest_pins_at_the_release_is_refused_naming_it():
    steps = _plan({**_ALL_TAGS, "dash": ["v0.42.0"]}, tap_only=True)
    assert [s["kind"] for s in steps] == ["refuse"]
    assert "missing on dash" in steps[0]["detail"]


def test_tap_only_with_a_missing_umbrella_tag_is_refused_naming_it():
    assert "missing on coxswain" in _plan({**_ALL_TAGS, "coxswain": []}, tap_only=True)[0]["detail"]


def test_tap_only_with_no_umbrella_entry_refuses_as_unknown_rather_than_passing():
    steps = _plan({"dash": [f"v{V}"]}, tap_only=True)
    assert [s["kind"] for s in steps] == ["refuse"]
    assert "tags unknown for coxswain" in steps[0]["detail"]


def test_tap_only_does_not_ask_a_pinned_dash_for_the_tag():
    manifest = {**_MANIFEST, "components": {"dash": {"repo": "org/dash", "tag": "v0.42.0"}}}
    steps = _plan({"dash": None, "coxswain": [f"v{V}"]}, manifest=manifest, tap_only=True)
    assert [s["kind"] for s in steps] == ["tap_formula_pr"] and steps[0]["towpath_version"] == "0.42.0"


def test_tap_only_for_a_version_below_the_manifests_is_refused_like_a_normal_plan():
    steps = _plan(_ALL_TAGS, version="2026.10.0", tap_only=True)
    assert [s["kind"] for s in steps] == ["refuse"]
    assert f"is not greater than the current version {V}" in steps[0]["detail"]


def test_tap_only_with_an_unreadable_remote_says_unknown_not_missing():
    steps = _plan({**_ALL_TAGS, "dash": None}, tap_only=True)
    assert [s["kind"] for s in steps] == ["refuse"]
    assert "tags unknown for dash" in steps[0]["detail"]


def test_tap_only_with_a_dirty_tap_checkout_is_the_tap_refusal():
    steps = _plan(_ALL_TAGS, tap_only=True, tap_state="dirty")
    assert [(s["kind"], s["component"]) for s in steps] == [("refuse", "tap")]
