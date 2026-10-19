"""Pure refresh report: vendor model ids newer than the catalog's newest per family.

    refresh_report(catalog_ids, vendor_ids) -> RefreshReport

No I/O. The vendor list carries no prices, so a stub leaves them unset (None).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from core.catalog import parse_model_id

__all__ = ["RefreshReport", "RowStub", "refresh_report"]


@dataclass(frozen=True)
class RowStub:
    """The catalog row a new id would need. None means not yet known, not zero."""

    id: str
    family: str
    input: float | None = None
    output: float | None = None
    cache_write: float | None = None
    cache_read: float | None = None


@dataclass(frozen=True)
class RefreshReport:
    newer: dict[str, tuple[RowStub, ...]]  # family -> stubs, oldest first
    unknown: tuple[str, ...]  # unparseable ids, or families the catalog has no row for


def refresh_report(catalog_ids: Iterable[str], vendor_ids: Iterable[str]) -> RefreshReport:
    """A family is known only if a catalog id parses into it; equal versions are not newer."""
    parsed_catalog = [p for i in catalog_ids if (p := parse_model_id(i)) is not None]
    baseline = {
        fam: max(v for f, v in parsed_catalog if f == fam)
        for fam in {f for f, _ in parsed_catalog}
    }
    vendor = [(i, parse_model_id(i)) for i in sorted(set(vendor_ids))]
    unknown = tuple(i for i, p in vendor if p is None or p[0] not in baseline)
    fresh = sorted(
        (p[1], i, p[0])
        for i, p in vendor
        if p is not None and p[0] in baseline and p[1] > baseline[p[0]]
    )
    newer = {
        fam: tuple(RowStub(id=i, family=fam) for _, i, f in fresh if f == fam)
        for fam in sorted({f for _, _, f in fresh})
    }
    return RefreshReport(newer=newer, unknown=unknown)
