"""The pure planner over coxswain's manifest: what `cox install --dry-run`
prints and `cox versions` tabulates. No filesystem, subprocess or clock here
— `cli.py` gathers facts at the edge and hands them in as plain data."""

from __future__ import annotations

from collections.abc import Mapping

from agent_tools import release_version
from agent_tools.upgrade_plan import release_tag

# The one checkout every install has. It is not a manifest component: the manifest lives inside it, so its pin
# is the manifest's own `[coxswain] version`. Only optional extras (dash) stay `[components]`.
CHECKOUT = "coxswain"
CHECKOUT_REPO = "ppfenning/coxswain"


def checkout_spec(manifest: Mapping) -> dict:
    """The checkout as a component spec: the repository (`[coxswain] repo`, else the default), the release tag of
    `[coxswain] version`, and `main` for the edge channel."""
    table = manifest.get("coxswain", {})
    version = table.get("version")
    return {"repo": table.get("repo", CHECKOUT_REPO), "tag": release_tag(str(version)) if version else None,
            "ref": table.get("ref", "main"), "required": True}


def clone_argv(kind: str, version: str, repo: str, path: str) -> list[str] | str:
    """The git argv that clones a release checkout into `path`, `[]` for a checkout there, else a refusal."""
    if kind == "absent" and release_version.is_release(version):
        return ["git", "clone", "--branch", release_version.release_tag(version), f"https://github.com/{repo}.git", path]
    if kind == "absent":
        return (f"refusing: cox {version} is not a release, so there is no tag to clone; "
                f"clone the repository into {path} or pass --manifest")
    if kind == "dir":
        return []
    return f"refusing: {path} is a {kind}, not a checkout"


# `--with` flags of components that are folders of the checkout now and so always install. Accepted, with a note,
# for one release so old scripts (and the rehearsal of a 0.47 install) keep working; then refused as unknown.
RETIRED_FLAGS = {"crew": "--with crew does nothing now: crew is part of the checkout and always installs"}


def drop_retired_flags(with_flags: list[str]) -> tuple[list[str], list[str]]:
    """`(flags, notes)`: the flags without the retired ones, and one note per retired flag given, first sight order."""
    retired = [f for f in dict.fromkeys(with_flags) if f in RETIRED_FLAGS]
    return [f for f in with_flags if f not in RETIRED_FLAGS], [RETIRED_FLAGS[f] for f in retired]


def _known_flags(manifest: Mapping) -> set[str]:
    return {spec["flag"] for spec in manifest.get("components", {}).values() if spec.get("flag")}


def _requested(spec: Mapping, with_flags: set[str]) -> bool:
    return bool(spec.get("required")) or spec.get("flag") in with_flags


CHANNELS = ("release", "edge")


def _pinned(spec: Mapping, channel: str) -> str | None:
    return spec.get("ref", "main") if channel == "edge" else spec.get("tag")


def _installed(checkout: Mapping, channel: str) -> str | None:
    return checkout.get("branch") if channel == "edge" else checkout.get("tag")


def _at_pin(checkout: Mapping, pinned: str | None, channel: str) -> bool:
    """On edge, at the pin means on that branch at its tip (`branch_tip`)."""
    return _installed(checkout, channel) == pinned and (channel != "edge" or bool(checkout.get("branch_tip")))


def _channel_refusal(requested: list, channel: str) -> dict | None:
    """All or nothing: a release install may not include a branch-only pin."""
    if channel not in CHANNELS:
        return {"kind": "refuse", "component": "channel", "detail": f"unknown channel: {channel}"}
    branch_only = [name for name, spec in requested if channel == "release" and spec.get("ref") and not spec.get("tag")]
    if not branch_only:
        return None
    lead = "mixed channels" if len(branch_only) < len(requested) else "branch-only manifest"
    return {"kind": "refuse", "component": branch_only[0],
            "detail": f"{lead}: {branch_only[0]} pins a branch, not a tag; --edge moves every component or none"}


def _checkout_step(name: str, spec: Mapping, checkout: Mapping, channel: str = "release") -> dict:
    """`refuse` (dirty) beats everything, then absent is `clone`, off the pin is `fetch_checkout`, at it `skip`."""
    pinned = _pinned(spec, channel)
    if checkout.get("dirty"):
        return {"kind": "refuse", "component": name, "detail": f"{name} checkout is dirty; refusing to touch it"}
    if not checkout.get("present"):
        return {"kind": "clone", "component": name, "detail": f"clone {spec.get('repo')} at {pinned}"}
    if not _at_pin(checkout, pinned, channel):
        return {"kind": "fetch_checkout", "component": name, "detail": f"{_installed(checkout, channel)} -> {pinned}"}
    return {"kind": "skip", "component": name, "detail": f"already at {pinned}"}


def _own_checkout_step(spec: Mapping, checkout: Mapping, channel: str) -> dict:
    """The one checkout holds the manifest, so on the release channel a clean checkout already present is the
    release, at a tag or not (a candidate commit): installed as is. Moving it is `cox upgrade`'s job."""
    if channel == "release" and checkout.get("present") and not checkout.get("dirty") \
            and not _at_pin(checkout, _pinned(spec, channel), channel):
        at = checkout.get("tag") or "an untagged commit"
        return {"kind": "skip", "component": CHECKOUT, "detail": f"present at {at}; installed as is (cox upgrade moves it)"}
    return _checkout_step(CHECKOUT, spec, checkout, channel)


def _desktop_step(name: str, spec: Mapping) -> dict:
    return {"kind": "desktop", "component": name, "detail": f"desktop entry at {spec.get('path')}"}


def plan(manifest: Mapping, facts: Mapping, options: Mapping) -> list[dict]:
    """Steps in manifest order: `clone`/`fetch_checkout`/`skip`/`refuse` for
    the checkout, then for each requested component, then one `setup_install`, one
    `doctor`, then `desktop` for each requested component that carries a
    `path`. An unknown `--with` flag or an unsupported provider refuses the
    whole plan down to the one step naming why."""
    provider = options.get("provider")
    providers = manifest.get("providers", {})
    status = providers.get(provider, {}).get("status")
    if status != "supported":
        reason = (f"provider {provider!r} is not in the manifest" if provider not in providers
                   else f"provider {provider!r} is {status!r}, not supported")
        return [{"kind": "refuse", "component": provider, "detail": reason}]

    with_flags = set(options.get("with") or [])
    unknown = sorted(with_flags - _known_flags(manifest))
    if unknown:
        flag = unknown[0]
        return [{"kind": "refuse", "component": flag, "detail": f"unknown --with flag: {flag}"}]

    checkouts = facts.get("checkouts", {})
    components = [(name, spec) for name, spec in manifest.get("components", {}).items() if _requested(spec, with_flags)]
    requested = [(CHECKOUT, checkout_spec(manifest)), *components]
    channel = options.get("channel", "release")
    refusal = _channel_refusal(components, channel)
    if refusal:
        return [refusal]
    empty_checkout = {"present": False, "tag": None, "dirty": False}
    checkout_steps = [(_own_checkout_step(spec, checkouts.get(name, empty_checkout), channel) if name == CHECKOUT
                       else _checkout_step(name, spec, checkouts.get(name, empty_checkout), channel))
                      if _pinned(spec, channel) else
                      {"kind": "skip", "component": name, "detail": "the manifest names no [coxswain] version"}
                      for name, spec in requested]
    setup_install_step = {"kind": "setup_install", "component": "setup_install",
                           "detail": f"channel={channel} team={options.get('team')} workspace={options.get('workspace')}"}
    doctor_step = {"kind": "doctor", "component": "doctor",
                   "detail": f"run cox setup doctor; provider CLI on PATH: {facts.get('provider_cli_on_path')}"}
    desktop_steps = [_desktop_step(name, spec) for name, spec in requested if spec.get("path")]
    return checkout_steps + [setup_install_step, doctor_step] + desktop_steps


def _component_row(name: str, spec: Mapping, checkouts: Mapping, channel: str = "release") -> tuple:
    pinned = _pinned(spec, channel)
    checkout = checkouts.get(name)
    if not checkout or not checkout.get("present"):
        return (name, pinned, None, "missing")
    return (name, pinned, _installed(checkout, channel), "ok" if _at_pin(checkout, pinned, channel) else "drift")


def rows(manifest: Mapping, facts: Mapping, channel: str = "release") -> list[tuple]:
    """`(component, pinned_tag, installed_tag, status)` for the checkout and every manifest
    component (`missing` absent, `drift` present off its pin, `ok` present at
    it) plus one `extra` row per checkout present in `facts` that the
    manifest never declared. On `edge` the pin and installed columns hold branches."""
    components = {CHECKOUT: checkout_spec(manifest), **manifest.get("components", {})}
    checkouts = facts.get("checkouts", {})
    component_rows = [_component_row(name, spec, checkouts, channel) for name, spec in components.items()]
    extra_rows = [(name, None, checkout.get("tag"), "extra") for name, checkout in checkouts.items()
                  if name not in components and checkout.get("present")]
    return component_rows + extra_rows
