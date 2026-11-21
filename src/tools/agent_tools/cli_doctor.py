"""`cox setup doctor` and `setup install`: the facts the doctor gathers and the rows it prints."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from pathlib import Path

import yaml

from agent_tools import smoke_warm
from agent_tools.cli_chair_service import _chair_default_environment_file, _write_private
from agent_tools.cli_common import _profile_path, _read_text_or_none
from agent_tools.cli_host import _run_ssh
from agent_tools.cli_install import _load_lazy_modules, _migrate_kind, _schema_versions
from agent_tools.cli_lazy import (
    bounded_run,
    chair_facts,
    chair_service,
    chair_unit,
    claude_token,
    doctor,
    forge,
    home_paths,
    host_cmd,
    install_exec,
    install_root,
    lane_hosts,
    lane_prereqs,
    log_doctor,
    provenance,
    remote_doctor,
    route,
    route_sync_gh,
    run_store,
    setup_install,
    setup_migrate,
    setup_migrate_hosts,
    setup_receipts,
    setup_token,
    store_dialect,
    store_url,
    unit_env,
)
from agent_tools.cli_runs_fetch import _lane_host_candidates, _profile_lane_hosts
from agent_tools.cli_setup_store import _doctor_profile, _probe_store, _unit_env_loaded
from agent_tools.cli_views import _dash_dirs


def _core_freshness(provider_profile: str | None) -> list[str] | None:
    """The model freshness lines for `provider_profile`; None for a core too old for the check (the doctor prints one skip row)."""
    if not provider_profile:
        return ["model freshness not checked: AGENT_TOOLS_PROVIDER_PROFILE is unset"]
    try:
        from core.model_freshness import freshness_warnings
    except ImportError:
        return None
    # load_profile reads the file with Path.read_text, so it takes a Path, never the raw str.
    profile_path = Path(provider_profile).expanduser()
    try:
        import core
        from core.catalog import load_catalog
        from core.profile import load_profile
        if not profile_path.is_file():
            return [f"model freshness not checked: provider profile not found at {profile_path}"]
        if not os.access(profile_path, os.R_OK):
            return [f"model freshness not checked: provider profile unreadable at {profile_path}"]
        catalog_path = Path(core.__file__).resolve().parent.parent / "providers" / "catalog.yaml"
        catalog = load_catalog(catalog_path)
        profile = catalog if isinstance(catalog, list) else load_profile(profile_path, catalog_path)
        errors = catalog if isinstance(catalog, list) else profile if isinstance(profile, list) else None
        if errors is None:
            return list(freshness_warnings(profile, catalog))
        return ["model freshness not checked: " + str(errors[0] if errors else "unknown error")]
    except Exception:
        return [f"model freshness not checked: could not evaluate the provider profile at {profile_path}"]


_HARNESS_PLUGIN_GROUPS = ("coxswain.system_one", "coxswain.runners")
_PLUGINS_PROBE_SCRIPT = (
    "import json, sys\n"
    "from importlib.metadata import entry_points\n"
    "print(json.dumps({g: sorted(ep.name for ep in entry_points(group=g)) for g in sys.argv[1:]}))\n"
)


def plugins_probe_argv(python: str) -> list[str]:
    """The harness interpreter listing its own plugin entry points: `cox` may run from another venv (a uv tool)."""
    return [python, "-c", _PLUGINS_PROBE_SCRIPT, *_HARNESS_PLUGIN_GROUPS]


def parse_plugins_probe(returncode: int, stdout: str) -> dict[str, list[str]] | None:
    """`{group: [names]}` for every probed group, else None, which the doctor row reads as "not checked", never as empty."""
    if returncode != 0:
        return None
    try:
        parsed = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or set(parsed) != set(_HARNESS_PLUGIN_GROUPS):
        return None
    if not all(isinstance(names, list) and all(isinstance(n, str) for n in names) for names in parsed.values()):
        return None
    return parsed


def _harness_plugins(python: str) -> dict[str, list[str]] | None:
    """Edge. The plugin entry points the harness interpreter sees; None when it cannot say."""
    try:
        done = subprocess.run(plugins_probe_argv(python), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_plugins_probe(done.returncode, done.stdout)


def _overlay_mapping(text: str) -> tuple[dict | None, str | None]:
    """Overlay YAML text as (mapping, None), or (None, one problem). An empty file is `{}`."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"project overlay is not valid YAML: {str(exc).strip().splitlines()[0]}"
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, f"project overlay must be a mapping, got {type(data).__name__}"
    return data, None


def _core_probe(cartridges_dir: str, team: str, roots: list, overlay_text: str | None, provider_profile: str | None) -> dict:
    """Edge. The cartridge API, imported in-process (one venv: core sits beside agent_tools), as one JSON-shaped dict.

    Each step's failure lands in its own key and never raises; a failed import or skill index stops the walk there."""
    out: dict = {"import": None, "load": None, "indexed": {}, "resolved": None, "skill_index": {}, "layers": None}
    try:
        from core.cartridge import load
        from core.skills import index_from_roots
    except Exception as exc:
        out["import"] = f"{type(exc).__name__}: {exc}"
        return out
    try:
        index = index_from_roots(roots)
        out["indexed"] = {root: len(index_from_roots([root])) for root in roots}
        out["skill_index"] = {name: [str(p) for p in paths] for name, paths in index.items()}
    except Exception as exc:
        # An empty mapping would read downstream as "no roots configured"; a zero
        # per root fails the skills row honestly, and the cartridge row names why.
        out["indexed"] = dict.fromkeys(roots, 0)
        out["load"] = f"skill index failed: {type(exc).__name__}: {exc}"
        return out
    try:
        out["resolved"] = load(team, cartridges_dir, skill_index=index)
    except Exception as exc:
        out["load"] = f"{type(exc).__name__}: {exc}"
    try:
        from core.cartridge import layers
        out["layers"] = [[label, resolved] for label, resolved in layers(team, cartridges_dir, skill_index=index)]
    except Exception as exc:
        # Never an empty list: a chain one layer short must be visibly unknown,
        # not silently read as "nothing set anything".
        out["layers"] = None
        out["layers_error"] = f"{type(exc).__name__}: {exc}"
    if overlay_text is None:
        out["overlay_errors"] = None
    else:
        try:
            from core.cartridge import overlay_errors
            mapping, problem = _overlay_mapping(overlay_text)
            out["overlay_errors"] = [problem] if problem else overlay_errors(mapping)
        except Exception as exc:
            out["overlay_errors"] = [f"{type(exc).__name__}: {exc}"]
    out["freshness"] = _core_freshness(provider_profile)
    return out


def _run_core_probe(cartridges_dir: str, team: str, skills_roots: list, raw_roots: list | None = None,
                     overlay_text: str | None = None, provider_profile: str | None = None) -> dict:
    """The core probe's facts (spec: setup doctor). Any failure the probe did not
    catch itself is folded into `core_import`, never raised."""
    try:
        parsed = _core_probe(cartridges_dir, team, list(skills_roots), overlay_text, provider_profile)
    except Exception as exc:
        return {"core_import": f"{type(exc).__name__}: {exc}"}
    indexed = parsed.get("indexed", {}) or {}
    if raw_roots and len(raw_roots) == len(skills_roots):
        # The probe saw expanded paths; the core keys its skills row by the
        # profile's own strings, so map the counts back by position.
        indexed = {raw: indexed.get(exp, 0) for raw, exp in zip(raw_roots, skills_roots)}
    facts = {"core_import": parsed.get("import"), "cartridge_load": parsed.get("load"),
             "skill_roots_indexed": indexed, "resolved": parsed.get("resolved"),
             "skill_index": parsed.get("skill_index", {})}
    if "overlay_errors" in parsed:
        facts["overlay_errors"] = parsed["overlay_errors"]
    if "freshness" in parsed:
        facts["freshness_lines"] = parsed["freshness"]
    # Exactly one of `provenance`/`provenance_error` is set below, on every
    # path: a fact dict carrying neither would read as "nothing to report"
    # rather than "the walk never happened", which is the silent-mislabel
    # failure this probe exists to rule out.
    parsed_layers = parsed.get("layers")
    if parsed_layers is None:
        facts["provenance_error"] = parsed.get("layers_error") or "the probe exited before the layer walk"
    elif not parsed_layers:
        facts["provenance_error"] = "the loader returned no layers"
    else:
        try:
            facts["provenance"] = provenance.attribute([(label, resolved) for label, resolved in parsed_layers])
        except Exception as exc:
            facts["provenance_error"] = f"{type(exc).__name__}: {exc}"
    return facts


def _provider_command(text) -> str | None:
    if text is None:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("command:"):
            # A flat YAML scalar may carry a trailing comment (`command: claude  # ...`);
            # the command is the first token, never the comment.
            value = stripped.partition(":")[2].split("#", 1)[0].strip().strip("\"'")
            return value.split()[0] if value else None
    return None


def _provider_facts(provider_profile_path: str) -> dict:
    command = _provider_command(_read_text_or_none(Path(provider_profile_path)))
    if command is None:
        return {"provider_command": None, "provider_on_path": False}
    facts = {"provider_command": command, "provider_on_path": shutil.which(command) is not None}
    if facts["provider_on_path"]:
        try:
            proc = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=20)
            lines = (proc.stdout or proc.stderr or "").strip().splitlines()
            facts["provider_version"] = lines[0] if lines else ""
        except (OSError, subprocess.TimeoutExpired):
            pass
    return facts


_NO_STORE_YET = "no store yet (it is created by the first run)"
_NO_RUNS_TABLE = "the store answers but has no runs table yet (it is created by the first run)"
_STORE_CONNECT_TIMEOUT_S = 5


def _sqlite_store_file(url: str) -> Path:
    """The file `connect_readonly_url` opens for a non-Postgres URL."""
    return Path(url.removeprefix("sqlite:///") if url.startswith("sqlite:") else url)


def _with_connect_timeout(url: str) -> str:
    """The Postgres URL with a connect timeout added unless it sets one."""
    parts = urllib.parse.urlsplit(url)
    if "connect_timeout" in urllib.parse.parse_qs(parts.query):
        return url
    query = "&".join(q for q in (parts.query, f"connect_timeout={_STORE_CONNECT_TIMEOUT_S}") if q)
    return urllib.parse.urlunsplit(parts._replace(query=query))


def _store_error(exc: Exception) -> str:
    """A fixed message for a known cause, else the exception class; driver text can quote the URL, so it is never passed on."""
    if str(exc) == store_dialect.MISSING_PSYCOPG:
        return store_dialect.MISSING_PSYCOPG
    if type(exc).__name__ == "UndefinedTable" or (isinstance(exc, sqlite3.OperationalError) and "no such table" in str(exc)):
        return _NO_RUNS_TABLE
    return f"the store did not answer ({type(exc).__name__})"


def _store_facts(provider_profile: str, workspace_dir: str) -> dict:
    """Edge. Whether the run store answers a read; the URL never leaves this function."""
    url = store_url.resolve_store_url(provider_profile, Path(workspace_dir) / "runs")
    kind = store_url.describe_store(url).removeprefix("store: ")
    try:
        postgres = store_dialect.is_postgres(url)
    except ValueError:  # urlsplit rejects e.g. an unclosed `[`; the message would quote the URL
        return {"kind": kind, "reachable": False, "runs": None, "fresh": False, "error": "storage_url is not a valid URL"}
    # `other` is a bare path with no scheme, which `connect_readonly_url` opens as a SQLite file.
    if not postgres and kind not in ("sqlite", "other"):
        return {"kind": kind, "reachable": False, "runs": None, "fresh": False,
                "error": f"unsupported store scheme {kind}: storage_url must be a postgresql:// or sqlite:/// URL"}
    if not postgres and not _sqlite_store_file(url).exists():
        return {"kind": kind, "reachable": False, "error": _NO_STORE_YET, "runs": None, "fresh": True}
    try:
        with contextlib.closing(store_dialect.connect_readonly_url(_with_connect_timeout(url) if postgres else url)) as conn:
            runs = int(conn.execute("SELECT count(*) AS n FROM runs").fetchone()["n"])
    except Exception as exc:  # any driver failure is the answer this row reports
        error = _store_error(exc)
        return {"kind": kind, "reachable": False, "error": error, "runs": None, "fresh": error == _NO_RUNS_TABLE}
    return {"kind": kind, "reachable": True, "error": None, "runs": runs, "fresh": False}


def _workspace_facts(workspace_dir: str) -> dict:
    ws = Path(workspace_dir).expanduser()
    return {"workspace_dirs": {name: (ws / name).exists() for name in ("work", "runs", "intake")}}


def _git_and_forge_facts(profile: dict) -> dict:
    """`git_version`, the profile's forge name, whether that forge is installed,
    and `gh_auth` only for the github forge. Gathered with or without a profile."""
    try:
        done = bounded_run.completed(["git", "--version"], capture_output=True, text=True)
        git_version = done.stdout.strip() if done.returncode == 0 else None
    except OSError:
        git_version = None
    name = forge.forge_name(profile)
    facts = {"git_version": git_version or None, "forge": name, "forge_found": forge.forge_for(name) is not None}
    if name in ("github", "auto"):
        try:
            facts["gh_auth"] = route_sync_gh.api_login_ok(bounded_run.completed)
        except OSError:
            facts["gh_auth"] = False
    return facts


_TOOLS_PLUGIN_GROUPS = ("coxswain.sources", "coxswain.forges", "coxswain.trackers")


def _tools_plugin_facts() -> dict:
    """Entry-point names per group, read in tools' own environment. Listing only."""
    return {g: sorted(ep.name for ep in importlib.metadata.entry_points(group=g)) for g in _TOOLS_PLUGIN_GROUPS}


def _chair_unit_facts() -> dict:
    """Edge: the installed chair unit's text, its effective PATH across unit and drop-ins, and per absolute PATH entry the names of the doctor's unit tools executable there."""
    unit_file = chair_service.unit_path(Path.home())
    text = _read_text_or_none(unit_file)
    facts: dict = {"chair_unit_text": text}
    if text is not None:
        observed = chair_unit.read_chair_path(unit_file.parent)
        facts["chair_unit_path"] = observed
        entries = [e for e in doctor.unit_path_entries(observed.path) if e.startswith("/")] if observed else []
        facts["chair_unit_tools"] = {
            e: [n for n in doctor._UNIT_TOOLS if (Path(e) / n).is_file() and os.access(Path(e) / n, os.X_OK)]
            for e in entries
        }
    return facts


def _gather_doctor_facts(profile_path: Path, repo: Path) -> dict:
    """Gathers exactly the Facts keys `doctor.checks` reads; never refuses on
    a missing or unparseable profile, since reporting that is the doctor's
    job (unlike `_resolve_profile_or_refuse`, which is for `file`/`launch`)."""
    text = _read_text_or_none(profile_path)
    facts: dict = {"profile_path": str(profile_path), "profile_text": text}
    profile = _doctor_profile(text)
    facts.update(_git_and_forge_facts(profile or {}))
    facts["plugins_tools"] = _tools_plugin_facts()
    facts.update(_chair_unit_facts())
    facts["claude_token"] = setup_token.token_fact(claude_token.token_path(str(Path.home())))
    if profile is None:
        return facts
    singles = [profile.get(k, "") for k in ("cartridges_dir", "provider_profile", "harness_dir", "workspace_dir", "umbrella_dir")]
    roots = list(profile.get("skills_roots") or [])
    # Every profile path is expanded ONCE, here; `paths_exist` keeps the raw
    # strings as keys (doctor.py's contract) but tests the expanded path.
    expand = lambda p: str(Path(p).expanduser()) if p else ""  # noqa: E731
    facts["paths_exist"] = {p: Path(expand(p)).exists() for p in (*singles, *roots) if p}
    harness_dir = expand(profile.get("harness_dir", ""))
    python = install_root.harness_python(harness_dir) if harness_dir else None
    facts["harness_python"] = str(python) if python else None
    facts["harness_python_exists"] = bool(python) and python.exists()
    if facts["harness_python_exists"]:
        plugins = _harness_plugins(str(python))
        if plugins is not None:
            facts["plugins_harness"] = plugins
        overlay_text = _read_text_or_none(repo / ".agent" / "cartridge.yaml")
        facts.update(_run_core_probe(expand(profile.get("cartridges_dir", "")), profile.get("team", ""),
                                     [expand(r) for r in roots], raw_roots=roots, overlay_text=overlay_text,
                                     provider_profile=expand(profile.get("provider_profile", "")) or None))
    if profile.get("provider_profile"):
        facts.update(_provider_facts(expand(profile["provider_profile"])))
        if profile.get("workspace_dir"):
            facts["store"] = _store_facts(expand(profile["provider_profile"]), expand(profile["workspace_dir"]))
    if profile.get("workspace_dir"):
        facts.update(_workspace_facts(expand(profile["workspace_dir"])))
    facts["schema_versions"] = _schema_versions(harness_dir, expand(profile.get("provider_profile", "")),
                                                 [expand(r) for r in roots])
    return facts


def _parquet_traces_line(profile: dict | None) -> str | None:
    """Edge: the Parquet readability line for this profile's traces root, or None when
    the profile names no workspace_dir and so no root can be resolved."""
    if not profile or not profile.get("workspace_dir"):
        return None
    provider = profile.get("provider_profile")
    provider_data = store_url.read_provider_profile(provider) if provider else {}
    root = store_url.profile_traces_root(provider_data, Path(profile["workspace_dir"]).expanduser() / "runs")
    check = run_store.parquet_readable(root)
    endpoint = root.object_store.get("endpoint") if root.object_store else None
    return doctor.parquet_line(check.readable, check.reason, str(endpoint) if endpoint else None)


def _setup_doctor_host(a: argparse.Namespace, run) -> int:
    """Prints the named lane host's doctor rows under a header; non-zero when the host is unknown or its doctor fails."""
    path = _profile_path(a)
    text = _read_text_or_none(path)
    if text is None:
        print(f"cannot read profile: {path}")
        return 1
    try:
        workspace = route.parse_profile(text).get("workspace_dir", "")
    except route.ProfileError:
        workspace = ""
    table = run_store.hosts(Path(workspace).expanduser() / "runs") if workspace else []
    hosts = _lane_host_candidates(text, table)
    if isinstance(hosts, lane_hosts.LaneHostError):
        print(hosts.message)
        return 1
    host = lane_hosts.find_lane_host(hosts, a.host)
    if host is None:
        print(f"unknown lane host: {a.host}")
        print(f"configured: {', '.join(h.name for h in hosts) or 'none'}")
        return 1
    code, rows = remote_doctor.doctor_on_host(host, run)
    print(f"doctor on {host.name} ({host.ssh})")
    print("\n".join(rows))
    return code


def _doctor_store_rows(profile: dict | None) -> list[dict]:
    """Edge. The chosen store's rows; the probes run only when store.mode is shared and the provider profile names a store."""
    profile = profile or {}
    mode = (profile.get("store") or {}).get("mode", "files")
    provider = profile.get("provider_profile")
    data = store_url.read_provider_profile(provider) if provider else {}
    runs_dir = Path(profile.get("workspace_dir") or "").expanduser() / "runs"
    traces = store_url.profile_traces_root(data, runs_dir).url
    probed = mode == "shared" and all(k in data for k in ("storage_url", "traces_url", "object_store"))
    return doctor.store_rows(mode, store_url.profile_store_url(data, runs_dir), traces, _probe_store(data, {}) if probed else [])


def _doctor_artifacts_rows(profile: dict | None) -> list[dict]:
    """Edge. The artifacts root row: when the provider profile sets `artifacts_url`, write, read back and remove a probe
    object there (`harness.artifacts.probe`), in any store mode, since a single machine may set a `file://` root."""
    profile = profile or {}
    provider = profile.get("provider_profile")
    data = store_url.read_provider_profile(provider) if provider else {}
    url = data.get("artifacts_url")
    if not isinstance(url, str) or not url:
        return []
    from harness import artifacts  # the harness loads only on a machine that sets the key

    runs_dir = Path(profile.get("workspace_dir") or "").expanduser() / "runs"
    try:
        root = artifacts.artifacts_root(data, os.environ, runs_dir)
    except ValueError as exc:
        return doctor.artifacts_rows(url, artifacts.scrub(str(exc), artifacts.secret_values(os.environ))[:200], None)
    except Exception as exc:  # pyarrow missing, or a plugin that would not load
        return doctor.artifacts_rows(url, type(exc).__name__, None)
    # Host, pid and a random part: two doctors at once, on one machine or several, never share a probe object.
    token = f"doctor-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    failed = artifacts.probe(root, token) if root is not None else None
    return doctor.artifacts_rows(url, None, failed)


def _doctor_worker_role_rows(profile: dict | None) -> list[dict]:
    """Edge. The worker store role row: when the units' env (loaded under any variable the process lacks) names a
    worker role, connect as it through `store_scoped.open_scoped`, which never migrates, and run the probes, each in
    a transaction rolled back. `doctor.worker_role_rows` judges; no row on a machine with no worker role."""
    role, password = os.environ.get(unit_env.WORKER_ROLE_ENV, ""), os.environ.get(unit_env.WORKER_PASSWORD_ENV, "")
    if not role and not password:
        return []
    provider = (profile or {}).get("provider_profile")
    data = store_url.read_provider_profile(provider) if isinstance(provider, str) else {}
    owner = data.get("storage_url")
    try:
        postgres = isinstance(owner, str) and store_dialect.is_postgres(owner)
    except ValueError:
        postgres = False
    verdict, error = None, None
    if role and password and postgres:
        from harness import store_roles, store_scoped
        from harness.store_read import StoreVersionError

        try:
            outcomes, public_create = store_scoped.probe_url(store_roles.worker_url(str(owner), role, password))
            verdict = store_roles.probe_verdict(outcomes, public_create)
        except StoreVersionError as exc:
            error = str(exc)
        except Exception as exc:  # the row reports why the role is unusable; the doctor never raises
            error = _store_error(exc)
    return doctor.worker_role_rows(bool(role), bool(password), postgres, verdict, error)


_DOCTOR_HOME_CHECKS = (
    "home root", "marketplace registered", "marketplace location", "old layout", "home paths", "home recorded",
    "cartridge links", "lane host paths",
)


def _doctor_legacy_path(profile: Mapping, locs) -> str | None:
    """Edge. A component checkout still a real directory in the installs folder, else the pre-home checkout folder
    when it is not the installs folder and holds a real coxswain-graphs directory."""
    left = [p for p in (Path(locs.installs) / name for name in setup_migrate.RETIRED) if p.is_dir() and not p.is_symlink()]
    if left:
        return str(left[0])
    old = _migrate_old_root(profile, locs)
    graphs = Path(old) / setup_migrate.GRAPHS if old else None
    return old if old and old != locs.installs and graphs.is_dir() and not graphs.is_symlink() else None


def _doctor_home_rows(profile: dict | None) -> list[dict]:
    """Edge. The coxswain home rows; every one is skipped without a profile. All reads happen here, `doctor.home_rows` judges."""
    if profile is None:
        return [doctor._skip(check) for check in _DOCTOR_HOME_CHECKS]
    locs = home_paths.resolve_here()
    if isinstance(locs, home_paths.Refused):
        return [{"check": "home root", "ok": False, "detail": locs.reason}]
    listing = _marketplace_listing()
    source = (listing or {}).get(setup_install.MARKETPLACE)
    workspace = profile.get("workspace_dir")
    in_project = bool(source and workspace and Path(source).parent == Path(workspace).expanduser().parent)
    record = _read_text_or_none(Path(locs.record))
    rows = doctor.home_rows(
        locs,
        listing,
        Path(locs.home).is_dir(),
        os.access(locs.home, os.W_OK),
        _doctor_legacy_path(profile, locs),
        in_project,
        _migrate_cartridge_links(profile),
        _doctor_install_sources(_migrate_install_bins(), setup_migrate.read_venv_source(setup_install.layout(locs.installs).checkout)),
        home_record=(record or "").strip() or None,
        skills_roots=profile.get("skills_roots") or (),
        provider_profile=profile.get("provider_profile") or "",
        harness_dir=profile.get("harness_dir") or "",
        user_home=str(Path.home()),
        workspace_dir=workspace or "",
    )
    return [*rows, doctor.lane_host_paths_row(_migrate_observe_hosts(locs))]


def _doctor_dash_build_rows(profile: dict | None) -> list[dict]:
    """Edge. The `dash build` row; none without a profile or a resolvable home. A development install is one whose
    venv is editable from the checkout, the source the `install bins` row reads."""
    if profile is None:
        return []
    locs = home_paths.resolve_here()
    if isinstance(locs, home_paths.Refused):
        return []
    checkout = setup_install.layout(locs.installs).checkout
    dev_install = setup_migrate.read_venv_source(checkout) == checkout
    dash_present = Path(install_exec.clone_dir(locs.installs, "dash")).is_dir()
    return [doctor.dash_build_row(dev_install, dash_present, shutil.which("cargo") is not None)]


def _doctor_running_install_rows(profile: dict | None) -> list[dict]:
    """Edge. The `running install` row: where this interpreter's `coxswain` distribution is editable from, against the
    install checkout; none without a profile or a resolvable home."""
    if profile is None:
        return []
    locs = home_paths.resolve_here()
    if isinstance(locs, home_paths.Refused):
        return []
    try:
        record = importlib.metadata.distribution(setup_install.TOOL).read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        record = None
    checkout = setup_install.layout(locs.installs).checkout
    return [doctor.running_install_row(checkout, sys.executable, doctor.editable_source(record))]


def _doctor_extras_rows() -> list[dict]:
    """Edge. The `extras` row: which of the extras `cox` needs this interpreter cannot import. Finds, never imports."""
    missing = [extra for extra, module in doctor.EXTRA_MODULES if importlib.util.find_spec(module) is None]
    return [doctor.extras_row(sys.executable, missing)]


def _doctor_steward_rows(profile: dict | None) -> list[dict]:
    """Edge. The steward row; skipped without a profile. A missing or malformed `chair` reads as off."""
    if profile is None:
        return [doctor._skip("steward")]
    chair = profile.get("chair")
    raw = chair.get("steward") if isinstance(chair, Mapping) else None
    return [doctor.steward_row(chair_facts.steward_asked_for(raw))]


def _smoke_postgres_facts(target: smoke_warm.Target) -> tuple[smoke_warm.Inspected | None, bool]:
    """Edge. The container as docker reports it, and whether a docker daemon answered."""
    if shutil.which("docker") is None:
        return None, False
    try:
        r = bounded_run.completed(smoke_warm.inspect_argv(target), capture_output=True, text=True)
    except OSError:
        return None, False
    return smoke_warm.parse_inspect(r.stdout), smoke_warm.docker_answered(r.returncode, r.stderr)


def _port_free(port: int) -> bool:
    """Edge. Whether this process could bind the warm server's loopback port, which is what `docker run` will try."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((smoke_warm.HOST, port))
        except OSError:
            return False
    return True


def _smoke_postgres_wait_ready(target: smoke_warm.Target, attempts: int = 30) -> None:
    """Edge. Polls `pg_isready` once a second; gives up quietly, the row then reports what docker shows."""
    for _ in range(attempts):
        if bounded_run.completed(smoke_warm.ready_argv(target), capture_output=True, text=True).returncode == 0:
            return
        time.sleep(1)


def _smoke_postgres_step(argv: list[str], out) -> int:
    """Edge. One docker command, its exit and docker's first error line printed to `out`."""
    done = bounded_run.completed(argv, capture_output=True, text=True)
    reason = (done.stderr or "").strip().splitlines()[:1]
    print(f"{smoke_warm.ROW}: {' '.join(argv[:2])}: exit {done.returncode} {' '.join(reason)}".rstrip(), file=out)
    return done.returncode


def _doctor_smoke_postgres_rows(*, fix: bool, out) -> list[dict]:
    """Edge. The `smoke postgres` row. Without `fix` it only reports. With `fix` it runs the docker commands the
    pure core names (start, create, unpause or replace) and stops at the first that fails. After a failed
    `docker run` it removes the container that run left in `created`; when every command succeeded it waits for
    readiness. Then it reports the container as it stands. Skipped, with no docker command, where
    `smoke_warm.SKIP_ENV` is set."""
    if os.environ.get(smoke_warm.SKIP_ENV):
        return [smoke_warm.skipped_row()]
    target = smoke_warm.target_from(os.environ)
    if isinstance(target, str):
        return [smoke_warm.refused_row(target)]
    found, docker_ok = _smoke_postgres_facts(target)
    port_free = _port_free(target.port)
    steps = smoke_warm.fix_argvs(found, target, port_free=port_free) if fix and docker_ok else []
    failed = next((argv for argv in steps if _smoke_postgres_step(argv, out) != 0), None)
    if failed is not None and failed[:2] == ["docker", "run"]:
        for argv in smoke_warm.cleanup_argvs(_smoke_postgres_facts(target)[0], target):
            _smoke_postgres_step(argv, out)
    if steps:
        if failed is None:
            _smoke_postgres_wait_ready(target)
        found, docker_ok = _smoke_postgres_facts(target)
        port_free = _port_free(target.port)
    return [smoke_warm.build_row(found, docker_ok, target, port_free=port_free)]


_LANE_PROBE_TIMEOUT_S = 60.0


def _doctor_lane_rows(profile: dict | None) -> list[dict]:
    """Edge. What the smoke gate needs here (`lane_prereqs`), probed under this process's PATH; skipped without a
    profile. The probe only reads: it never pulls an image, so a second run finds what the first did."""
    if profile is None:
        return [doctor._skip(check) for check in (lane_prereqs.DOCKER_CHECK, *lane_prereqs.CLIENT_TOOLS, lane_prereqs.GH_CHECK)]
    try:
        done = bounded_run.completed(lane_prereqs.script_argv(), timeout=_LANE_PROBE_TIMEOUT_S,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as exc:
        return lane_prereqs.reply_rows(127, str(exc), remote=False)
    return lane_prereqs.reply_rows(done.returncode, done.stdout, remote=False)


def _doctor_log_rows(profile: dict | None, *, fix: bool, out) -> list[dict]:
    """Edge. The append-only log rows, under the profile's workspace and its runs dir; none without a workspace.
    With `fix`, damaged lines move to `<log>.damaged` first, one line per log moved, and the rows show the repaired logs."""
    if not (profile and profile.get("workspace_dir")):
        return []
    runs_dir, work_dir = _dash_dirs(None, None, profile)
    if fix:
        for name, count in log_doctor.fix_logs(work_dir, runs_dir).items():
            if count:
                print(f"{name}: moved {count} damaged line(s) to {name}.damaged", file=out)
    return log_doctor.check_logs(work_dir, runs_dir)


def _setup_doctor(a: argparse.Namespace) -> int:
    """Edge. With `--host`, the host's doctor over ssh. Else this machine's, with the units' env file loaded under any
    variable the process lacks, so a hand run in an ssh shell judges what the units would see. `--json` keeps stdout
    to the JSON alone, so the note naming what was loaded is printed only without it."""
    if getattr(a, "host", None):
        return _setup_doctor_host(a, _run_ssh)
    with _unit_env_loaded(None if a.json else print):
        return _setup_doctor_here(a)


def _setup_doctor_here(a: argparse.Namespace) -> int:
    repo = Path(a.repo).expanduser() if a.repo else Path.cwd()
    facts = _gather_doctor_facts(_profile_path(a), repo)
    rows = doctor.checks(facts)
    if "freshness_lines" in facts:
        lines = facts["freshness_lines"]
        fn = (lambda *_: lines) if isinstance(lines, list) else None
        rows = [*rows, *doctor.freshness_rows(None, None, None, fn)]
    profile = _doctor_profile(facts["profile_text"])
    out = sys.stderr if a.json else sys.stdout
    fix = getattr(a, "fix", False)
    rows = [
        *rows,
        *_doctor_home_rows(profile),
        *_doctor_store_rows(profile),
        *_doctor_artifacts_rows(profile),
        *_doctor_worker_role_rows(profile),
        *_doctor_dash_build_rows(profile),
        *_doctor_running_install_rows(profile),
        *_doctor_extras_rows(),
        *_doctor_steward_rows(profile),
        *_doctor_lane_rows(profile),
        *_doctor_smoke_postgres_rows(fix=fix, out=out),
        *_doctor_log_rows(profile, fix=fix, out=out),
    ]
    rc = doctor.exit_code(rows)
    # Informational: outside rows, so outside rc. Reuses the text the facts already read.
    parquet = _parquet_traces_line(profile)
    # The tokens this machine could advertise as a lane host; a profile-less run probed nothing, so it names none.
    lane_capabilities = sorted(lane_prereqs.capabilities(rows))
    if a.json:
        print(json.dumps({"rows": rows, "ok": rc == 0, "parquet_traces": parquet, "lane_capabilities": lane_capabilities},
                         indent=2))
    else:
        print(doctor.render(rows))
        if profile is not None:
            print(f"lane capabilities: {', '.join(lane_capabilities) or 'none'}")
        if parquet:
            print(parquet)
    return rc


def _marketplace_paths(parsed: object) -> dict[str, str] | None:
    """Maps `claude plugin marketplace list --json` entries to name -> path; "" when an entry has no path."""
    if not isinstance(parsed, list):
        return None
    return {
        e["name"]: str(e.get("path") or "") for e in parsed if isinstance(e, dict) and isinstance(e.get("name"), str)
    }


def _marketplace_listing() -> dict[str, str] | None:
    """Edge: the installed marketplaces, or None when claude is absent, fails, or prints non-JSON."""
    if shutil.which("claude") is None:
        return None
    try:
        r = bounded_run.completed(["claude", "plugin", "marketplace", "list", "--json"], capture_output=True, text=True)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    try:
        return _marketplace_paths(json.loads(r.stdout))
    except ValueError:
        return None


def _marketplace_plugin_ids(parsed: object) -> list[str]:
    """Maps `claude plugin list --json` entries to their ids ending @coxswain-cartridges, in order."""
    if not isinstance(parsed, list):
        return []
    ids = (e.get("id") for e in parsed if isinstance(e, dict))
    return [i for i in ids if isinstance(i, str) and i.endswith("@coxswain-cartridges")]


def _marketplace_plugins() -> list[str]:
    """Edge: the plugin ids installed from coxswain-cartridges; [] when claude is absent, fails, or prints non-JSON."""
    if shutil.which("claude") is None:
        return []
    try:
        r = bounded_run.completed(["claude", "plugin", "list", "--json"], capture_output=True, text=True)
    except OSError:
        return []
    if r.returncode != 0:
        return []
    try:
        return _marketplace_plugin_ids(json.loads(r.stdout))
    except ValueError:
        return []


def _install_home_refusal(home: str, record: str | None, named: bool) -> str | None:
    """The refusal text when a home named by flag or environment differs from a non-empty record."""
    recorded = (record or "").strip()
    if named and recorded and recorded != home:
        return f"the home record names {recorded}, not {home}; run cox setup migrate --home {home}"
    return None


def _install_facts(a: argparse.Namespace) -> dict:
    """Gathers exactly what `install_plan` needs and nothing it decides:
    PATH lookups, the coxswain tool's receipt extras, the profile's existence, and the
    config/settings paths expanded once, here, and the platform, home, cox path and
    whether the chair service file exists, and the default garage.env and its `export` lines.
    A `--root` install passes no home and no env file. Without `--root` the checkouts,
    profile and workspace come from the resolved coxswain home; a refusal
    raises ValueError for the caller to print."""
    config_dir = str(Path("~/.config").expanduser())
    claude_settings_path = str(Path("~/.claude/settings.json").expanduser())
    if a.root:
        if not a.workspace:
            raise ValueError("--workspace is required with --root")
        root, locs, workspace_exists, registered = a.root, None, False, None
        profile_file = setup_install.profile_path(config_dir)
    else:
        locs = home_paths.resolve_here(a.home)
        if isinstance(locs, home_paths.Refused):
            raise ValueError(locs.reason)
        refusal = _install_home_refusal(
            locs.home, _read_text_or_none(Path(locs.record)), bool(a.home or os.environ.get("COXSWAIN_HOME")))
        if refusal:
            raise ValueError(refusal)
        root, profile_file = locs.installs, locs.profile
        workspace_exists = Path(locs.workspaces, "main").exists()
        listing = _marketplace_listing() if a.plugins and shutil.which("claude") is not None else None
        registered = (listing or {}).get(setup_install.MARKETPLACE)
    tool_dir = setup_receipts.default_tool_dir(os.environ, str(Path.home()))
    paths = setup_install.layout(root)
    # A `--root` install is a sandbox: it never writes into the real user's service folders.
    service_home = "" if a.root else str(Path.home())
    service_file = chair_service.service_file(sys.platform, Path(service_home)) if service_home else None
    chair_env_file = None if a.root else _chair_default_environment_file()
    chair_env_text = _read_text_or_none(Path(chair_env_file)) if chair_env_file else None
    return {
        "tool_extras": setup_receipts.read_tool_extras(tool_dir, setup_install.TOOL),
        "root": root,
        "team": a.team,
        "workspace": a.workspace or "",
        "locations": locs,
        "registered_marketplace": registered,
        "workspace_exists": workspace_exists,
        "provider_profile": a.provider_profile or paths.provider_profile,
        "skills_root": a.skills_root or paths.skills_root,
        "uv_on_path": shutil.which("uv") is not None,
        "claude_on_path": shutil.which("claude") is not None,
        "profile_exists": Path(profile_file).exists(),
        "force_profile": a.force_profile,
        "plugins": a.plugins,
        "hook": a.hook,
        "config_dir": config_dir,
        "claude_settings_path": claude_settings_path,
        "assume": a.assume,
        "window_ceiling_usd": a.window_ceiling_usd,
        "weekly_ceiling_usd": a.weekly_ceiling_usd,
        "platform": sys.platform,
        "home": service_home,
        "cox_path": shutil.which("cox"),
        "chair_service_exists": service_file is not None and service_file.exists(),
        "chair_env_file": chair_env_file,
        "chair_env_exports": tuple(chair_service.export_lines(chair_env_text or "")),
    }


def _install_run(step: dict) -> str | None:
    argv = " ".join(step["argv"])
    try:
        proc = bounded_run.completed(step["argv"], cwd=step.get("cwd"), capture_output=True, text=True)
    except OSError as exc:
        if step.get("warn_only"):
            print(f"warn: {argv}: {exc}")
            return None
        print(f"FAILED: {argv}: {exc}")
        return f"{argv}: {exc}"
    if proc.returncode == 0:
        print(f"run: {argv}")
        return None
    tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
    if step.get("warn_only"):
        print(f"warn: {argv} exited {proc.returncode}: {tail}")
        return None
    print(f"FAILED: {argv} exited {proc.returncode}")
    print(tail)
    return f"{argv} exited {proc.returncode}"


def _execute_step(step: dict) -> str | None:
    op = step["op"]
    if op == "run":
        return _install_run(step)
    if op == "write":
        path = Path(step["path"])
        if path.suffix in (".plist", ".service", ".timer"):
            _write_private(path, step["text"])
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(step["text"], encoding="utf-8")
        print(f"write {path}")
        return None
    if op == "skip":
        print(f"skip {step['what']} — {step['why']}")
        return None
    if op == "mkdir":
        Path(step["path"]).mkdir(parents=True, exist_ok=True)
        print(f"mkdir {step['path']}")
        return None
    if op == "hook":
        path = Path(step["path"])
        try:
            existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except json.JSONDecodeError as exc:
            print(f"FAILED: {path} is not valid JSON: {exc}")
            return f"{path} is not valid JSON: {exc}"
        new_settings, changed = setup_install.hook_settings(existing)
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(new_settings, indent=2) + "\n", encoding="utf-8")
            print(f"hook added to {path}")
        else:
            print(f"hook already present in {path}")
        return None
    if op == "symlink":
        return _migrate_link(step)
    if op == "print":
        print(step["text"])
        return None
    return f"unknown step op: {op!r}"


def _setup_install(a: argparse.Namespace) -> int:
    try:
        steps = setup_install.install_plan(**_install_facts(a))
    except ValueError as exc:
        print(f"refusing: {exc}")
        return 2
    if a.dry_run:
        print(setup_install.render_plan(steps))
        return 0
    _load_lazy_modules()  # a `uv tool install --force` step replaces this cox tool's own environment mid-run
    for step in steps:
        failure = _execute_step(step)
        if failure is not None:
            print(f"setup install: stopped: {failure}")
            return 1
    return 0


def _migrate_profile(locs) -> dict:
    """Edge. The profile as a mapping; empty when it is absent or unreadable."""
    text = _read_text_or_none(Path(locs.profile))
    try:
        data = yaml.safe_load(text) if text is not None else None
    except yaml.YAMLError:
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def _migrate_cartridge_links(profile: Mapping) -> dict | None:
    """Edge. Lexical target of each base and local link under cartridges_dir, at that folder and one team deeper."""
    folder = profile.get("cartridges_dir")
    if not isinstance(folder, str):
        return None
    root = Path(folder).expanduser()
    found = {*root.glob("*/base"), *root.glob("*/local"), root / "base", root / "local"}
    return {str(p): os.readlink(p) for p in sorted(found) if p.is_symlink()}


def _migrate_old_root(profile: Mapping, locs) -> str | None:
    """Pure. The folder holding the old checkouts: the parent of the first checkout-named path component in a
    profile setting that lies outside the installs folder, else the installs folder when every setting is already
    there. None when no setting names a checkout, so nothing is observed and the planner refuses."""
    raw = [item for key in setup_migrate.SETTING_KEYS for item in (profile.get(key) if isinstance(profile.get(key), list) else [profile.get(key)])]
    roots = [
        str(p.parent)
        for value in raw
        if isinstance(value, str)
        for p in [Path(value).expanduser(), *Path(value).expanduser().parents]
        if p.name in setup_migrate.CHECKOUTS and p.name != setup_migrate.UMBRELLA  # "coxswain" also names config and state folders
    ]
    outside = [r for r in roots if r != locs.installs]
    return (outside or roots or [None])[0]


def _migrate_tool_dir() -> str | None:
    """Edge. The output of `uv tool dir`; None when uv is missing, exits non-zero or prints nothing."""
    try:
        done = bounded_run.completed(["uv", "tool", "dir"], capture_output=True, text=True)
    except OSError:
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def _migrate_install_bins() -> dict | None:
    """Edge. `setup_migrate.observe_install_bins` for this machine; None when the uv tool dir is unknown."""
    tool_dir = _migrate_tool_dir()
    return None if tool_dir is None else setup_migrate.observe_install_bins(tool_dir)


def _doctor_install_sources(bins: Mapping | None, venv_source: str | None) -> dict[str, str | None] | None:
    """Pure. The two editable sources `doctor.home_rows` compares with the checkout: the venv's `coxswain` and the
    `coxswain` uv tool's receipt. None when the uv tool dir is unknown."""
    if bins is None:
        return None
    return {"venv": venv_source, "tool receipt": (bins.get(setup_install.TOOL) or {}).get("editable")}


def _migrate_observe_hosts(locs) -> list:
    """Edge. One ObservedHost per beaten active table row, then per profile lane host the table does not list."""
    profile = _migrate_profile(locs)
    declared = _profile_lane_hosts(_read_text_or_none(Path(locs.profile)) or "")
    declared = () if isinstance(declared, lane_hosts.LaneHostError) else declared
    workspace = profile.get("workspace_dir")
    runs = Path(workspace).expanduser() / "runs" if isinstance(workspace, str) else None
    rows = run_store.hosts(runs) if runs is not None and runs.is_dir() else []
    by_name = {str(r["name"]): r for r in rows}
    from_profile = {h.name: h.workspace_dir for h in declared}
    tabled = host_cmd.host_rows_to_lane_hosts(rows, (), socket.gethostname())
    seen = [
        setup_migrate_hosts.ObservedHost(
            h.name, h.ssh, h.workspace_dir,
            {Path(p).name: p for p in host_cmd.recorded_repos(by_name[h.name])},
            from_profile.get(h.name),
            "beat" if by_name[h.name].get("beat_at") is not None else "fallback",
        )
        for h in tabled
    ]
    listed = {h.name for h in tabled}
    return [*seen, *(setup_migrate_hosts.ObservedHost(h.name, h.ssh, h.workspace_dir, {}, h.workspace_dir, "fallback")
                     for h in declared if h.name not in listed)]


def _migrate_link(step: dict) -> str | None:
    path = Path(step["path"])
    kind = _migrate_kind(path)
    if kind in ("dir", "file"):
        return f"{path} is a real {kind}, so it was not replaced by a link"
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        path.unlink()
    path.symlink_to(step["target"])
    print(f"link {path} -> {step['target']}")
    return None


def _setup_doctor_row(a: argparse.Namespace) -> int:
    """Indirect through the module global, not a frozen reference: SETUP_COMMANDS
    is built once at import, but test_setup_screen.py monkeypatches
    `cli._setup_doctor` per test and expects the swap honoured."""
    return _setup_doctor(a)
