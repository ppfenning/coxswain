"""Pull each component's declared docs into docs/components/<name>/<path>.

The core (`plan`) is pure: given a parsed manifest, it decides which URLs to
fetch and where to write them, with no I/O. A `*.md` glob entry cannot be
expanded without asking GitHub what is in the directory, so `plan` returns it
as a single unresolved item; the edge (`_run`, called from `main`) expands it
via the GitHub contents API and does the actual fetching and writing.

Dest paths preserve each doc's full relative path (not just its basename)
under docs/components/<component>/, e.g. `graphs/delivery/epic-swarm.md`
lands at `docs/components/graphs/graphs/delivery/epic-swarm.md`. This is
deliberate: the graphs component pulls both a generated page
`docs/graphs/<name>.md` and a hand-written prose doc `graphs/<phase>/<name>.md`
for the same graph, and the two share a basename. Collapsing to the basename
would make the second overwrite the first.

A doc that fails to fetch is a build failure, not a quiet skip: `_run`
counts failures and `main` exits nonzero if any planned doc did not land, so
a stale pinned tag fails the docs workflow instead of publishing the
placeholder text in its place. A directory listing that resolves but holds
no `.md` files counts the same way; a glob that silently expands to nothing
is indistinguishable from success unless something says so. `_list_dir`
returns the ref that actually resolved (the pinned tag, or `main` on a 404)
alongside the file names, and `_glob_docs` (pure) turns that ref and those
names into concrete `doc` items the same shape `plan` already produces for
listed paths; `_run` fetches and writes that one shape without caring which
kind planned it.
"""

from __future__ import annotations

import json
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path


def raw_url(repo: str, ref: str, entry: str) -> str:
    """Pure: the raw.githubusercontent URL for one file at one ref."""
    return f"https://raw.githubusercontent.com/{repo}/{ref}/{entry}"


def fallback_url(doc: dict) -> str | None:
    """Pure: the same document at `main`, or None when there is nowhere to fall back to.

    One ref rule for every path: the pinned tag when it carries the file, else
    `main`. The glob listing has always had this fallback because a tag may be
    cut before a document exists. A listed path had the tag and nothing else,
    so the same tag took the whole docs build down with it — and because the
    pull step is required, down means publishing nothing.
    """
    if not doc.get("repo") or not doc.get("entry") or doc.get("ref") == "main":
        return None
    return raw_url(doc["repo"], "main", doc["entry"])


def _item(name: str, repo: str, tag: str, entry: str) -> dict:
    if entry.endswith("/*.md"):
        directory = entry[: -len("/*.md")]
        return {
            "kind": "glob",
            "repo": repo,
            "tag": tag,
            "dir": directory,
            "dest_dir": f"docs/components/{name}/{directory}",
        }
    return {
        "kind": "doc",
        "repo": repo,
        "entry": entry,
        "ref": tag,
        "url": raw_url(repo, tag, entry),
        "dest": f"docs/components/{name}/{entry}",
    }


def plan(manifest: dict) -> list[dict]:
    """Return one item per declared doc: a resolved `doc` or an unresolved `glob`.

    Components declared with a `path` (in-repo, e.g. `desktop`) have no docs
    to pull and are skipped, as does any repo component with no `docs` list.
    """
    components = manifest.get("components", {})
    return [
        _item(name, info["repo"], info["tag"], entry)
        for name, info in components.items()
        if "repo" in info
        for entry in info.get("docs", [])
    ]


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:
        return response.read()


def _write(dest: str, content: bytes) -> None:
    destination = Path(dest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)


def _list_dir(repo: str, tag: str, directory: str) -> tuple[str, list[str]]:
    """Return the ref that resolved and the .md file names in `directory`.

    The contents API 404s for a ref that does not exist yet (a tag not
    pushed at the time docs build); `main` always resolves. The ref is
    returned alongside the names because the raw fetch for each name must
    use the ref the listing actually came from, not the ref that 404'd. A
    non-404 response that isn't a directory listing (e.g. `directory` names
    a file, not a folder) is a fetch failure too, not a crash: the caller
    treats `URLError` as one skip, same as a 404.
    """
    for ref in (tag, "main"):
        url = f"https://api.github.com/repos/{repo}/contents/{directory}?ref={ref}"
        try:
            listing = json.loads(_fetch(url))
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and ref == tag:
                continue
            raise
        else:
            if not isinstance(listing, list):
                raise urllib.error.URLError(f"{url} did not return a directory listing")
            return ref, [entry["name"] for entry in listing if entry["name"].endswith(".md")]
    raise AssertionError("unreachable: main always resolves or raises")


def _glob_docs(item: dict, ref: str, names: list[str]) -> list[dict]:
    """Turn a resolved glob directory listing into concrete `doc` items.

    Pure: takes the ref and names `_list_dir` already resolved and returns
    plain (url, dest) data in the same shape `plan` produces for a listed
    path, so `_run` fetches and writes one shape regardless of which kind
    planned it.
    """
    return [
        {
            "kind": "doc",
            "url": f"https://raw.githubusercontent.com/{item['repo']}/{ref}/{item['dir']}/{name}",
            "dest": f"{item['dest_dir']}/{name}",
        }
        for name in names
    ]


def _resolve(items: list[dict]) -> tuple[list[dict], int]:
    """Expand every `glob` item into `doc` items; count each that failed to resolve.

    A glob that lists zero `.md` files is counted as a failure: a directory
    that is supposed to hold generated pages and comes back empty means the
    pages were not generated yet, not that there is nothing to pull.
    """
    docs: list[dict] = []
    failures = 0
    for item in items:
        if item["kind"] != "glob":
            docs.append(item)
            continue
        try:
            ref, names = _list_dir(item["repo"], item["tag"], item["dir"])
        except urllib.error.URLError as exc:
            print(f"skip: {item['dir']}: {exc}")
            failures += 1
            continue
        if not names:
            print(f"skip: {item['dir']}: no .md files at {ref}")
            failures += 1
            continue
        docs.extend(_glob_docs(item, ref, names))
    return docs, failures


def _fetch_with_fallback(doc: dict, fetch=None) -> bytes:
    """Edge: fetch `doc` at its ref, and on a 404 at `main` instead.

    `fetch` is injected so the ref rule is testable without a network.
    """
    fetch = fetch or _fetch
    try:
        return fetch(doc["url"])
    except urllib.error.HTTPError as exc:
        alternate = fallback_url(doc) if exc.code == 404 else None
        if alternate is None or alternate == doc["url"]:
            raise
        print(f"note: {doc['url']} is not at that ref; falling back to main")
        return fetch(alternate)


def _run(items: list[dict]) -> int:
    """Fetch and write every planned doc; return how many did not land."""
    docs, failures = _resolve(items)
    for doc in docs:
        try:
            content = _fetch_with_fallback(doc)
        except urllib.error.URLError as exc:
            print(f"skip: {doc['url']}: {exc}")
            failures += 1
            continue
        _write(doc["dest"], content)
    return failures


def main() -> int:
    manifest = tomllib.loads(Path("manifest.toml").read_text())
    failures = _run(plan(manifest))
    if failures:
        print(f"failed: {failures} doc(s) did not pull")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
