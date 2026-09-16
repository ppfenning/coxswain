"""Cartridge loader and validator: resolve base + team into one merged config.

CONTRACT (implement against this; see docs/CLEAN-ROOM.md — write it fresh)

    load(team, cartridges_dir) -> dict

Merge semantics:

1.  A team cartridge declares `extends: <parent>`. Resolve the chain to the
    root, then deep-merge parent-under-child. Child wins on scalar conflicts.
2.  Within EACH layer of that chain, `<layer>/cartridge.d/*.yaml` fragments
    (sorted by filename) fold over that layer, after the layer has already
    been merged into the chain — so `base`'s fragments fold before `base`
    joins `local`, and a team's fragments fold last. Each fragment is
    checked against the ACCUMULATED authority at that point: the parent
    chain, then the layer's own `cartridge.yaml`, then every fragment
    folded before it — not against the layer alone, so a fragment adding a
    kind the layer's `cartridge.yaml` never mentioned is still checked
    against whatever the parent chain declared for it. A refusal names the
    fragment file, never the team.
3.  `context` lists CONCATENATE, base-first. A team pack refines a base
    principle; it does not replace it. Order is the reading order.
4.  Resolved `context` entries are ABSOLUTE paths. Graph scripts have no
    filesystem access — they pass paths to agent nodes, which read them.
5.  Emit `cartridge_dir` and `cartridge_sha` on the resolved dict. The sha
    covers the merged config AND every context pack's content AND every
    fragment's content: changing a charter, or a fragment, changes the
    hash, which resets autonomy streaks.
6.  `crew` is the canonical key for the seats mapping. `cast` is accepted as
    a deprecated alias for one release: a layer using it resolves exactly as
    `crew` would, and is named in the resolved dict's `deprecations` list.
    The resolved dict carries both `crew` and `cast` with the same value.

Validation — refuse to resolve, loudly, when:

-   a REQUIRED role from the base is unbound
-   a team TIGHTENS nothing but LOOSENS a risk or ramp the base declared
-   a bound skill name does not resolve to exactly one skill body
-   a context path does not exist
-   a write kind names an apply_arm role that is not bound
-   a layer declares both `cast` and `crew` for the seats mapping

Fail at load, never at run. A graph that discovers a missing binding halfway
through a production sweep has already done half the damage.

CLI: `python -m core.cartridge --team <name> --json` prints the resolved
cartridge, which is what a shell injects into a graph's `args.cartridge`.
There is deliberately no inline fallback anywhere in a graph — a fallback means
the seam never gets exercised and quietly rots.

IMPLEMENTATION NOTES (decisions the contract left open)

`skill_index` is a REQUIRED keyword argument, not an optional one. The contract
says resolution must refuse when a bound name does not resolve to exactly one
body; a default of `None` would make that check quietly skippable, which is the
precise failure mode this substrate exists to prevent. Build one with
`core.skills.index_from_roots`, or hand tests a plain dict.

`cartridge_sha` hashes the merged config with `context` EXCLUDED, then hashes
each context pack's bytes in resolved order, then each fragment's bytes in the
order they were folded. Context paths are absolute, so including them in the
hashed payload would make the sha depend on where the repo happens to be
checked out — every machine would compute a different hash and no autonomy
streak would ever survive moving a directory. Content is what matters, and
content is what is hashed.

A fragment's `context` entries resolve relative to the TEAM directory (the one
holding `cartridge.yaml`), not to `cartridge.d/` itself — a fragment is
additional declaration for that layer, not a nested cartridge with its own
base path. A fragment's `extends` key, if it has one, is silently ignored:
`_chain` resolves inheritance from each layer's `cartridge.yaml` before any
fragment is read, so by the time a fragment is folded in, the chain is
already fixed.

`_fold_fragments` is the one place fragments are folded and checked against an
authority; `load` only reads files (the layer's `cartridge.yaml` and its
`cartridge.d/*.yaml`) and calls it once per layer, so an illegal loosening is
reported exactly once no matter how many fragments led to it. An empty or
comment-only fragment file parses to `None`; it folds as `{}`, exactly as if
the file were absent, rather than being refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from functools import reduce
from pathlib import Path
from typing import Any

import yaml

from core.init import init_plan, render_plan

__all__ = ["CartridgeError", "load"]

# A team may move a value toward the strict end of these orderings, never back.
RISK_ORDER: Mapping[str, int] = {"low": 0, "medium": 1, "high": 2}
RAMP_ORDER: Mapping[str, int] = {"eligible": 0, "deferred": 1, "gated": 2, "never": 3}
GATE_ORDER: Mapping[str, int] = {"ticket": 0, "phase": 1, "epic": 2, "full": 3}

# apply_arm usually names a role, but two values are literal sinks rather than
# agent roles: the shell applies it itself, or it goes out as a pull request.
NON_ROLE_APPLY_ARMS = frozenset({"shell", "pr"})

# Emitted by load(), so excluded from the payload that load() hashes. `cast`
# and `deprecations` are excluded too: `cast` mirrors `crew` exactly, and
# `deprecations` records which spelling a layer used, neither of which is
# content a team declared.
DERIVED_KEYS = frozenset({"cartridge_dir", "cartridge_sha", "cast", "deprecations", "overlay_sha"})

OVERLAY_ALLOWED_KEYS = frozenset({"context", "policy", "landing_areas", "description"})

# The flat `--team` parser's `--cartridges-dir` default and `init`'s template
# source both resolve to the package's own `cartridges/`, never the shell's
# cwd; one constant is how the two parsers are kept in agreement.
_DEFAULT_CARTRIDGES_DIR = Path(__file__).resolve().parent.parent / "cartridges"


class CartridgeError(Exception):
    """A cartridge could not be resolved, or resolved into something invalid."""


def _read_yaml(path: Path, *, empty_ok: bool = False) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CartridgeError(f"{path}: cannot read cartridge: {exc}") from exc
    if data is None and empty_ok:
        return {}
    if not isinstance(data, dict):
        raise CartridgeError(f"{path}: expected a mapping at the top level, got {type(data).__name__}")
    return data


def _normalize_crew(raw: Mapping[str, Any], label: str) -> tuple[dict[str, Any], str | None, str | None]:
    """`cast` is a deprecated alias for `crew`, treated exactly as `crew` for
    merging and validation. `label` is the layer or fragment name a problem
    or deprecation names, matching the labels `layers()` uses. A conflict is
    returned as a problem string rather than raised, so `_resolve` reports it
    alongside every other problem the chain has, in one pass.
    """
    if "cast" in raw and "crew" in raw:
        problem = f"{label}: declares both 'cast' and 'crew'; 'cast' is a deprecated alias for 'crew'"
        return dict(raw), None, problem
    if "cast" not in raw:
        return dict(raw), None, None
    normalized = dict(raw)
    normalized["crew"] = normalized.pop("cast")
    return normalized, f"{label}: rename cast to crew", None


def _chain(team: str, cartridges_dir: Path) -> list[tuple[str, Path, dict[str, Any]]]:
    """Resolve the `extends` chain, returned base-first (root ... team)."""
    seen: list[str] = []
    chain: list[tuple[str, Path, dict[str, Any]]] = []
    name: Any = team
    while name is not None:
        if not isinstance(name, str):
            raise CartridgeError(f"'extends' must name a cartridge, got {name!r}")
        if name in seen:
            raise CartridgeError("cartridge inheritance cycle: " + " -> ".join([*seen, name]))
        seen.append(name)
        directory = cartridges_dir / name
        manifest = directory / "cartridge.yaml"
        if not manifest.is_file():
            raise CartridgeError(f"no cartridge for '{name}': expected {manifest}")
        raw = _read_yaml(manifest)
        chain.append((name, directory, raw))
        name = raw.get("extends")
    chain.reverse()
    return chain


def _fragments(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Read `<directory>/cartridge.d/*.yaml`, sorted by filename.

    A directory with no `cartridge.d/` yields no fragments — a team that has
    never split anything out resolves exactly as it did before fragments
    existed. An empty or comment-only fragment reads as `{}`, as if absent.
    A fragment's `context` entries resolve against `directory` itself (the
    team directory), not against `cartridge.d/`.
    """
    frag_dir = directory / "cartridge.d"
    if not frag_dir.is_dir():
        return []
    fragments: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(frag_dir.glob("*.yaml")):
        raw = _read_yaml(path, empty_ok=True)
        frag = dict(raw)
        frag["context"] = _absolutise_context(raw, directory)
        fragments.append((path, frag))
    return fragments


def _absolutise_context(raw: Mapping[str, Any], directory: Path) -> list[str]:
    """Context paths are relative to the cartridge that declared them."""
    entries = raw.get("context", [])
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise CartridgeError(f"{directory}: 'context' must be a list, got {type(entries).__name__}")
    return [str((directory / str(entry)).resolve()) for entry in entries]


def _merge(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge child over parent. `context` concatenates; other lists replace.

    A list that is not `context` is a value, not a tree — a team that names its
    board sections means those sections, not the base's plus its own.

    `policy.pacing` is neither a risk nor a ramp, so it merges like any other
    mapping here: a team layer may set or override its fields without the
    tighten-only restriction `_loosenings` enforces on write_kinds.
    `policy.tracker` is the same: neither a risk nor a ramp, so a team layer
    may set it freely.
    """
    merged = dict(parent)
    for key, value in child.items():
        if key == "context":
            merged[key] = [*parent.get(key, []), *value]
        elif isinstance(value, Mapping) and isinstance(parent.get(key), Mapping):
            merged[key] = _merge(parent[key], value)
        else:
            merged[key] = value
    return merged


def _loosenings(parent_kinds: Mapping[str, Any], child_kinds: Mapping[str, Any], child: str) -> list[str]:
    """A team may tighten a risk or ramp. It may never loosen one."""
    problems: list[str] = []
    for kind, spec in child_kinds.items():
        base = parent_kinds.get(kind)
        if not isinstance(spec, Mapping) or not isinstance(base, Mapping):
            continue
        for field, order in (("risk", RISK_ORDER), ("ramp", RAMP_ORDER)):
            if field not in spec or field not in base:
                continue
            new, old = spec[field], base[field]
            if new not in order:
                problems.append(f"'{child}' sets {kind}.{field} to unknown value '{new}'")
            elif old in order and order[new] < order[old]:
                problems.append(
                    f"'{child}' loosens {kind}.{field} from '{old}' to '{new}'; "
                    "a team may tighten what the base declared, never loosen it"
                )
    return problems


def gate_loosening(parent: str, child: str, label: str) -> str | None:
    """Tighten-only: `child` may move `policy.gate` toward `ticket`, never toward `full`."""
    if child not in GATE_ORDER:
        return f"'{label}' sets policy.gate to unknown value '{child}'; must be one of {', '.join(GATE_ORDER)}"
    if parent in GATE_ORDER and GATE_ORDER[child] > GATE_ORDER[parent]:
        return (
            f"'{label}' loosens policy.gate from '{parent}' to '{child}'; "
            "a team may tighten what the base declared, never loosen it"
        )
    return None


def _is_plain_list(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# `landing_areas.checks` is the build/lint/test list a project layer may
# declare; an overlay's list REPLACES the base cartridge's list for that
# tree, because a project that runs `bash -n` does not also want `pytest`.
# That is the opposite of the repo-root `.agent-checks` file, which ADDS
# extra checks and is untouched by this key.
_OVERLAY_NESTED_ALLOWED = {"policy": "review_tier", "landing_areas": "checks"}

# Which allowed nested subkeys hold a list of entries rather than a mapping;
# `landing_areas.checks` is the one exception to the mapping shape the loop
# in `overlay_errors` otherwise assumes.
_OVERLAY_LIST_SUBKEYS = frozenset({"checks"})


def _checks_problems(checks: Any) -> list[str]:
    """A replacement `checks` list: non-empty, each entry a mapping with `name` and `cmd`, names unique."""
    if not _is_plain_list(checks):
        return [f"project layer overlay refuses landing_areas.checks: must be a list, got {type(checks).__name__}"]
    if not checks:
        return ["project layer overlay refuses landing_areas.checks: list must not be empty"]
    problems: list[str] = []
    seen: list[str] = []
    for entry in checks:
        if not isinstance(entry, Mapping) or "name" not in entry or "cmd" not in entry:
            problems.append(f"project layer overlay refuses checks entry {entry!r}: missing 'name' or 'cmd'")
            continue
        if not all(isinstance(entry.get(k), str) and entry[k].strip() for k in ("name", "cmd")):
            problems.append(f"project layer overlay refuses checks entry {entry!r}: 'name' and 'cmd' must be non-empty strings")
            continue
        if entry["name"] in seen:
            problems.append(f"project layer overlay refuses checks entry '{entry['name']}': duplicate name")
        seen.append(entry["name"])
    return problems


def overlay_errors(overlay: Mapping[str, Any]) -> list[str]:
    """Refused top-level keys, refused nested keys, and malformed values, named for `CartridgeError`."""
    problems = [f"project layer overlay refuses key '{key}'" for key in overlay if key not in OVERLAY_ALLOWED_KEYS]
    context = overlay.get("context")
    if context is not None and not _is_plain_list(context):
        problems.append(f"overlay key 'context' must be a list, got {type(context).__name__}")
    for key, allowed_subkey in _OVERLAY_NESTED_ALLOWED.items():
        value = overlay.get(key)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            problems.append(f"overlay key '{key}' must be a mapping, got {type(value).__name__}")
            continue
        problems += [f"project layer overlay refuses key '{key}.{sub}'" for sub in value if sub != allowed_subkey]
        sub_value = value.get(allowed_subkey)
        if sub_value is None:
            continue
        if allowed_subkey in _OVERLAY_LIST_SUBKEYS:
            problems += _checks_problems(sub_value)
        elif not isinstance(sub_value, Mapping):
            problems.append(f"overlay key '{key}.{allowed_subkey}' must be a mapping, got {type(sub_value).__name__}")
    return problems


def _overlay_review_tier(overlay: Mapping[str, Any]) -> Mapping[str, Any]:
    return _as_mapping(_as_mapping(overlay.get("policy")).get("review_tier"))


# Lists whose membership review_tier's tighten-only rule bounds in opposite
# directions: more tier2 surfaces is more scrutiny (tighter); more tier0
# patterns routes more work to the weakest review (looser).
_REVIEW_TIER_GROW_ONLY = frozenset({"tier2_surfaces"})
_REVIEW_TIER_SHRINK_ONLY = frozenset({"tier0_patterns"})


def _review_tier_problems(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> list[str]:
    """Every tighten-only violation in `overlay` against `base`'s `review_tier`."""
    problems: list[str] = []
    for key, new in overlay.items():
        old = base.get(key)
        if key in _REVIEW_TIER_GROW_ONLY or key in _REVIEW_TIER_SHRINK_ONLY:
            if not _is_plain_list(new):
                problems.append(f"overlay sets {key} to {type(new).__name__}, not a list")
            elif _is_plain_list(old):
                grows = key in _REVIEW_TIER_GROW_ONLY
                changed = [v for v in old if v not in new] if grows else [v for v in new if v not in old]
                if changed:
                    verb, allowed = ("drops", "add") if grows else ("adds", "remove")
                    problems.append(f"overlay {verb} {key} {changed}; a project layer may only {allowed} entries")
        elif isinstance(new, bool) or not isinstance(new, (int, float)):
            problems.append(f"overlay sets review_tier.{key} to {type(new).__name__}, not a number")
        elif isinstance(old, (int, float)) and not isinstance(old, bool) and new > old:
            problems.append(
                f"overlay raises review_tier.{key} from {old} to {new}; a project layer may only lower a threshold"
            )
    return problems


def apply_overlay(
    resolved: Mapping[str, Any], overlay: Mapping[str, Any] | None, overlay_dir: Path | str | None = None
) -> dict[str, Any]:
    """Merge the project layer's whitelisted keys over `resolved`, unchecked; `overlay=None` returns it unchanged."""
    if overlay is None:
        return dict(resolved)
    merged = dict(resolved)
    if "context" in overlay:
        entries = (
            [str((Path(overlay_dir) / str(entry)).resolve()) for entry in overlay["context"]]
            if overlay_dir is not None
            else list(overlay["context"])
        )
        merged["context"] = [*resolved.get("context", []), *entries]
    if "description" in overlay:
        merged["description"] = overlay["description"]
    landing_areas = _as_mapping(overlay.get("landing_areas"))
    if "checks" in landing_areas:
        merged["landing_areas"] = {**_as_mapping(resolved.get("landing_areas")), "checks": landing_areas["checks"]}
    review_tier = _overlay_review_tier(overlay)
    if review_tier:
        base_policy = _as_mapping(resolved.get("policy"))
        base_review_tier = _as_mapping(base_policy.get("review_tier"))
        merged["policy"] = {**base_policy, "review_tier": {**base_review_tier, **review_tier}}
    return merged


def _merge_overlay(
    team: str,
    merged: Mapping[str, Any],
    overlay: Mapping[str, Any],
    overlay_dir: Path | str | None,
    skill_index: Mapping[str, Sequence[Any]],
) -> dict[str, Any]:
    """`merged` with `overlay` applied, raising CartridgeError listing EVERY overlay problem found."""
    review_tier = _overlay_review_tier(overlay)
    base_review_tier = _as_mapping(_as_mapping(merged.get("policy")).get("review_tier"))
    problems = [*overlay_errors(overlay), *_review_tier_problems(base_review_tier, review_tier)]
    if problems:
        raise CartridgeError(
            f"cartridge '{team}' overlay failed to resolve ({len(problems)} problem(s)):\n  - "
            + "\n  - ".join(problems)
        )
    overlaid = apply_overlay(merged, overlay, overlay_dir)
    validation = _validate(overlaid, skill_index)
    if validation:
        raise CartridgeError(
            f"cartridge '{team}' overlay failed to resolve ({len(validation)} problem(s)):\n  - "
            + "\n  - ".join(validation)
        )
    return overlaid


def _fold_fragments(
    layer: Mapping[str, Any],
    fragments: Sequence[tuple[str, Mapping[str, Any]]],
    authority: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Fold `fragments` (sorted-filename order, already read) over `layer`.

    Each fragment is checked against the ACCUMULATED authority: `authority`
    as given (the parent chain merged with this layer's own `cartridge.yaml`),
    then every fragment folded before it. A fragment that loosens a risk or
    ramp field against that accumulated authority is a problem string naming
    the fragment's own label, never the team. Pure: takes already-read dicts,
    returns the folded layer and the problems found; nothing here reads a
    file or rebinds a running total from outside.
    """

    def step(
        acc: tuple[dict[str, Any], dict[str, Any], list[str]],
        fragment: tuple[str, Mapping[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        folded, current_authority, problems = acc
        label, frag = fragment
        frag_kinds = frag.get("write_kinds") or {}
        authority_kinds = current_authority.get("write_kinds") or {}
        frag_gate = _as_mapping(frag.get("policy")).get("gate")
        authority_gate = _as_mapping(current_authority.get("policy")).get("gate")
        gate_problem = (
            gate_loosening(authority_gate, frag_gate, label)
            if frag_gate is not None and authority_gate is not None
            else None
        )
        found = [
            *(
                _loosenings(authority_kinds, frag_kinds, label)
                if isinstance(frag_kinds, Mapping) and isinstance(authority_kinds, Mapping)
                else []
            ),
            *([gate_problem] if gate_problem else []),
        ]
        next_folded = _merge(folded, frag)
        return next_folded, next_folded, [*problems, *found]

    folded, _, problems = reduce(step, fragments, (dict(layer), dict(authority), []))
    return folded, problems


def _walk(
    chain: Sequence[tuple[str, Path, dict[str, Any]]],
) -> tuple[list[tuple[str, dict[str, Any]]], list[str], list[Path], list[str]]:
    """Walk `chain` base-first, snapshotting the resolved-so-far cartridge.

    One entry per layer right after its `cartridge.yaml` merges, labelled by
    that layer's name; one entry per fragment right after it folds, labelled
    `<layer>/cartridge.d/<file>.yaml`. Each fragment folds through
    `_fold_fragments` one at a time so a snapshot can be taken between
    fragments, checked against everything folded before it — the same
    accumulated authority `_fold_fragments` computes when given the whole
    list at once. `load` and `layers` share this walk; neither re-derives it.

    Every layer's and fragment's raw dict is normalized through
    `_normalize_crew` before it merges or folds, so `cast` and `crew` are
    interchangeable for the rest of resolution; each use of `cast` appends
    to the returned deprecations, in the order layers and fragments fold. A
    layer or fragment declaring both `cast` and `crew` appends to the
    returned problems instead of raising, alongside every other problem.
    """
    entries: list[tuple[str, dict[str, Any]]] = []
    merged: dict[str, Any] = {}
    problems: list[str] = []
    deprecations: list[str] = []
    fragment_paths: list[Path] = []
    for name, directory, raw in chain:
        normalized_layer, layer_deprecation, layer_problem = _normalize_crew(raw, name)
        if layer_deprecation:
            deprecations.append(layer_deprecation)
        if layer_problem:
            problems.append(layer_problem)
        level = dict(normalized_layer)
        level["context"] = _absolutise_context(normalized_layer, directory)
        child_kinds = level.get("write_kinds") or {}
        parent_kinds = merged.get("write_kinds") or {}
        if isinstance(child_kinds, Mapping) and isinstance(parent_kinds, Mapping):
            problems.extend(_loosenings(parent_kinds, child_kinds, name))
        child_gate = _as_mapping(level.get("policy")).get("gate")
        parent_gate = _as_mapping(merged.get("policy")).get("gate")
        if child_gate is not None and parent_gate is not None:
            gate_problem = gate_loosening(parent_gate, child_gate, name)
            if gate_problem:
                problems.append(gate_problem)
        layer_authority = _merge(merged, level)
        entries.append((name, layer_authority))
        current = layer_authority
        for path, frag in _fragments(directory):
            fragment_paths.append(path)
            label = f"{name}/cartridge.d/{path.name}"
            normalized_frag, frag_deprecation, frag_problem = _normalize_crew(frag, label)
            if frag_deprecation:
                deprecations.append(frag_deprecation)
            if frag_problem:
                problems.append(frag_problem)
            current, frag_problems = _fold_fragments(current, [(label, normalized_frag)], current)
            problems.extend(frag_problems)
            entries.append((label, current))
        merged = current
    return entries, problems, fragment_paths, deprecations


def _overlay_text(overlay: Mapping[str, Any]) -> str:
    return json.dumps(overlay, sort_keys=True, separators=(",", ":"), default=str)


def _cartridge_sha(
    merged: Mapping[str, Any],
    context_paths: Sequence[str],
    fragment_paths: Sequence[Path] = (),
    overlay: Mapping[str, Any] | None = None,
) -> str:
    payload = {k: v for k, v in merged.items() if k not in DERIVED_KEYS and k != "context"}
    digest = hashlib.sha256()
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))
    for path in context_paths:
        digest.update(b"\0")
        digest.update(Path(path).read_bytes())
    for path in fragment_paths:
        digest.update(b"\0")
        digest.update(Path(path).read_bytes())
    if overlay is not None:
        digest.update(b"\0")
        digest.update(_overlay_text(overlay).encode("utf-8"))
    return digest.hexdigest()


def _validate(merged: Mapping[str, Any], skill_index: Mapping[str, Sequence[Any]]) -> list[str]:
    problems: list[str] = []
    skills = merged.get("skills") or {}
    if not isinstance(skills, Mapping):
        return [f"'skills' must be a mapping, got {type(skills).__name__}"]

    roles = merged.get("roles") or {}
    required = roles.get("required", []) if isinstance(roles, Mapping) else []
    for role in required:
        if role not in skills:
            problems.append(f"required role '{role}' is unbound; bind it under 'skills'")

    for role, name in skills.items():
        bodies = skill_index.get(name, ())
        if len(bodies) == 1:
            continue
        if not bodies:
            problems.append(f"role '{role}' binds skill '{name}', which resolves to no skill body")
        else:
            found = ", ".join(str(b) for b in bodies)
            problems.append(f"role '{role}' binds skill '{name}', which resolves to {len(bodies)} bodies: {found}")

    # Seats bind Claude Code plugin skills (superpowers:*, example-skills:*, ...)
    # that live in the provider's plugin cache, not in the harness skill index;
    # they are not validated here. Only role bindings under `skills` are.
    for entry in merged.get("context", []):
        if not Path(entry).is_file():
            problems.append(f"context pack does not exist: {entry}")

    gate = _as_mapping(merged.get("policy")).get("gate")
    if gate is not None and gate not in GATE_ORDER:
        problems.append(f"policy.gate is set to unknown value '{gate}'; must be one of {', '.join(GATE_ORDER)}")

    write_kinds = merged.get("write_kinds") or {}
    if isinstance(write_kinds, Mapping):
        for kind, spec in write_kinds.items():
            if not isinstance(spec, Mapping):
                continue
            arm = spec.get("apply_arm")
            if arm is None or arm in NON_ROLE_APPLY_ARMS or arm in skills:
                continue
            problems.append(f"write kind '{kind}' names apply_arm '{arm}', which is not a bound role")

    crew = merged.get("crew")
    if isinstance(crew, Mapping):
        for seat, entry in crew.items():
            if isinstance(entry, Mapping) and "enabled" in entry and not isinstance(entry["enabled"], bool):
                problems.append(f"seat '{seat}' sets enabled to {entry['enabled']!r}; enabled must be a bool")

    return problems


def _resolve(
    team: str,
    cartridges_dir: Path | str,
    *,
    skill_index: Mapping[str, Sequence[Any]],
    overlay: Mapping[str, Any] | None = None,
    overlay_dir: Path | str | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Resolve `team`'s full chain, then the project layer's `overlay`, and return every snapshot `_walk` recorded.

    Raises CartridgeError listing EVERY problem found, not just the first — a
    caller fixing bindings one error per run is a caller who stops reading
    them. The last entry carries `cartridge_dir`/`cartridge_sha`, the same
    values `load` has always returned; `load` and `layers` are both thin
    reads of this one resolution, never two.
    """
    cartridges_dir = Path(cartridges_dir).expanduser().resolve()
    chain = _chain(team, cartridges_dir)
    entries, problems, fragment_paths, deprecations = _walk(chain)
    merged = entries[-1][1]

    problems = [*problems, *_validate(merged, skill_index)]
    if problems:
        raise CartridgeError(
            f"cartridge '{team}' failed to resolve ({len(problems)} problem(s)):\n  - "
            + "\n  - ".join(problems)
        )

    overlaid = merged if overlay is None else _merge_overlay(team, merged, overlay, overlay_dir, skill_index)

    final = dict(overlaid)
    final["cartridge_dir"] = str(chain[-1][1].resolve())
    final["cartridge_sha"] = _cartridge_sha(final, final.get("context", []), fragment_paths, overlay)
    final["overlay_sha"] = (
        hashlib.sha256(_overlay_text(overlay).encode("utf-8")).hexdigest() if overlay is not None else None
    )
    final["deprecations"] = deprecations
    # `cast` is a deprecated alias for `crew`; mirrored here so readers still
    # on the old name keep working. Drop this line when `cast` is removed
    # next release. `_normalize_crew` renames a layer's seats key from `cast`
    # to `crew` before it enters the payload `cartridge_sha` hashes (merge
    # rule 5), so this release changes the sha, and resets the autonomy
    # streak, for every team whose cartridge declared `cast`.
    final["cast"] = final.get("crew", {})
    return [*entries[:-1], (entries[-1][0], final)]


def load(
    team: str,
    cartridges_dir: Path | str,
    *,
    skill_index: Mapping[str, Sequence[Any]],
    overlay: Mapping[str, Any] | None = None,
    overlay_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Resolve `team` against its inheritance chain and the project layer's overlay, and validate the result.

    Raises CartridgeError listing EVERY problem found, not just the first — a
    caller fixing bindings one error per run is a caller who stops reading them.
    """
    return _resolve(team, cartridges_dir, skill_index=skill_index, overlay=overlay, overlay_dir=overlay_dir)[-1][1]


def layers(
    team: str, cartridges_dir: Path | str, *, skill_index: Mapping[str, Sequence[Any]]
) -> list[tuple[str, dict[str, Any]]]:
    """Resolve `team` and return the resolved-so-far cartridge after each step.

    One entry per layer after its `cartridge.yaml` merges (labelled `base`,
    `local`, the team...), then one entry per fragment after it folds
    (labelled `<layer>/cartridge.d/<file>.yaml`). The last entry equals
    `load(team, cartridges_dir, skill_index=skill_index)` exactly. Raises the
    same `CartridgeError` `load` raises, on the same problems.
    """
    return _resolve(team, cartridges_dir, skill_index=skill_index)


def _main(argv: Sequence[str] | None = None) -> int:
    # `init` is a separate subcommand with its own parser (`_init_main`); every
    # other argv, including none, falls through unchanged to the flat parser below.
    resolved_argv = sys.argv[1:] if argv is None else argv
    if resolved_argv and resolved_argv[0] == "init":
        return _init_main(resolved_argv[1:])

    parser = argparse.ArgumentParser(prog="python -m core.cartridge", description=__doc__.splitlines()[0])
    parser.add_argument("--team", required=True, help="team cartridge to resolve")
    parser.add_argument(
        "--cartridges-dir",
        default=_DEFAULT_CARTRIDGES_DIR,
        help="directory holding cartridge directories (default: ./cartridges)",
    )
    parser.add_argument(
        "--skills-root",
        action="append",
        default=[],
        metavar="PATH",
        help="plugin root to scan for skill bodies (repeatable)",
    )
    parser.add_argument(
        "--unverified-skills",
        action="store_true",
        help="resolve without checking that bound skills exist; prints a warning, never silent",
    )
    parser.add_argument("--json", action="store_true", help="print the resolved cartridge as JSON")
    args = parser.parse_args(resolved_argv)

    if not args.skills_root and not args.unverified_skills:
        parser.error("pass --skills-root at least once, or --unverified-skills to skip the check explicitly")

    from core.skills import index_from_roots

    index: Mapping[str, Sequence[Any]] = index_from_roots(args.skills_root)
    if args.unverified_skills:
        print("warning: skill bindings NOT verified (--unverified-skills)", file=sys.stderr)

        class _Unverified(dict):
            def get(self, key, default=None):
                return [key]

        index = _Unverified()

    try:
        resolved = load(args.team, args.cartridges_dir, skill_index=index)
    except CartridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(resolved, indent=2, sort_keys=True, default=str) if args.json else resolved["cartridge_sha"])
    return 0


def _example_team_template() -> Mapping[str, str]:
    """Read the package's bundled `cartridges/example-team/` into {relative path: text}."""
    root = _DEFAULT_CARTRIDGES_DIR / "example-team"
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _apply_init_step(step: Mapping[str, Any], *, force: bool) -> None:
    """Perform one `init_plan` step; refuse an unforceable conflict by path."""
    op = step["op"]
    if op == "mkdir":
        Path(step["path"]).mkdir(parents=True, exist_ok=True)
    elif op == "symlink":
        path, target = Path(step["path"]), Path(step["target"])
        if path.is_symlink():
            if path.resolve() == target.resolve():
                return
            if not force:
                raise ValueError(f"'{path}' already exists and does not point at '{target}'")
            path.unlink()
        elif path.exists():
            # `--force` replaces a symlink; it never replaces a real directory.
            raise ValueError(f"'{path}' already exists and is not a symlink")
        path.symlink_to(target, target_is_directory=True)
    elif op == "write":
        path = Path(step["path"])
        if path.exists() and not force:
            raise ValueError(f"'{path}' already exists")
        path.write_text(step["text"], encoding="utf-8")
    elif op == "print":
        print(step["text"])
    else:
        raise ValueError(f"unknown step op: {op!r}")


def _init_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="cartridge init", description="Scaffold a new team cartridge.")
    parser.add_argument("team", help="team slug for the new cartridge")
    parser.add_argument(
        "--cartridges-dir",
        default=Path.cwd() / "cartridges",
        help="directory to create <team> under (default: ./cartridges in the current directory, not the package's)",
    )
    parser.add_argument("--extends", default="local", choices=("base", "local"), help="parent cartridge (default: local)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    parser.add_argument("--force", action="store_true", help="replace an existing symlink or overwrite an existing file")
    args = parser.parse_args(argv)

    try:
        steps = init_plan(
            args.team,
            args.cartridges_dir,
            extends=args.extends,
            package_cartridges_dir=_DEFAULT_CARTRIDGES_DIR,
            template=_example_team_template(),
        )
        if args.dry_run:
            print(render_plan(steps))
            return 0
        # Apply every step that touches the disk first, then say where the
        # cartridge was written, then the profile lines — the README promises
        # that order, and a reader pastes the last two lines into a profile.
        for step in steps:
            if step["op"] != "print":
                _apply_init_step(step, force=args.force)
        print(f"wrote {Path(steps[0]['path']).resolve()}")
        for step in steps:
            if step["op"] == "print":
                _apply_init_step(step, force=args.force)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
