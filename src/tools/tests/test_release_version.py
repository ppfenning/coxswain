"""CalVer release tags and the legacy SemVer line below them, with literals."""
from agent_tools.release_version import is_calver, newest, pep440, previous, release_key, release_tag


def test_calver_tag_sorts_above_the_last_semver_tag():
    assert release_key("v2026.10.0") > release_key("v0.47.0")


def test_a_later_month_sorts_above_any_n_of_the_month_before():
    assert release_key("v2026.11.0") > release_key("v2026.10.12")


def test_n_compares_as_a_number_not_as_text():
    assert release_key("v2026.10.10") > release_key("v2026.10.9")


def test_a_semver_major_never_outranks_a_year():
    assert release_key("v999.0.0") < release_key("v2026.1.0")


def test_a_legacy_beta_sorts_below_its_own_release():
    assert release_key("v0.20.0-beta.1") < release_key("v0.20.0")


def test_padded_month_and_non_release_shapes_do_not_parse():
    assert release_key("v2026.01.0") is None
    assert release_key("v2026.13.0") is None
    assert release_key("nightly") is None
    assert release_key("v0.37.0-19-gaa15d67") is None


def test_is_calver_takes_the_bare_and_the_tagged_form():
    assert is_calver("2026.10.0") and is_calver("v2026.11.3")
    assert not is_calver("0.47.0")


def test_release_tag_adds_one_v():
    assert release_tag(" 2026.10.0 ") == "v2026.10.0"
    assert release_tag("v2026.10.0") == "v2026.10.0"


def test_newest_ignores_listing_order_and_prereleases():
    tags = ["v0.47.0", "v2026.10.9", "v2026.10.10", "v0.48.0-beta.1", "v2026.9.0", "dash-1"]
    assert newest(tags) == "v2026.10.10"


def test_previous_of_the_first_calver_release_is_the_last_semver_one():
    assert previous(["v0.46.0", "v0.47.0", "v2026.10.0"], "2026.10.0") == "v0.47.0"
    assert previous(["v2026.10.0", "v2026.10.1"], "v2026.11.0") == "v2026.10.1"


def test_pep440_keeps_calver_and_maps_a_legacy_beta():
    assert pep440("2026.10.0") == "2026.10.0"
    assert pep440("v0.2.0-beta.2") == "0.2.0b2"
