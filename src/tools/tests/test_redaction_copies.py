"""`forgejo_api.redact` and `store_switch.display` still have their own copies of what `core.redaction` does.

The harness scrubs with the `core` ones, the tools with theirs; until the tools import them from `core`, they must
agree, or a scrubbed artifact and a printed URL hide different things.
"""

from __future__ import annotations

import pytest

from agent_tools import forgejo_api, store_switch
from core import redaction

TEXTS = ["plain", "a tok\x00en b tok\\x00en", "tökén in a header 't\\xf6k\\xe9n'"]
URLS = [
    "postgresql://cox:hunter2@db:5432/store?sslpassword=s3",
    "postgres://db/store?password=x&secret_key=y",
    "https://example.invalid/path",
    "sqlite:///var/runs/store.sqlite3",
]


@pytest.mark.parametrize("token", ["tok\x00en", "tökén", ""])
def test_the_tools_redact_agrees_with_core(token) -> None:
    assert [forgejo_api.redact(t, token) for t in TEXTS] == [redaction.redact(t, token) for t in TEXTS]


def test_the_tools_display_agrees_with_core() -> None:
    assert [store_switch.display(u) for u in URLS] == [redaction.display(u) for u in URLS]
