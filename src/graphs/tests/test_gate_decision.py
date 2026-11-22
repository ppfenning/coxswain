"""The gate's answer table: only an explicit approval approves."""

from __future__ import annotations

import pytest

from harness.gate import decision_for


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("a", ("approved", False)),
        ("approve", ("approved", False)),
        ("e", ("approved", True)),
        ("edits", ("approved", True)),
        ("r", ("refused", False)),
        ("", ("refused", False)),
        ("yes", ("refused", False)),
        ("A", ("refused", False)),
    ],
)
def test_the_first_letter_decides_and_anything_but_an_approval_refuses(answer: str, expected: tuple[str, bool]) -> None:
    assert decision_for(answer) == expected
