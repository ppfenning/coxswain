from core.catalog import is_family_alias, newest_in_family, parse_model_id


def test_parse_splits_family_from_integer_version():
    assert parse_model_id("claude-opus-5-5") == ("opus", (5, 5))
    assert parse_model_id("claude-sonnet-5") == ("sonnet", (5,))


def test_parse_ignores_a_trailing_date():
    assert parse_model_id("claude-haiku-4-5-20251001") == ("haiku", (4, 5))


def test_parse_rejects_a_name_with_no_version():
    assert parse_model_id("opus-latest") is None


def test_newest_prefers_5_5_over_5_over_4_5():
    ids = ["claude-opus-4-5", "claude-opus-5", "claude-opus-5-5"]
    assert newest_in_family(ids, "opus") == "claude-opus-5-5"
    assert newest_in_family(ids[:2], "opus") == "claude-opus-5"


def test_newest_treats_a_two_digit_minor_as_a_number():
    assert newest_in_family(["claude-opus-5-9", "claude-opus-5-10"], "opus") == "claude-opus-5-10"


def test_newest_ignores_other_families():
    ids = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001"]
    assert newest_in_family(ids, "haiku") == "claude-haiku-4-5-20251001"


def test_newest_in_an_unknown_family_is_none():
    assert newest_in_family(["claude-opus-5-5"], "mythos") is None


def test_bare_family_names_are_aliases():
    assert all(is_family_alias(n) for n in ("sonnet", "opus", "haiku"))


def test_family_latest_is_an_alias():
    assert is_family_alias("opus-latest")


def test_an_exact_id_is_not_an_alias():
    assert not is_family_alias("claude-opus-5-5")
    assert not is_family_alias("claude-haiku-4-5-20251001")
