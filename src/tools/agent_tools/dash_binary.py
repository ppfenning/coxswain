"""Pure rules for the dash release binary asset: names, URLs, version judgment, planner step.

No file, process, network, clock or environment access. Callers pass in what they observed.
"""

BINARIES = ("towpath", "coxtop")
DASH_REPO_URL = "https://github.com/ppfenning/coxswain-dash"

# (platform.system(), machine) to the target triple devtools/release.py publishes.
_TARGETS = {
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
}


def _bare(version: str) -> str:
    return version[1:] if version.startswith("v") else version


def host_target(system: str, machine: str) -> str | None:
    return _TARGETS.get((system, machine))


def asset_name(version: str, target: str) -> str:
    return f"towpath-v{_bare(version)}-{target}.tar.gz"


def asset_url(repo: str, tag: str, target: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{asset_name(tag, target)}"


def installed_version(output: str) -> str | None:
    """Last whitespace-separated token of `towpath --version`, without a leading v."""
    tokens = output.split()
    version = _bare(tokens[-1]) if tokens else ""
    return version or None


def binary_status(pinned_tag: str, installed: str | None) -> str:
    if installed is None:
        return "missing"
    return "ok" if installed == _bare(pinned_tag) else "drift"


def binary_step(component: str, spec: dict, target: str | None, bin_dir: str) -> dict:
    """Planner step for the dash binary; spec is the manifest component table (repo, tag)."""
    repo, tag = spec["repo"], spec["tag"]
    if target is None:
        return {
            "kind": "skip",
            "component": component,
            "detail": (
                f"no release asset for this machine; build towpath from source with "
                f"cargo install --git {DASH_REPO_URL} --tag {tag}"
            ),
        }
    return {
        "kind": "binary",
        "component": component,
        "detail": f"install {', '.join(BINARIES)} {tag} for {target} into {bin_dir}",
        "url": asset_url(repo, tag, target),
        "target": target,
        "bin_dir": bin_dir,
        "binaries": list(BINARIES),
    }
