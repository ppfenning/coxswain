"""MkDocs hook: generated pages join the nav under the page they belong to, so they get previous and next links.

`docs/_pull.py` copies every component's docs to docs/components/<name>/..., and `docs/_cli.py` writes one page
per command under docs/reference/cli/, but the nav in mkdocs.yml lists only the hand-written component pages and
the CLI reference index. MkDocs gives a page outside the nav no previous or next page, so none of them could be
stepped through (2026-10-23). `on_nav` turns each such nav entry into a section holding that page first and then
its generated pages in path order (a component's readme.md first), and recomputes the previous and next links over
the whole nav. `generated_dir` and `under` are the pure part: which directory belongs to a page, and in what order.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable, Sequence

from mkdocs.structure.nav import Navigation, Section
from mkdocs.structure.pages import Page

COMPONENTS_DIR = "components"
README = "readme.md"
# The CLI reference index; docs/_cli.py writes one page per command beside it at build time.
CLI_INDEX = "reference/cli/index.md"


def component_of(src_uri: str) -> str | None:
    """`tools` for components/tools.md, the hand-written page a component's pulled docs sit under; None otherwise."""
    head, name = posixpath.split(src_uri)
    stem, ext = posixpath.splitext(name)
    return stem if head == COMPONENTS_DIR and ext == ".md" and stem != "index" else None


def generated_dir(src_uri: str) -> str | None:
    """The directory whose generated pages sit under this nav page: components/<name>/ for a component page,
    reference/cli/ for the CLI reference index; None for any other page."""
    component = component_of(src_uri)
    if component is not None:
        return f"{COMPONENTS_DIR}/{component}/"
    return posixpath.dirname(CLI_INDEX) + "/" if src_uri == CLI_INDEX else None


def pulled_order(component: str, src_uris: Iterable[str]) -> list[str]:
    """The component's pulled pages, docs/components/<component>/..., its readme.md first, then by path."""
    return under(f"{COMPONENTS_DIR}/{component}/", src_uris)


def under(prefix: str, src_uris: Iterable[str]) -> list[str]:
    """The pages under prefix, its readme.md first, then by path."""
    pulled = [uri for uri in src_uris if uri.startswith(prefix) and uri.endswith(".md")]
    return sorted(pulled, key=lambda uri: (uri != prefix + README, uri))


def _pages(items: Sequence) -> list[Page]:
    found: list[Page] = []
    for item in items:
        if isinstance(item, Page):
            found.append(item)
        elif isinstance(item, Section):
            found.extend(_pages(item.children))
    return found


def _link(pages: Sequence[Page]) -> None:
    for before, page, after in zip([None, *pages[:-1]], pages, [*pages[1:], None], strict=True):
        page.previous_page = before
        page.next_page = after


def on_nav(nav: Navigation, config, files) -> Navigation:
    by_uri = {f.src_uri: f.page for f in files.documentation_pages() if f.page is not None}
    in_nav = {page.file.src_uri for page in nav.pages}
    for section in (item for item in nav.items if isinstance(item, Section)):
        for index, child in enumerate(section.children):
            prefix = generated_dir(child.file.src_uri) if isinstance(child, Page) else None
            pulled = [by_uri[uri] for uri in under(prefix, by_uri) if uri not in in_nav] if prefix else []
            if not pulled:
                continue
            group = Section(child.title or prefix.rstrip("/"), [child, *pulled])
            group.parent = section
            for page in group.children:
                page.parent = group
            section.children[index] = group
    nav.pages = _pages(nav.items)
    _link(nav.pages)
    return nav
