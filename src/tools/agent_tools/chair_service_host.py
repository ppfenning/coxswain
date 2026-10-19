"""The install steps for a resident chair on another host, and its status line. Pure; no ssh, sudo, or systemctl runs here."""

from __future__ import annotations

import shlex
from collections.abc import Mapping

from agent_tools.host_cmd import ChairHead, sync_argvs
from agent_tools.setup_install import CHECKOUT, SYNC_ARGV

Step = tuple[list[str], bool]


def sync_or_clone(path: str, url: str, head: ChairHead | None) -> str:
    """The remote shell line for one checkout: present, it is brought to `head` the way `cox host sync` does
    (`host_cmd.sync_argvs`: fetch, then fast-forward the branch or check out the commit detached, as at a release
    tag; with no head, `pull --ff-only`); absent, it is cloned. A failed sync fails the line, never falls to a clone."""
    sync = " && ".join(shlex.join(argv) for argv in sync_argvs(path, head))
    return f"if test -d {path}; then {sync}; else git clone {url} {path}; fi"


def install_steps(
    ssh: str, root: str, repos: dict[str, str], workspace_remote: str, label: str,
    heads: Mapping[str, ChairHead] | None = None,
) -> list[Step]:
    """Argv and a run flag (False means print it for a person) for installing a resident chair on `ssh`.

    The workspace clone (`{root}/workspace` from `workspace_remote`) refuses when the host already has a plain
    directory there instead of a git checkout; that step is still listed, never dropped, so the person sees the
    failure and clears the directory themselves. Each checkout in `repos` follows the chair's commit for the
    checkout of its name in `heads` (`sync_or_clone`). The one checkout, `{root}/coxswain`, gets its venv synced and
    becomes the editable `cox` tool.
    """
    checkout = f"{root}/{CHECKOUT}"

    def _sync(name: str, url: str) -> Step:
        return (["ssh", ssh, sync_or_clone(f"{root}/{name}", url, (heads or {}).get(name))], True)

    return [
        *(_sync(name, url) for name, url in repos.items()),
        _sync("workspace", workspace_remote),
        (["ssh", ssh, f"cd {checkout} && {shlex.join(SYNC_ARGV)}"], True),
        (["ssh", ssh, f"uv tool install --force --editable '{checkout}[traces,parquet,lake,postgres]'"], True),
        (["ssh", ssh, f"cox chair service --install --label {label}"], True),
        (["sudo", "loginctl", "enable-linger", "<user>"], False),
        (["ssh", ssh, "systemctl --user enable --now coxswain-chair.service"], False),
    ]


def status_line(host: str, unit_state: str | None, chair_line: str | None, last_tick: str | None) -> str:
    return f"{host}: {unit_state or 'no unit'}; {chair_line}; {last_tick or 'no tick'}"
