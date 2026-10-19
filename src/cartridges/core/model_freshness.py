"""Pure freshness warnings: tiers behind their family's newest row, vendor ids the catalog lacks.

    freshness_warnings(profile, catalog, vendor_ids=None) -> list[str]

No I/O. Nothing calls this yet. The coxswain-tools doctor intake must call it with the
profile from `load_profile` and the catalog from `load_catalog`. `vendor_ids` comes from
the refresh edge; when it is None no vendor line is produced, so the check never needs
the network.
"""

from __future__ import annotations

from collections.abc import Iterable

from core.catalog import Catalog, newest_in_family, parse_model_id
from core.profile import Profile
from core.refresh import refresh_report

__all__ = ["freshness_warnings"]


def _stale_tier_lines(profile: Profile, catalog: Catalog) -> list[str]:
    ids = [e.id for e in catalog.entries]
    rows = [
        (tier, parsed[0], parsed[1], resolved.model, newest_in_family(ids, parsed[0]))
        for tier, resolved in profile.tiers.items()
        if (parsed := parse_model_id(resolved.model)) is not None
    ]
    return [
        f"tier {tier!r} (family {family}): resolves to {model}, catalog has newer {newest}"
        for tier, family, version, model, newest in rows
        if newest is not None and (parse_model_id(newest) or ("", ()))[1] > version
    ]


def _vendor_lines(catalog: Catalog, vendor_ids: Iterable[str]) -> list[str]:
    ids = [e.id for e in catalog.entries]
    report = refresh_report(ids, vendor_ids)
    return [
        f"family {family}: vendor lists {stubs[-1].id}, newer than catalog {newest_in_family(ids, family)};"
        " catalog needs a row"
        for family, stubs in report.newer.items()
    ]


def freshness_warnings(
    profile: Profile, catalog: Catalog, vendor_ids: Iterable[str] | None = None
) -> list[str]:
    """Stale-tier lines first, then vendor lines; the vendor lines exist only when `vendor_ids` is given."""
    return _stale_tier_lines(profile, catalog) + (
        _vendor_lines(catalog, vendor_ids) if vendor_ids is not None else []
    )
