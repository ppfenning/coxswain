"""Hiding secrets in text that is about to be shown: a token wherever it appears, and the passwords in a URL.

The harness scrubs artifacts with these, so they live below both the harness and the tools.
`agent_tools.forgejo_api.redact` and `agent_tools.store_switch.display` (over `lake_config.redact`) still have
their own copies, and
`test_redaction` checks that those copies agree with these until the tools import them from here.
"""

from __future__ import annotations

import re
from functools import reduce

_PASSWORD = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://[^/:@]*):[^/]*@")
_QUERY_SECRET = re.compile(r"(?i)\b(password|passwd|sslpassword|secret|secret_key|access_key)=[^&\s]*")


def _token_forms(token: str) -> tuple[str, ...]:
    """The token as written, then as str and bytes reprs escape it. http.client quotes a bad header value with %r."""
    try:
        as_bytes = repr(token.encode("latin-1"))[2:-1]
    except UnicodeEncodeError:
        as_bytes = token
    return tuple(sorted({token, repr(token)[1:-1], as_bytes}, key=len, reverse=True))


def redact(text: str, token: str) -> str:
    """Pure. Replaces every occurrence of a non-empty token, including its repr-escaped forms, with `***`."""
    return reduce(lambda acc, form: acc.replace(form, "***"), _token_forms(token), text) if token else text


def display(url: str) -> str:
    """Pure. The URL with any password, in the userinfo or the query, replaced by `***`."""
    return _QUERY_SECRET.sub(r"\1=***", _PASSWORD.sub(r"\1:***@", url))
