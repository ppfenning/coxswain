"""`cox setup store` and `setup token`: choosing and switching the store, and the token."""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from agent_tools.cli_common import _profile_path, _read_text_or_none
from agent_tools.cli_lazy import (
    bounded_run,
    chair_service,
    claude_token,
    route,
    setup_store,
    setup_store_io,
    setup_token,
    store_probe,
    store_switch,
    store_url,
    unit_env,
    upgrade_edge,
)


def _doctor_profile(text: str | None) -> dict | None:
    """The doctor's one parse rule: a missing or unparseable profile is None, never a refusal."""
    try:
        return route.parse_profile(text) if text is not None else None
    except route.ProfileError:
        return None


# (attribute, prompt label, flag). The first seven are connection details; the last three name environment variables.
_STORE_DETAILS = (
    ("host", "Postgres host", "--host"),
    ("port", "Postgres port", "--port"),
    ("database", "Postgres database", "--database"),
    ("user", "Postgres user", "--user"),
    ("bucket", "S3 bucket", "--bucket"),
    ("endpoint", "S3 endpoint (http:// or https://)", "--endpoint"),
    ("region", "S3 region", "--region"),
)
# (SharedConnection field, attribute, prompt label, flag)
_STORE_CREDENTIALS = (
    ("pg_password", "password_env", "Environment variable holding the Postgres password", "--password-env"),
    ("s3_access_key_id", "access_key_env", "Environment variable holding the S3 access key id", "--access-key-env"),
    ("s3_secret_access_key", "secret_key_env", "Environment variable holding the S3 secret key", "--secret-key-env"),
)


def _git_exclude_file(directory: Path) -> Path | None:
    """Edge. The enclosing repository's info/exclude; None outside a repository or without git."""
    try:
        done = bounded_run.completed(["git", "rev-parse", "--git-path", "info/exclude"], cwd=directory,
                              capture_output=True, text=True, check=False)
    except OSError:
        return None
    found = done.stdout.strip()
    return directory / found if done.returncode == 0 and found else None


@contextlib.contextmanager
def _env_overlay(overlay: Mapping[str, str]):
    """Edge. `store_probe` reads the S3 keys from os.environ under the names in the profile; the user's own variables
    may differ, so the resolved values sit under those names for the probes only and the environment is restored."""
    saved = {name: os.environ.get(name) for name in overlay}
    os.environ.update(overlay)
    try:
        yield
    finally:
        for name, old in saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


def _units_env_file(home: Path) -> Path:
    """Edge. The env file this machine's units read: the one the upgrade, chair or lander unit names, in that order,
    else the default `cox setup unit-env` writes. `unit_env.units_environment_file` decides; this only reads."""
    from agent_tools import lander_cli

    units = [upgrade_edge.schedule_paths(sys.platform, home)[0], chair_service.unit_path(home),
             chair_service.chair_plist_path(home), lander_cli.unit_path(home)]
    texts = (_read_text_or_none(path) for path in units)
    return Path(unit_env.units_environment_file(texts, unit_env.env_file_path(str(home)))).expanduser()


@contextlib.contextmanager
def _unit_env_loaded(note: Callable[[str], None] | None = None):
    """Edge. A hand run (an ssh or interactive shell) sees the environment the units see: every variable the units'
    env file defines that the process lacks is set for the duration, then removed. The process environment wins.
    `note` gets the names loaded and the file, never a value."""
    path = _units_env_file(Path.home())
    loaded = unit_env.missing_values(os.environ, unit_env.parse_env(_read_text_or_none(path) or ""))
    if loaded and note is not None:
        note(f"env: loaded {', '.join(loaded)} from {path}, the file the units read")
    with _env_overlay(loaded):
        yield


def _probe_store(profile: Mapping, overlay: Mapping[str, str]) -> list:
    """Edge, the default probes: one read-only Postgres connection and one bucket head. Never starts either service."""
    with _env_overlay(overlay):
        return [store_probe.probe_postgres(profile["storage_url"]),
                store_probe.probe_object_store(profile["object_store"], profile["traces_url"])]


def _asked_or_given(value, label: str, ask: Callable[[str], str], interactive: bool):
    if value is not None:
        return value
    return ask(f"{label}: ").strip() if interactive else ""


def _store_choice(a: argparse.Namespace, ask: Callable[[str], str], interactive: bool) -> str | None:
    """Edge. The flag beats the prompt; no terminal means the default, with no prompt. None when the value is invalid."""
    default = setup_store.default_store_choice()
    if a.store is not None:
        return setup_store.parse_store_choice(a.store)
    if not interactive:
        return default
    answer = ask(f"Store for run records: {default} or shared [{default}]: ")
    return setup_store.parse_store_choice(answer) if answer.strip() else default


_FILES_STORE_RETIRED = (
    "the files store is retired: the one store is Postgres. Use `cox setup store --store shared` "
    "(and `cox setup store switch shared` to carry an old local store's rows over)."
)


def _store_files(a: argparse.Namespace, settings_path: Path) -> int:
    if a.dry_run:
        print("dry run: would record store.mode files; nothing was written")
        return 0
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        setup_store_io.apply_files_choice(settings_path)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("store: files; store.mode recorded and no shared profile written")
    return 0


def _shared_connection(a: argparse.Namespace, env: Mapping[str, str], ask: Callable[[str], str],
                       interactive: bool) -> tuple[setup_store.SharedConnection | None, int]:
    """Edge. The validated SharedConnection and 0, or None and the exit code after printing why."""
    fields = (*_STORE_DETAILS, *((attr, label, flag) for _f, attr, label, flag in _STORE_CREDENTIALS))
    values = {attr: _asked_or_given(getattr(a, attr), label, ask, interactive) for attr, label, _flag in fields}
    missing = [flag for attr, _label, flag in fields if values[attr] in (None, "")]
    if missing:
        print(f"error: the shared store needs {', '.join(missing)}; pass the flags or run in a terminal",
              file=sys.stderr)
        return None, 2
    creds = setup_store_io.credentials_from_env(
        env, {field: values[attr] for field, attr, _label, _flag in _STORE_CREDENTIALS})
    if creds["unset"]:
        for var in creds["unset"]:
            print(f"error: environment variable {var} is not set or is empty", file=sys.stderr)
        return None, 2
    try:
        port = int(values["port"])
    except ValueError:
        print("error: the Postgres port must be a whole number", file=sys.stderr)
        return None, 2
    conn = setup_store.SharedConnection(
        pg_host=values["host"], pg_port=port, pg_database=values["database"], pg_user=values["user"],
        pg_password=creds["values"]["pg_password"], s3_endpoint=values["endpoint"], s3_bucket=values["bucket"],
        s3_region=values["region"], s3_access_key_id=creds["values"]["s3_access_key_id"],
        s3_secret_access_key=creds["values"]["s3_secret_access_key"],
    )
    problems = setup_store.validate_shared(conn)
    if problems:
        print("\n".join(f"error: {sentence}" for sentence in problems), file=sys.stderr)
        return None, 2
    return conn, 0


def _store_shared(a: argparse.Namespace, settings_path: Path, env: Mapping[str, str], ask: Callable[[str], str],
                  interactive: bool, probes: Callable | None, exclude_file: Path | None) -> int:
    conn, code = _shared_connection(a, env, ask, interactive)
    if conn is None:
        return code
    if a.dry_run:
        print(f"dry run: {setup_store.redacted_summary(conn)}; nothing was written")
        return 0
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        print(setup_store_io.apply_shared_choice(
            conn, settings_path, exclude_file or _git_exclude_file(settings_path.parent)))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not write the shared store files ({type(exc).__name__})", file=sys.stderr)
        return 1
    overlay = {setup_store.ACCESS_KEY_ENV: conn.s3_access_key_id, setup_store.SECRET_KEY_ENV: conn.s3_secret_access_key}
    results = (probes or _probe_store)(setup_store.build_shared_profile(conn), overlay)
    print(store_probe.summarize(results))
    if all(r.ok for r in results):
        return 0
    print("warning: the shared store did not answer from this machine; the profile was kept", file=sys.stderr)
    return 0 if a.allow_unreachable else 1


def _setup_store(a: argparse.Namespace, *, env: Mapping[str, str] | None = None, ask: Callable[[str], str] = input,
                 interactive: bool | None = None, probes: Callable | None = None,
                 exclude_file: Path | None = None) -> int:
    """Edge. Choose the store once (flag, else prompt in a terminal, else files) and hand off to the pure cores.
    Starts no service: the user's Postgres and object store are only probed."""
    tty = (sys.stdin.isatty() and sys.stdout.isatty()) if interactive is None else interactive
    choice = _store_choice(a, ask, tty)
    if choice is None:
        print("error: the store must be files or shared", file=sys.stderr)
        return 2
    settings_path = _profile_path(a)
    if choice == "files":
        print(f"error: {_FILES_STORE_RETIRED}", file=sys.stderr)
        return 2
    return _store_shared(a, settings_path, os.environ if env is None else env, ask, tty, probes, exclude_file)


def _setup_token(a: argparse.Namespace) -> int:
    """Edge. Store the long-lived Claude token: getpass for the prompt, a prompt only when both ends are terminals."""
    return setup_token.store(
        a.token, a.from_file,
        path=claude_token.token_path(str(Path.home())),
        interactive=sys.stdin.isatty() and sys.stdout.isatty(),
        ask_secret=setup_token.prompt_secret,
        read_text=setup_token.read_file_text,
    )


def _setup_unit_env(a: argparse.Namespace) -> int:
    """Edge. Write the one env file the chair, lander and upgrade units read (`unit_env.ENV_FILE`), keeping the lines it
    has and setting the token from the stored token file, else from this shell's environment. Prints the path and the
    names written, never a value; refuses, writing nothing, what `unit_env.file_text` refuses."""
    home = str(Path.home())
    path = Path(a.environment_file).expanduser() if a.environment_file else Path(unit_env.env_file_path(home))
    token = claude_token.read_token(claude_token.token_path(home)) or (os.environ.get(claude_token.TOKEN_ENV) or "").strip()
    try:
        text = unit_env.file_text(_read_text_or_none(path), token or None)
    except ValueError as exc:
        print(f"refusing: {path}: {exc}")
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(text, encoding="utf-8")
    print(path)
    print(f"names: {', '.join(unit_env.parse_env(text)) or 'none'}")
    if not token:
        print(f"note: no token found; run `cox setup token`, then this again, so the units get {claude_token.TOKEN_ENV}")
    return 0


def _setup_token_status(a: argparse.Namespace) -> int:
    """Edge. Report the stored token's state; `--probe` spends one small claude request to prove it works."""
    return setup_token.status(
        a.probe,
        path=claude_token.token_path(str(Path.home())),
        read_token=claude_token.read_token,
        run=setup_token.run_command,
    )


def _store_mode_and_runs(settings_path: Path) -> tuple[str, Path | None]:
    """Edge. The store.mode setting (files when unset) and the runs directory under the profile's workspace_dir."""
    profile = _doctor_profile(_read_text_or_none(settings_path)) or {}
    workspace = profile.get("workspace_dir")
    return (profile.get("store") or {}).get("mode", "files"), (Path(workspace).expanduser() / "runs" if workspace else None)


def _switch_env(a: argparse.Namespace, env: Mapping[str, str]) -> dict[str, str]:
    """`env` with the S3 keys also under the names the profile's object_store block reads, when the flags name them."""
    named = {setup_store.ACCESS_KEY_ENV: getattr(a, "access_key_env", None),
             setup_store.SECRET_KEY_ENV: getattr(a, "secret_key_env", None)}
    return {**env, **{key: env[var] for key, var in named.items() if var and env.get(var)}}


def _setup_store_switch(a: argparse.Namespace, *, env: Mapping[str, str] | None = None, copy_store: Callable | None = None,
                        copy_traces: Callable | None = None, probes: Callable | None = None,
                        settings_path: Path | None = None) -> int:
    """Edge. Copy the run store and traces to the other backend, then record store.mode. Deletes nothing from the source."""
    env = os.environ if env is None else env
    settings_path = settings_path or _profile_path(a)
    if a.target != "shared":
        print(f"error: {_FILES_STORE_RETIRED}", file=sys.stderr)
        return 2
    mode, runs_dir = _store_mode_and_runs(settings_path)
    if mode == a.target:
        print(f"store is already {mode}; nothing to do")
        return 0
    if runs_dir is None:
        print("error: the profile names no workspace_dir, so there is no runs directory to switch", file=sys.stderr)
        return 2
    profile_file = setup_store_io.default_profile_path(settings_path)
    to_shared = True
    conn = None
    source_file = profile_file
    if to_shared:
        conn, code = _shared_connection(a, env, input, sys.stdin.isatty() and sys.stdout.isatty())
        if conn is None:
            return code
        shared = setup_store.build_shared_profile(conn)
    else:
        provider = (_doctor_profile(_read_text_or_none(settings_path)) or {}).get("provider_profile")
        source_file = Path(provider).expanduser() if provider else profile_file
        shared = dict(store_url.read_provider_profile(source_file))
    local = (store_url.default_url(runs_dir), store_url.default_traces_root(runs_dir))
    remote = (shared.get("storage_url", ""), shared.get("traces_url", ""))
    source, target = (local, remote) if to_shared else (remote, local)
    try:
        steps = store_switch.plan_switch("to_shared" if to_shared else "to_files", source[0], target[0], source[1], target[1])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not steps:
        print(f"error: {source_file} does not name a postgresql store, so nothing was switched", file=sys.stderr)
        return 2
    print(store_switch.render_plan(steps))
    if a.dry_run:
        print("dry run: nothing was written or copied")
        return 0
    try:
        if to_shared:
            setup_store_io.write_shared_profile(profile_file, shared, _git_exclude_file(profile_file.parent))
        results = store_switch.run_plan(
            steps, copy_store or store_switch.default_copy_store,
            copy_traces or store_switch.traces_copier(shared.get("object_store"), _switch_env(a, env)))
        failed = next((r for r in results if not r["ok"]), None)
        if failed is not None:
            print(f"error: {failed['kind']} failed: {failed['error']}", file=sys.stderr)
            return 1
        setup_store_io.record_store_mode(a.target, settings_path)
        if not to_shared:
            profile_file.unlink(missing_ok=True)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not write the store files ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"store: {a.target}; store.mode recorded")
    if conn is not None:
        overlay = {setup_store.ACCESS_KEY_ENV: conn.s3_access_key_id, setup_store.SECRET_KEY_ENV: conn.s3_secret_access_key}
        print(store_probe.summarize((probes or _probe_store)(shared, overlay)))
    return 0


def _setup_fields(a: argparse.Namespace) -> tuple[str, str, str]:
    """(root, team, workspace) to prefill the TUI with, from the profile
    when it resolves, else empty strings — never a hard refusal here."""
    text = _read_text_or_none(_profile_path(a))
    if text is None:
        return "", "", ""
    try:
        profile = route.parse_profile(text)
    except route.ProfileError:
        return "", "", ""
    harness_dir = profile.get("harness_dir", "")
    root = str(Path(harness_dir).parent) if harness_dir else ""
    return root, profile.get("team", ""), profile.get("workspace_dir", "")
