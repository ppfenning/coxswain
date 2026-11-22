"""`cox inbox`, registered (F12): the real installed `cox` reads every source against a real store.

An empty sandbox has nothing waiting, so the listing is empty and exits 0, and an id that names nothing exits 1 with
a line that says so. Nothing is stubbed.
"""

from __future__ import annotations

import json


def test_an_empty_inbox_lists_nothing_and_an_unknown_id_is_refused(sandbox):
    listed = sandbox.cox("inbox", "--json")
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert json.loads(listed.stdout) == []

    refused = sandbox.cox("inbox", "accept", "0000")
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "no inbox item with id '0000'" in refused.stderr
