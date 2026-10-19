"""`cartridge providers refresh`: report vendor models newer than the catalog's newest per family. Read-only.

Breadcrumbs, 2026-10-07:
- This repo has no `cox` binary. The command is `cartridge providers refresh`, dispatched from `core.cartridge._main`.
- Only `ModelEntry.id` feeds the comparison. Aliases such as `opus` or `claude-haiku-4-5` are not model ids.
- The printed stubs carry no prices on purpose: the vendor list has none, so they print as `unset`, never `0`.
- Nothing here opens a catalog file for writing. A human copies a stub into providers/catalog.yaml.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from core.catalog import load_catalog
from core.refresh import RefreshReport, RowStub, refresh_report
from core.vendor_models import FetchError, list_anthropic_models

__all__ = ["main", "render_report", "run"]

_DEFAULT_CATALOG = Path(__file__).resolve().parent.parent / "providers" / "catalog.yaml"
_API_KEY_ENV = "ANTHROPIC_API_KEY"
_TIMEOUT_SECONDS = 30


def _price(value: float | None) -> str:
    return "unset" if value is None else str(value)


def _stub_lines(stub: RowStub) -> list[str]:
    prices = ", ".join(
        f"{k}: {_price(v)}"
        for k, v in (
            ("input", stub.input),
            ("output", stub.output),
            ("cache_write", stub.cache_write),
            ("cache_read", stub.cache_read),
        )
    )
    return [f"  - id: {stub.id}", f"    family: {stub.family}", f"    price: {{{prices}}}"]


def render_report(report: RefreshReport) -> str:
    """One block per family with newer ids, oldest first, then the ids the catalog cannot place."""
    newer = [
        line
        for family, stubs in report.newer.items()
        for line in (f"newer in {family}:", *[ln for stub in stubs for ln in _stub_lines(stub)])
    ]
    unknown = ["unknown (unparseable, or no catalog row for the family):", *[f"  - {i}" for i in report.unknown]]
    return "\n".join(
        [
            *(newer if newer else ["no vendor model is newer than the catalog"]),
            *(unknown if report.unknown else []),
        ]
    )


def run(
    argv: Sequence[str],
    *,
    catalog_path: Path,
    api_key: str,
    get: Callable[[str, dict, dict], object],
    out: TextIO,
    err: TextIO,
) -> int:
    """Load the catalog, list vendor ids, print the report. Exit 2 on a bad catalog, 1 on a failed vendor list."""
    parser = argparse.ArgumentParser(prog="cartridge providers refresh", description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", type=Path, default=catalog_path, help="catalog file to read (never written)")
    args = parser.parse_args(argv)

    catalog = load_catalog(args.catalog)
    if isinstance(catalog, list):
        print("\n".join(catalog), file=err)
        return 2
    vendor = list_anthropic_models(api_key, get)
    if isinstance(vendor, FetchError):
        print(vendor.reason, file=err)
        return 1
    print(render_report(refresh_report([e.id for e in catalog.entries], vendor)), file=out)
    return 0


def _urllib_get(url: str, headers: dict, params: dict) -> object:
    """JSON body of a GET. A non-2xx status raises HTTPError, which the fetcher turns into a FetchError."""
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers=headers)
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv: Sequence[str]) -> int:
    api_key = os.environ.get(_API_KEY_ENV, "")
    if not api_key:
        print(f"{_API_KEY_ENV} is not set; it is needed to list the Anthropic models", file=sys.stderr)
        return 2
    return run(argv, catalog_path=_DEFAULT_CATALOG, api_key=api_key, get=_urllib_get, out=sys.stdout, err=sys.stderr)
