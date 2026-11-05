"""Pure rules for the dash release binary asset: names, URLs, version judgment, planner step.

No file, process, network, clock or environment access. Callers pass in what they observed.
"""

BINARIES = ("towpath", "coxtop")
DASH_REPO_URL = "https://github.com/ppfenning/coxswain-dash"
KILLED_DETAIL = "killed at launch, likely a stale code signature"

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


def _release_part(version: str) -> str:
    """Version without a `+<commit>` suffix; a dev build reports `2026.11.1+14df4b9`."""
    return version.split("+", 1)[0]


def killed_by_signal(code: int) -> bool:
    """Negative is the subprocess signal form; 129 to 159 is the shell form 128 plus a signal."""
    return code < 0 or 129 <= code <= 159


def binary_status(pinned_tag: str, installed: str | None, *, killed: bool = False) -> str:
    if installed is None:
        return "killed" if killed else "missing"
    return "ok" if _release_part(installed) == _bare(pinned_tag) else "drift"


def dev_build_argv(dash_dir: str) -> list[str]:
    return ["cargo", "build", "--release", "--manifest-path", f"{dash_dir}/Cargo.toml"]


def dev_link_argvs(dash_dir: str, bin_dir: str) -> list[list[str]]:
    return [["ln", "-sf", f"{dash_dir}/target/release/{name}", f"{bin_dir}/{name}"] for name in BINARIES]


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
