"""Pull each component's README into docs/components/<name>/README.md.

The core (`plan`) is pure: given a parsed manifest, it decides which URLs to
fetch and where to write them, with no I/O. The edge (`main`) does the actual
fetching and writing, and skips a component cleanly if its fetch fails.
"""

from __future__ import annotations

import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path


def plan(manifest: dict) -> list[tuple[str, str]]:
    """Return one (url, path) pair per repo-backed component.

    Components declared with a `path` (in-repo, e.g. `desktop`) have no
    README to pull and are skipped.
    """
    components = manifest.get("components", {})
    return [
        (
            f"https://raw.githubusercontent.com/{info['repo']}/{info['tag']}/README.md",
            f"docs/components/{name}/README.md",
        )
        for name, info in components.items()
        if "repo" in info
    ]


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:
        return response.read()


def main() -> None:
    manifest = tomllib.loads(Path("manifest.toml").read_text())
    for url, path in plan(manifest):
        try:
            content = _fetch(url)
        except urllib.error.URLError as exc:
            print(f"skip: {url}: {exc}")
            continue
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


if __name__ == "__main__":
    sys.exit(main())
