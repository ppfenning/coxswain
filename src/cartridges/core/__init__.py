"""core package: SCHEMA_VERSION declares the version of four load-bearing contracts.

SCHEMA_VERSION covers:
    core.workstore.STATES              the closed set of item states
    the item frontmatter field whitelist that read_item and write_item use
    core.workstore.ATTEMPT_KINDS       the closed set of kinds record_attempt validates
    core.ledger.REQUIRED_FIELDS        the row shape append writes and _validate checks

A change to any of the four requires bumping SCHEMA_VERSION and adding a
CHANGELOG.md section.
"""

from __future__ import annotations

SCHEMA_VERSION = "1.0"

__all__ = ["SCHEMA_VERSION"]
