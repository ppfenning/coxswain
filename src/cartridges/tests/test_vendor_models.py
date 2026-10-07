import json

from core.vendor_models import (
    CLAUDE_MODELS_ARGV,
    FetchError,
    list_anthropic_models,
    list_claude_models,
    parse_anthropic_page,
    parse_claude_models,
)

PAGE_ONE = json.loads(
    '{"data": [{"id": "claude-opus-5-5", "type": "model"}, {"id": "claude-sonnet-5-5", "type": "model"}],'
    ' "has_more": true, "first_id": "claude-opus-5-5", "last_id": "claude-sonnet-5-5"}'
)
PAGE_TWO = json.loads(
    '{"data": [{"id": "claude-haiku-4-5", "type": "model"}], "has_more": false,'
    ' "first_id": "claude-haiku-4-5", "last_id": "claude-haiku-4-5"}'
)
EMPTY = json.loads('{"data": [], "has_more": false, "first_id": null, "last_id": null}')
ERROR_BODY = json.loads('{"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}')
TRUNCATED = json.loads('{"data": [{"id": "claude-opus-5-5", "type": "model"}], "has_more": true, "last_id": null}')


def fake_get(pages):
    calls = []

    def get(url, headers, params):
        calls.append((url, headers, params))
        return pages[len(calls) - 1]

    get.calls = calls
    return get


def test_parse_page_with_more_and_last_page():
    assert parse_anthropic_page(PAGE_ONE) == (["claude-opus-5-5", "claude-sonnet-5-5"], "claude-sonnet-5-5")
    assert parse_anthropic_page(PAGE_TWO) == (["claude-haiku-4-5"], None)


def test_parse_empty_page_is_success_only_when_it_says_so():
    assert parse_anthropic_page(EMPTY) == ([], None)
    assert isinstance(parse_anthropic_page({}), FetchError)


def test_parse_error_body_is_error():
    assert parse_anthropic_page(ERROR_BODY) == FetchError("anthropic error body: authentication_error")


def test_parse_has_more_without_last_id_is_error():
    assert isinstance(parse_anthropic_page(TRUNCATED), FetchError)


def test_parse_claude_output_strips_decoration_and_duplicates():
    out = "\n- claude-opus-5-5\n* `claude-sonnet-5-5`\n\nclaude-opus-5-5\n"
    assert parse_claude_models(out) == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert parse_claude_models("") == []


def test_parse_claude_rejects_non_id_lines():
    assert isinstance(parse_claude_models("claude-opus-5-5\nwell-known\n"), FetchError)
    assert isinstance(parse_claude_models("Here are the models:\nclaude-opus-5-5\n"), FetchError)
    assert isinstance(parse_claude_models("claude-sonnet\n"), FetchError)


def test_anthropic_two_pages_joined_in_order_with_key_in_headers_only():
    get = fake_get([PAGE_ONE, PAGE_TWO])
    assert list_anthropic_models("sk-secret", get) == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"]
    (url0, headers0, params0), (_, _, params1) = get.calls
    assert headers0["x-api-key"] == "sk-secret"
    assert "sk-secret" not in url0 + repr(params0) + repr(params1)
    assert "after_id" not in params0
    assert params1["after_id"] == "claude-sonnet-5-5"


def test_anthropic_empty_list():
    assert list_anthropic_models("k", fake_get([EMPTY])) == []


def test_anthropic_error_body_on_second_page_fails_whole_list():
    assert list_anthropic_models("k", fake_get([PAGE_ONE, ERROR_BODY])) == FetchError(
        "anthropic error body: authentication_error"
    )


def test_anthropic_truncated_pagination_fails():
    assert isinstance(list_anthropic_models("k", fake_get([TRUNCATED])), FetchError)


def test_anthropic_get_raising_returns_error_without_key():
    def get(url, headers, params):
        raise TimeoutError("sk-secret timed out")

    result = list_anthropic_models("sk-secret", get)
    assert isinstance(result, FetchError)
    assert "sk-secret" not in result.reason


def test_anthropic_stuck_cursor_stops():
    stuck = {"data": [{"id": "claude-x-1"}], "has_more": True, "last_id": "claude-x-1"}
    assert isinstance(list_anthropic_models("k", lambda url, headers, params: stuck), FetchError)


def test_claude_models_parsed_with_argv():
    seen = []

    def run(argv):
        seen.append(argv)
        return 0, "claude-opus-5-5\nclaude-sonnet-5-5\n"

    assert list_claude_models(run) == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert seen == [list(CLAUDE_MODELS_ARGV)]


def test_claude_failures_are_values():
    def run(argv):
        raise FileNotFoundError("claude")

    assert list_claude_models(lambda argv: (1, "claude-opus-5-5")) == FetchError("claude command exited 1")
    assert isinstance(list_claude_models(run), FetchError)
