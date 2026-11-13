"""Pure core of the chair's config reload: hash a text, diff two parsed configs, and word the report. No I/O, no clock."""

import hashlib
from collections.abc import Mapping, Sequence

# Profile settings the chair captures once at start, so a change needs a new chair. Everything else is read from the
# profile by a lambda on each tick. Sources in cli.py:
# - `_chair_run_deps`: `harness_dir`, `workspace_dir`, `ids`, `notify`, `log_retention_days`, `weekly_reset`, the
#   `chair.*` settings below, and `lane_hosts`.
# - `cartridges_dir` and `team`: `_check_capacity` reaches `chair_capacity.chair_max_in_flight`, which reads both,
#   and `_chair_run_deps` captures its result as `lane_capacity`.
# - `provider_profile`: `_chair_run` reads it once through `_lake_provider` and passes the resulting `mode` in.
# The parsed profile mapping never carries `lane_hosts` (`_chair_run_deps` reads it from the raw text), so
# `changed_keys` cannot report it. A caller that wants it reported adds the key itself when the text digests differ.
RESTART_KEYS: frozenset[str] = frozenset({
    "harness_dir",
    "workspace_dir",
    "ids",
    "notify",
    "log_retention_days",
    "weekly_reset",
    "lane_hosts",
    "provider_profile",
    "cartridges_dir",
    "team",
    "chair.stale_days",
    "chair.restart_window_s",
    "chair.restart_max_defer_s",
    "chair.launch_stagger_s",
})

# Cartridge settings captured once: the same `lane_capacity` reads the team cartridge's dispatch cap at build. The
# dispatch cap in the docket is re-read each tick, but the gate-check worker cap is not.
CARTRIDGE_RESTART_KEYS: frozenset[str] = frozenset({"policy.dispatch.max_in_flight"})

_MISSING = object()
_RESTART_MARK = " (needs restart)"


def digest(text: bytes | str) -> str:
    """The same text and its UTF-8 bytes give the same digest."""
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def _leaves(tree: Mapping, prefix: str = "") -> dict[str, object]:
    """Dotted path to value for each non-mapping value; an empty mapping holds no leaf."""

    def walk(key: object, value: object) -> dict[str, object]:
        path = f"{prefix}{key}"
        return _leaves(value, f"{path}.") if isinstance(value, Mapping) else {path: value}

    return {path: value for key, child in tree.items() for path, value in walk(key, child).items()}


def changed_keys(old: Mapping, new: Mapping) -> list[str]:
    """Sorted dotted paths of the leaves that differ, were added or were removed."""
    before, after = _leaves(old), _leaves(new)
    return sorted(k for k in before.keys() | after.keys() if before.get(k, _MISSING) != after.get(k, _MISSING))


def _under(keys: Sequence[str], roots: frozenset[str]) -> list[str]:
    return [k for k in keys if any(k == r or k.startswith(f"{r}.") for r in roots)]


def restart_keys(keys: Sequence[str]) -> list[str]:
    """The profile keys in `RESTART_KEYS` or below one, in input order."""
    return _under(keys, RESTART_KEYS)


def cartridge_restart_keys(keys: Sequence[str]) -> list[str]:
    """The cartridge keys in `CARTRIDGE_RESTART_KEYS` or below one, in input order."""
    return _under(keys, CARTRIDGE_RESTART_KEYS)


def _marked(keys: Sequence[str], restart: set[str]) -> list[str]:
    return [f"{k}{_RESTART_MARK}" if k in restart else k for k in keys]


def reload_line(profile_keys: Sequence[str], cartridge_keys: Sequence[str], needs_restart: Sequence[str]) -> str:
    """Empty when no key changed. A key is marked when it is in `needs_restart` and a restart key of its own group."""
    groups = (
        ("profile", _marked(profile_keys, set(needs_restart) & set(restart_keys(profile_keys)))),
        ("cartridge", _marked(cartridge_keys, set(needs_restart) & set(cartridge_restart_keys(cartridge_keys)))),
    )
    parts = [f"{name} {', '.join(keys)}" for name, keys in groups if keys]
    return f"chair reload: {'; '.join(parts)}" if parts else ""


def failed_line(source: str, reason: str) -> str:
    return f"chair reload: could not re-read {source}: {reason}; keeping the last good values"
