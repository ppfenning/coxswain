"""`cox route file`, `pull` and `sync`: filing work and syncing it with a tracker."""

from __future__ import annotations

import argparse
import collections
import contextlib
import datetime
import importlib
import importlib.metadata
import importlib.util
import io
import json
import socket
import subprocess
import sys
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path

import yaml

from agent_tools.cli_common import _profile_path, _read_text_or_none
from agent_tools.cli_lake import _lake_provider
from agent_tools.cli_lazy import (
    bounded_run,
    card_keys,
    chair_capacity,
    chair_facts,
    chair_read_quarantined,
    chair_read_stale,
    install_root,
    intake_fields,
    intake_pull_map,
    queue_export,
    queue_rows,
    repo_clone,
    repo_path,
    route,
    route_guard,
    route_reach,
    route_schedule,
    route_sync,
    route_sync_gh,
    run_store,
    sources,
    store_cli,
    tracker,
    work_state,
)
from agent_tools.cli_runs_fetch import _UNLABELED, _holder_label
from agent_tools.cli_stats import _resolve_profile_or_refuse


def _resolved_tracker(profile: dict, runs_dir: Path) -> str:
    """The policy file wins, then the profile's `tracker`, then `none`."""
    return tracker.tracker_name(profile, runs_dir)


def _initiative_token(runs_dir: Path, token: str) -> str:
    """Edge. The initiative slug for a token that may be its short id; a slug comes back unchanged."""
    return run_store.resolve_initiative(runs_dir, token)


def _after_ids(values: Sequence[str] | None) -> list[str] | None:
    """`--after` values as ids: None when the flag was absent, [] when every value was empty (clear the list)."""
    return None if values is None else [part.strip() for value in values for part in value.split(",") if part.strip()]


def _item_shape(rel: str) -> tuple[str, tuple[str, ...]] | None:
    """(`kind`, `path_parts`) `queue_rows.parse_item` wants for one `_write_mapping` key, or `None` for a
    path with no queue row of its own (anything outside `work/`/`intake/`). A `work/<id>/initiative.md` is an
    initiative row: launch reads it from the store and refused a one-task initiative filed without one
    (2026-10-23)."""
    parts = Path(rel).parts
    if len(parts) == 4 and parts[0] == "work":
        return "task", parts[1:]
    if len(parts) == 3 and parts[0] == "work" and parts[2] == "initiative.md":
        return "initiative", parts[1:]
    if len(parts) in (2, 3) and parts[0] == "intake":
        return "intake", parts[1:]
    return None


def _write_mapping(mapping: dict, ws: Path, by: str = _UNLABELED):
    """Write `mapping` (relative path -> text) under `ws`; `None` on success, else the refusal to print.
    A `work/<initiative>/initiative.md`, `work/<initiative>/<phase>/<task>.md` or `intake/...md` path is filed
    store-first through `route.write_filed_item`, which prints its own warning or error; anything else is
    written directly, as it always was. Each `work/<initiative>/<phase>/<task>.md` written with a `state:`
    is then mirrored to the store the same as today, one call per file."""
    targets = {rel: ws / rel for rel in mapping}
    existing = [str(path) for path in targets.values() if path.exists()]
    if existing:
        return f"routing: refusing to overwrite existing path(s): {', '.join(existing)}"
    runs_dir = ws / "runs"
    for rel, path in targets.items():
        shape = _item_shape(rel)
        if shape is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(mapping[rel], encoding="utf-8")
            queue_export.record_written(ws, {rel: mapping[rel]})
        elif route.write_filed_item(runs_dir, path, *shape, mapping[rel]) != 0:
            return f"routing: {path} was recorded in the store but its own file did not write; see above"
        print(str(path))
        _mirror_filed_state(rel, mapping[rel], runs_dir, by)
    return None


def _mirror_filed_state(rel: str, text: str, runs_dir: Path, by: str) -> None:
    parts = Path(rel).parts
    if len(parts) != 4 or parts[0] != "work":
        return
    fields = route.parse_frontmatter(text)[0]
    if not fields.get("state"):
        return
    line = store_cli.mirror_state(runs_dir, parts[1], str(fields.get("id") or Path(rel).stem), fields["state"], by)
    if line is not None:
        print(line)


def _sync_filed_items(mapping: dict, ws: Path, a: argparse.Namespace) -> None:
    """Run the per-item sync for each intake or work item `mapping` wrote, so its issue exists from birth.
    A failed sync (gh down, rate-limited) never fails the file: one line names the item and the way back."""
    rels = [rel for rel in mapping
            if (len(Path(rel).parts) == 2 and Path(rel).parts[0] == "intake")
            or (len(Path(rel).parts) == 4 and Path(rel).parts[0] == "work")]
    for rel in rels:
        item_id = route.parse_frontmatter(mapping[rel])[0].get("id") or Path(rel).stem
        sync = argparse.Namespace(item=item_id, workspace=str(ws), dry_run=False, project=None, profile=a.profile)
        if _sync_skipped(sync):
            print(f"route file: wrote {item_id}; not synced: tracker is none")
            continue
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = _route_sync(sync)
        except Exception as exc:
            rc, out = 1, io.StringIO(f"{type(exc).__name__}: {exc}")
        lines = out.getvalue().strip().splitlines()
        reason = f"; {lines[-1]}" if rc != 0 and lines else ""
        print(f"synced {item_id}" if rc == 0
              else f"route file: wrote {item_id}; issue sync failed (exit {rc}{reason}); run `route sync --item {item_id}`")


def _is_git_checkout_path(value: str, home: str) -> bool:
    """A leading tilde is expanded against `home`; an empty value is never a checkout (it would read as the cwd)."""
    return bool(value) and route.is_git_checkout(Path(repo_path.expand(value, home)))


def _not_a_checkout_message(value: str) -> str:
    return f"route file: refusing, {value} is not a git checkout on this machine"


def _reach_refused(refusal: str | None, notes: tuple[str, ...]) -> bool:
    """Print the refusal and return True when the decompose would refuse; else print one lint line per note."""
    if refusal:
        print(f"routing: {refusal}")
        return True
    for note in notes:
        print(f"routing: lint: {note}")
    return False


def _filing_repo_or_refusal(repo: str, profile: dict) -> tuple[str, str | None]:
    """The repo to file against and None, or `repo` and a `routing: ...` refusal."""
    chosen = repo_clone.filing_repo(repo, profile)
    if chosen.startswith(repo_clone.REFUSAL_PREFIX):
        return repo, f"routing: {chosen.removeprefix(repo_clone.REFUSAL_PREFIX)}"
    return chosen, None


def _intake_with_repo(intake_text: str, repo: str) -> str:
    fields, body = route.parse_frontmatter(intake_text)
    return route._frontmatter({**fields, "repo": repo}.items(), body)


def _route_file_from_intake(a: argparse.Namespace, ws: Path, profile: dict) -> int:
    intake_path = Path(a.from_intake)
    if intake_path.parent.resolve() != (ws / "intake").resolve():
        print(f"routing: --from-intake must name a file directly under {ws / 'intake'}")
        return 2
    intake_text = _read_text_or_none(intake_path)
    if intake_text is None:
        print(f"routing: cannot read intake file {a.from_intake}")
        return 2
    fields, intake_body = route.parse_frontmatter(intake_text)
    [entry] = route.intake_entries({intake_path.name: intake_text})
    typed = fields.get("repo", "")
    repo, refusal = _filing_repo_or_refusal(typed, profile)
    if refusal:
        print(refusal)
        return 2
    home = str(Path.home())
    if not _is_git_checkout_path(repo, home):
        print(_not_a_checkout_message(repo))
        return 2
    # Checked on the file as authored: `_intake_with_repo` below round-trips the frontmatter through a reader that
    # keeps the quotes in `["~/x"]`, and the launch never sees that copy.
    heal = route_reach.launch_text_heal(intake_text)
    if _reach_refused(heal.refusal, heal.notes):
        return 2
    if repo != typed:
        # A redirected repo is written the way `route file --repo` writes it, in the initiative and the archived intake.
        repo_text = route.repo_field(repo, home)
        intake_text = _intake_with_repo(intake_text, repo_text)
    else:
        repo_text = repo
    candidates = route.surface_candidates(intake_body, repo)
    surfaces = [c for c in candidates if (Path(repo) / c).is_file()]
    print(f"surfaces: {', '.join(surfaces)}" if surfaces else "surfaces: none resolved from the intake body")
    budget_usd = profile.get("one_task_budget_usd", 2.0)
    try:
        mapping = route.initiative_files(
            entry["title"], intake_body, repo_text, phase=a.phase,
            surfaces=surfaces, budget_usd=budget_usd, slug=fields.get("slug"),
        )
    except ValueError as exc:
        print(f"routing: {exc}")
        return 2
    slug = route.slugify(fields.get("slug") or entry["title"])
    initiative_rel = f"work/{slug}/initiative.md"
    new_initiative_text, new_intake_text = route.link_intake(
        mapping[initiative_rel], intake_text, f"intake/{intake_path.name}", slug
    )
    mapping[initiative_rel] = new_initiative_text
    mapping[f"intake/done/{intake_path.name}"] = new_intake_text
    refusal = _write_mapping(mapping, ws, by=_holder_label(a))
    if refusal:
        print(refusal)
        return 2
    intake_path.unlink()
    print(f"removed: {intake_path}")
    _sync_filed_items(mapping, ws, a)
    return 0


def _open_issues(ws: Path) -> list[dict]:
    """Rows with an issue number whose state is not finished: task rows end `done` or `dropped`, intake rows end `landed`."""
    closed = route.TERMINAL | {"landed"}
    keyed = ((card_keys.row_keys(row)[0], row) for row in run_store.read_queue(ws / "runs"))
    return [
        {"number": number, "title": row.get("title") or ""}
        for number, row in keyed
        if number is not None and row.get("state") not in closed
    ]


def _could_be_path(value: str) -> bool:
    """False for text that cannot name a file: a newline in it, or longer than any path."""
    return "\n" not in value and len(value) <= 4096


def _body_refusal(value: str) -> str:
    """The refusal for an unreadable --body: a value that is plainly text says so, rather than echoing it back as a
    file name (2026-10-23: a whole ticket body printed as `cannot read body file <body>`)."""
    if not _could_be_path(value) or len(value) > 200:
        return f"routing: --body takes a file path, or - for stdin, not the text itself (got {len(value)} characters of text)"
    return f"routing: cannot read body file {value}"


def _filing_stage(a: argparse.Namespace, profile: Mapping) -> str:
    """The stage a session files an intake at: the flag, else the profile's `intake_stage`, else draft."""
    if a.ready:
        return "ready"
    return "draft" if a.draft else profile.get("intake_stage") or "draft"


def _known_epic_ids(rows: Sequence[Mapping]) -> set[str]:
    """The epic ids the store's initiative rows name, which are the ids the dash feed rolls up."""
    return {epic for row in rows if (epic := queue_rows.epic_of_row(row)) is not None}


def _known_repo_names(profile: Mapping, rows: Sequence[Mapping], resolved: str = "") -> set[str]:
    """The profile's `repo_map` names, the repos the store rows already hold, and this filing's resolved repo."""
    held = {str(row["repo"]) for row in rows if row.get("repo")}
    return {*profile.get("repo_map", {}), *held, *([resolved] if resolved else [])}


def _repo_refusal_with_names(message: str, known: Collection[str]) -> str:
    return f"{message}; valid repos: {', '.join(sorted(known)) or 'none known'}"


def _route_file(a: argparse.Namespace) -> int:
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    ws = Path(profile["workspace_dir"]).expanduser()
    if a.from_intake:
        return _route_file_from_intake(a, ws, profile)
    if not a.title or not a.repo:
        print("routing: --title and --repo are required unless --from-intake is given")
        return 2
    link = getattr(a, "link", None) or ""
    if link and not a.intake:
        print("routing: --link needs --intake, since only an intake carries a link")
        return 2
    after = _after_ids(getattr(a, "after", None)) or []
    if after and not a.intake:
        print("routing: --after needs --intake, since only a queued intake waits")
        return 2
    for flag, noun in (("epic", "an epic"), ("priority", "a priority"), ("target_release", "a target release")):
        if getattr(a, flag, None) is not None and not a.intake:
            print(f"routing: --{flag.replace('_', '-')} needs --intake, since only an intake carries {noun}")
            return 2
    if a.draft and a.ready:
        print("routing: --draft and --ready are exclusive; give one")
        return 2
    if (a.draft or a.ready) and not a.intake:
        print("routing: --draft and --ready need --intake, since only an intake has a stage")
        return 2
    # Repos are siblings of the workspace: a bare name is resolved there, so the row never holds a name launch cannot read.
    home = str(Path.home())
    repo = route.resolve_repo(a.repo, ws.absolute().parent, route.is_git_checkout, home)
    rows = run_store.read_queue(ws / "runs")
    if isinstance(repo, route.RepoRefused):
        print(_repo_refusal_with_names(repo.message, _known_repo_names(profile, rows)))
        return 2
    resolved = repo
    repo, refusal = _filing_repo_or_refusal(resolved, profile)
    if refusal:
        print(refusal)
        return 2
    if not _is_git_checkout_path(repo, home):
        print(_not_a_checkout_message(a.repo if repo == resolved else repo))
        return 2
    repo = route.repo_field(repo, home)
    if a.body == "-":
        body = sys.stdin.read()
    elif a.body:
        body = _read_text_or_none(Path(a.body)) if _could_be_path(a.body) else None
        if body is None:
            print(_body_refusal(a.body))
            return 2
    else:
        body = ""
    try:
        if a.intake:
            refusals = intake_fields.validate(
                repo, a.epic, a.priority, a.target_release, _known_repo_names(profile, rows, repo), _known_epic_ids(rows)
            )
            if refusals:
                print("\n".join(f"routing: {line}" for line in refusals))
                return 2
            fields = intake_fields.normalise(repo, a.epic, a.priority, a.target_release)
            # `route.intake_file` writes the title when there is no body, and the launch reads what was written.
            heal = route_reach.launch_reach_heal(body or a.title, {})
            if _reach_refused(heal.refusal, heal.notes):
                return 2
            duplicate = card_keys.find_duplicate(a.title, _open_issues(ws))
            if duplicate is not None and not a.force:
                print(f"routing: refusing {a.title}: duplicate of issue {duplicate}; pass --force to file it anyway")
                return 2
            date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
            mapping = route.intake_file(a.title, body, repo, date, link=link, ids=profile.get("ids", "slug"), after=after,
                                       stage=_filing_stage(a, profile), epic=fields.get("epic", ""),
                                       priority=fields.get("priority", ""), target_release=fields.get("target_release", ""))
        else:
            mapping = route.initiative_files(a.title, body, repo, phase=a.phase)
    except ValueError as exc:
        print(f"routing: {exc}")
        return 2
    refusal = _write_mapping(mapping, ws, by=_holder_label(a))
    if refusal:
        print(refusal)
        return 2
    _sync_filed_items(mapping, ws, a)
    if a.intake:
        _print_fresh_id_note(a, mapping, ws)
    return 0


def _print_fresh_id_note(a: argparse.Namespace, mapping: dict, ws: Path) -> None:
    """Edge. Prints the fresh-id note when a dropped initiative holds the intake id. `route file` has no dry run."""
    if work_state.work_state_mode(_lake_provider(a)[0]) != "store":
        return
    rows = run_store.read_queue(ws / "runs")
    for text in mapping.values():
        intake_id = route.parse_frontmatter(text)[0].get("id")
        if isinstance(intake_id, str) and intake_id:
            note = route_guard.fresh_id_note(intake_id, route_guard.fresh_initiative_id(intake_id, rows))
            if note:
                print(note)


def _route_priority(a: argparse.Namespace) -> int:
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    ws = Path(profile["workspace_dir"]).expanduser()
    named = [name for name in ("up", "down", "top", "bottom") if getattr(a, name)]
    if a.set is None and not named:
        return route.list_open_queue(ws / "runs")
    if not a.initiative:
        print("routing: an initiative or queued intake is required with --set, --up, --down, --top or --bottom")
        return 2
    flag = a.set if a.set is not None else named[0]
    runs_dir = ws / "runs"
    queue = run_store.read_queue(runs_dir)
    target = route_guard.resolve_target(queue, a.initiative)
    if isinstance(target, route_guard.QueuedIntake):
        intake = next(row for row in queue if row.get("kind") == "intake" and row["task_id"] == target.task_id)
        return route.set_intake_priority(runs_dir, ws, intake, flag)
    return route.set_initiative_priority(runs_dir, ws, _initiative_token(runs_dir, a.initiative), flag)


def _schedule_inputs(profile: dict, mode: str, now: datetime.datetime):
    """Edge. The scheduler's inputs from the same readers the chair uses. Not read here, so the projection is a
    lower bound: live phases (a running phase reads waiting), remote hosts, and live lane load."""
    ws = Path(profile["workspace_dir"]).expanduser()
    runs_dir = ws / "runs"
    home = str(Path.home())
    files = chair_read_quarantined.initiative_files(ws, chair_read_quarantined.read_work_files(ws))
    items = chair_facts.read_ticket_items(ws, mode)
    ids = list(dict.fromkeys(str(r["initiative"]) for r in items))
    dispatch = {"max_in_flight": _local_max_in_flight(runs_dir, profile, socket.gethostname()), "live_runs": 0, "hosts": []}
    return chair_facts.schedule_inputs_from_rows(
        items,
        run_store.read_queue(runs_dir) if mode == "store" else [],
        chair_read_stale.read_chair_actions(runs_dir, now),
        dispatch,
        local=socket.gethostname(),
        repos=_initiative_repos(files, home),
        land_at=chair_facts.land_at_facts(_team_cartridge(profile)[0], _initiative_land_at(files), ids),
        live_phases=frozenset(),
    )


def _route_schedule(a: argparse.Namespace) -> int:
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    now = datetime.datetime.now(datetime.UTC)
    inputs = _schedule_inputs(profile, work_state.work_state_mode(_lake_provider(a)[0]), now)
    schedule, context = route_schedule.plan_from_inputs(inputs, now)
    named = a.explain or a.should_jump
    lines = (
        route_schedule.format_plain(schedule)
        if not named
        else route_schedule.explain_lines(schedule, context, a.explain)
        if a.explain
        # no model asker is wired: src/graphs/harness/invoke.py holds no single-shot helper, only invoke_graphs
        else route_schedule.should_jump_lines(schedule, context, a.should_jump, None, now.isoformat())
    )
    if lines is None:
        print(f"routing: unknown initiative: {named}")
        return 2
    print("\n".join(lines))
    return 0


def _run_argv(argv: list[str]) -> tuple[int, str, str]:
    """`(returncode, stdout, stderr)`; a missing executable is returncode 127, never an exception. Bounded: the adapters
    build `gh issue list` and `gh pr edit` argvs here, and a hung one is 124 with the failure line."""
    try:
        r = bounded_run.completed(argv, capture_output=True, text=True)
    except OSError as exc:
        return 127, "", str(exc)
    return r.returncode, r.stdout, r.stderr.strip()


def _fetch_listing(adapter, config) -> list | None:
    """The raw listing across `config.repos`; `None` after printing when a fetch fails. An adapter with no `list_argv` lists nothing; `pr_list_argv` adds its PRs; `project_list_argv` adds project items, each tagged `_listing: project`."""
    builders = [b for b in (getattr(adapter, "list_argv", None), getattr(adapter, "pr_list_argv", None)) if b]
    project_build = getattr(adapter, "project_list_argv", None)
    # An adapter whose API wraps its listing (e.g. under `data`) supplies `unwrap(payload, repo)`.
    unwrap = getattr(adapter, "unwrap", None) or (lambda payload, _repo: payload)
    listing: list = []
    for repo in config.repos:
        for build in builders:
            rc, out, err = _run_argv(build(config, repo))
            try:
                listing.extend(unwrap(json.loads(out or "[]"), repo) if rc == 0 else ())
            except json.JSONDecodeError as exc:
                rc, err = 1, f"listing is not JSON: {exc}"
            except (KeyError, TypeError) as exc:
                rc, err = 1, f"listing has an unexpected shape: {type(exc).__name__}: {exc}"
            if rc != 0:
                print(f"routing: listing {repo} failed: {err}")
                return None
        if project_build:
            rc, out, err = _run_argv(project_build(config, repo))
            try:
                items = json.loads(out or "[]") if rc == 0 else ()
                listing.extend({**item, "_listing": "project"} for item in items)
            except json.JSONDecodeError as exc:
                rc, err = 1, f"project listing is not JSON: {exc}"
            except (TypeError, AttributeError) as exc:
                rc, err = 1, f"project listing has an unexpected shape: {type(exc).__name__}: {exc}"
            if rc != 0:
                print(f"routing: project listing {repo} failed: {err}")
                return None
    return listing


def _pull_candidates(adapter, config, listing: list) -> tuple[dict, list[str]]:
    """Candidates by link, reading only entries `candidates` accepts, one entry at a time; an unparsable entry is a problem line."""
    found: dict = {}
    problems: list[str] = []
    for raw in listing:
        try:
            if isinstance(raw, dict) and raw.get("_listing") == "project":
                project = adapter.project_candidates([raw], config)
                wanted = {c.link for c in project}
                candidate = project[0] if project else None
            else:
                wanted = {ref.link for ref in adapter.candidates(config, [raw])}
                candidate = adapter.read(raw) if wanted else None
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            name = next((str(raw[k]) for k in ("url", "link", "title") if k in raw), "an entry") if isinstance(raw, dict) else "an entry"
            problems.append(f"routing: skipping unreadable listing entry {name}: {type(exc).__name__}: {exc}")
            continue
        # A link seen with a project item id keeps it, whichever listing reads the issue second.
        if (
            candidate is not None
            and candidate.link in wanted
            and not (found.get(candidate.link) and found[candidate.link].project_item_id)
        ):
            found[candidate.link] = candidate
    return found, problems


def _taken_by_project_item(found: dict, rows: list) -> frozenset[str]:
    """Links of candidates whose project item id a store row already holds."""
    return frozenset(
        link
        for link, c in found.items()
        if c.project_item_id and card_keys.find_row(rows, None, c.project_item_id) is not None
    )


def _mark_argv(adapter, config, ref, rel: str) -> list[str]:
    """An adapter that sets `MARK_NEEDS_TOKEN_ENV` also gets the profile's `token_env`."""
    if getattr(adapter, "MARK_NEEDS_TOKEN_ENV", False):
        return adapter.mark_argv(ref, rel, config.token_env)
    return adapter.mark_argv(ref, rel)


def _pull_one(adapter, config, mapping: dict, found: dict, ws: Path, dry_run: bool) -> bool:
    """Write one planned intake file, then mark its source; True after printing why when either step failed."""
    (rel, text), = mapping.items()
    fields = route.parse_frontmatter(text)[0]
    origin = found[fields["link"]]
    if dry_run:
        print(f"would write {rel}")
        return False
    if refusal := _write_mapping(mapping, ws):
        print(f"{refusal}; {origin.title!r} ({origin.link}) not filed")
        return True
    print(_filed_line(rel, fields))
    rc, _, err = _run_argv(_mark_argv(adapter, config, sources.Ref(origin.link, origin.repo), rel))
    if rc != 0:
        print(f"routing: wrote {rel}; marking {origin.link} failed (exit {rc}): {err}")
    return rc != 0


def _filed_line(rel: str, fields: Mapping) -> str:
    """Pure. The line a pull prints for a filed intake: its stage, and for a draft the command that readies it."""
    if fields.get("stage") == "draft":
        return f"filed {rel} as a draft; cox route ready {fields.get('id') or Path(rel).stem}"
    return f"filed {rel} (ready)"


def _review_one(adapter, origin, profile: str, dry_run: bool) -> bool:
    """Review one PR under the pull's profile, then mark it so the next pull skips it; True after printing why when either step failed."""
    if dry_run:
        print(f"would review {origin.link}")
        return False
    rc, _, err = _run_argv(route.review_argv(origin.link, profile))
    if rc != 0:
        print(f"routing: review of {origin.link} failed (exit {rc}): {err}")
        return True
    rc, _, err = _run_argv(adapter.mark_argv(sources.Ref(origin.link, origin.repo, "pr"), ""))
    if rc != 0:
        print(f"routing: reviewed {origin.link}; marking it failed (exit {rc}): {err}")
    return rc != 0


def _route_pull(a: argparse.Namespace) -> int:
    """`--source` pulls that one; without it every source the profile configures, in profile order (the chair's bare `route pull`)."""
    profile, rc = _resolve_profile_or_refuse(a)
    if rc is not None:
        return rc
    names = [a.source] if a.source else list(profile.get("sources", {}))
    if not names:
        print("routing: no sources in the profile")
        return 2
    return max([_pull_source(a, profile, name, named=len(names) > 1) for name in names])


def _taken_tickets(adapter, config) -> list | None:
    """Edge. The tickets a source's earlier pulls took, as candidates; [] for an adapter that cannot list them, None
    after printing when a listing fails."""
    build = getattr(adapter, "taken_list_argv", None)
    if build is None:
        return []
    unwrap = getattr(adapter, "unwrap", None) or (lambda payload, _repo: payload)
    tickets: list = []
    for repo in config.repos:
        rc, out, err = _run_argv(build(config, repo))
        try:
            tickets.extend(adapter.read(raw) for raw in (unwrap(json.loads(out or "[]"), repo) if rc == 0 else ()))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            rc, err = 1, f"{type(exc).__name__}: {exc}"
        if rc != 0:
            print(f"sync: listing the taken tickets of {repo} failed: {err}")
            return None
    return tickets


def _sync_source(adapter, config, ws: Path, fresh: Sequence, dry_run: bool) -> bool:
    """Edge. One-way sync of a source's tickets into the store's intake rows (`intake_sync`); True when it failed.
    `fresh` are the candidates the pull just read, so a queued ticket still on the intake listing is covered too."""
    from agent_tools import intake_sync  # imported here: cli loads a command module only when it runs

    taken = _taken_tickets(adapter, config)
    if taken is None:
        return True
    tickets = [*fresh, *taken]
    if not tickets:
        return False
    at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    failed = False
    for step in intake_sync.plan_sync(tickets, run_store.read_queue(ws / "runs"), at):
        if dry_run:
            if step.line:
                print(f"would {step.line}")
            continue
        refusals = [why for why in (run_store.upsert_row_detail(ws / "runs", row) for row in step.writes) if why]
        if refusals:
            failed = True
            print(f"sync: the store refused the change for {step.link}: {refusals[0]}")
        elif step.line:
            print(step.line)
    return failed


def _held_issues(ws: Path, repo_map: dict) -> dict[tuple[str, str], str]:
    """(repository, number) to `initiative/task_id` for the store rows that name both; empty when the store is unreadable."""
    keyed = ((route.row_issue_key(row, repo_map), row) for row in run_store.read_queue(ws / "runs"))
    return {key: f"{row.get('initiative')}/{row.get('task_id')}" for key, row in keyed if key is not None}


def _pull_source(a: argparse.Namespace, profile: dict, source: str, *, named: bool) -> int:
    """One source's pull; `named` puts the source in the nothing-written line when several pull in one run."""
    config = sources.source_config(profile, source)
    adapter = sources.adapter_for(source)
    if config is None or adapter is None:
        print(f"routing: no source {source!r} in the profile and adapters")
        return 2
    # Only a source that carries labels maps them; a folder's files keep no stage line, as before.
    pull_map = intake_pull_map.PullMap.from_profile(profile.get("intake_map") or {})
    if isinstance(pull_map, str):
        print(f"routing: intake_map: {pull_map}")
        return 2
    pull_map = pull_map if getattr(adapter, "MAPS_LABELS", False) else None
    listing = _fetch_listing(adapter, config)
    if listing is None:
        return 2
    ws = Path(profile["workspace_dir"]).expanduser()
    intake_root = ws / "intake"
    known = sorted(intake_root.glob("*.md")) + sorted((intake_root / "done").glob("*.md"))
    links = sources.intake_links({str(p.relative_to(ws)): p.read_text(encoding="utf-8") for p in known})
    found, unreadable = _pull_candidates(adapter, config, listing)
    rows = run_store.read_queue(ws / "runs")
    taken = frozenset(link for link in found if adapter.taken(link, links)) | _taken_by_project_item(found, rows)
    date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    repo_map = profile.get("repo_map", {})
    held = _held_issues(ws, repo_map)
    plan, reviews, refusals = route.pull_plan(
        tuple(found.values()),
        taken,
        repo_map,
        date=date,
        source=source,
        ids=profile.get("ids", "slug"),
        known_issues=held,
        pull_map=pull_map,
        known_epics=_known_epic_ids(rows),
        # A pulled issue is ready unless labelled a draft; the profile's `intake_stage` changes that default.
        default_stage=profile.get("intake_stage") or "ready",
    )
    problems = [*unreadable, *refusals]
    for line in problems:
        print(line)
    for line in route.pull_notes([c for c in found.values() if c.link not in taken], pull_map) if pull_map else ():
        print(line)
    if a.dry_run:
        for candidate, label in route.held_candidates(tuple(found.values()), held):
            print(f"routing: would skip {candidate.link}: already held by {label}")
    if not plan and not reviews:
        print(f"routing: pull wrote nothing{f' from {source}' if named else ''}: no eligible candidates")
    failed = [_pull_one(adapter, config, mapping, found, ws, a.dry_run) for mapping in plan]
    failed += [_review_one(adapter, found[link], str(_profile_path(a)), a.dry_run) for link in reviews]
    if work_state.work_state_mode(_lake_provider(a)[0]) == "store":
        # The store is the board, so an edit at the source reaches it here; with the files as the board there is no row to sync.
        failed.append(_sync_source(adapter, config, ws, [c for c in found.values() if c.kind != "pr"], a.dry_run))
    return 2 if problems or any(failed) else 0


def _harness_ready_or_refuse(harness_dir: str):
    """spec §4/§7: `launch` refuses before anything starts when the
    harness venv is not where the profile says. Returns 2 after printing
    the reason, or `None` when the venv is ready.
    """
    shell_py = Path(harness_dir) / "shell.py"
    venv_python = install_root.harness_python(harness_dir)
    if not shell_py.exists() or not venv_python.exists():
        print(f"routing: harness venv missing at {harness_dir}; install it (see the harness README)")
        return 2
    return None


def _route_sync_state_path(profile_path: Path) -> Path:
    """Where the resolved `owner/N` project persists across runs: a sidecar
    next to the profile, not a key inside it. `route.parse_profile` accepts
    a fixed field set (`route.py`'s `_KNOWN_KEYS`), and `project` is not one
    of them, so writing it into the profile itself would leave that file
    unparsable by every other route command from then on."""
    return profile_path.with_name(profile_path.name + ".route-sync-project")


def _resolve_sync_project(a: argparse.Namespace, items: list, state_path: Path):
    """`(project, None)` on success, `(None, 2)` after printing the reason.
    `project` comes back `None` (no error) only for a dry-run preview run
    before any project exists — nothing to create yet, nothing to preview
    against either."""
    if a.project:
        return a.project, None
    cached = _read_text_or_none(state_path)
    if cached and cached.strip():
        return cached.strip(), None
    owner = items[0].repo.split("/", 1)[0] if items and "/" in items[0].repo else ""
    if not owner:
        print("route sync: no --project given and no repo to derive a project owner from")
        return None, 2
    ok, found = route_sync_gh.find_project(bounded_run.completed, owner)
    if not ok:
        print(f"route sync: {found}")
        return None, 2
    if found is not None:
        return f"{owner}/{found}", None
    if a.dry_run:
        return None, None
    ok, number = route_sync_gh.create_project(bounded_run.completed, owner)
    if not ok:
        print(f"route sync: {number}")
        return None, 2
    project = f"{owner}/{number}"
    state_path.write_text(f"{project}\n")
    return project, None


def _sync_context(a: argparse.Namespace) -> tuple[str, str] | str:
    """`(workspace, tracker)` a sync runs against, or why the profile is unreadable.
    The policy file is read from `a.runs_dir` when a land passes one, else `<workspace>/runs`."""
    text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError as exc:
        return f"profile unreadable: {exc}"
    workspace = a.workspace or profile.get("workspace_dir") or "."
    runs_dir = Path(getattr(a, "runs_dir", None) or Path(workspace).expanduser() / "runs")
    return workspace, _resolved_tracker(profile, runs_dir)


def _sync_skipped(a: argparse.Namespace) -> bool:
    """True when the sync `a` describes would mirror nothing because the tracker is `none`."""
    context = _sync_context(a)
    return not isinstance(context, str) and context[1] == "none"


def _team_cartridge(profile: dict, repo: Path | None = None, overlay_keys: frozenset[str] | None = None) -> tuple[dict, str | None]:
    """Edge. The profile's team cartridge, with `repo`'s `.agent/cartridge.yaml` overlay when a repo is given (as
    `_cartridge_checks` applies it), and `None`; or `({}, why)` when it cannot be resolved. A profile naming no
    team and no cartridges_dir has no cartridge, which is `({}, None)`. `overlay_keys` keeps only those keys of the repo
    overlay, since the same file also carries `base:`, which the cartridge overlay refuses."""
    team, cartridges_dir = profile.get("team"), profile.get("cartridges_dir")
    if not team or not cartridges_dir:
        return {}, None
    try:
        from harness.resolve import overlay_path, resolve_cartridge
        overlay_file = Path(overlay_path(str(repo))) if repo is not None else None
        overlay = yaml.safe_load(overlay_file.read_text(encoding="utf-8")) if overlay_file and overlay_file.is_file() else None
        if overlay_keys is not None and isinstance(overlay, dict):
            overlay = {key: value for key, value in overlay.items() if key in overlay_keys}
        cartridge, _ = resolve_cartridge(team, cartridges_dir=Path(cartridges_dir).expanduser(),
                                         skills_root=[Path(r).expanduser() for r in profile.get("skills_roots") or []],
                                         overlay=overlay)
    except Exception as exc:
        return {}, f"cartridge {team} cannot be resolved: {type(exc).__name__}: {exc}"
    return cartridge, None


def _lane_model(profile: dict) -> str:
    """Edge. The team cartridge's `policy.dispatch.lane_model`, read when a decompose writes its rows; a profile with no
    cartridge, or one that cannot be resolved, reads `task`, the model that is off."""
    return queue_rows.cartridge_lane_model(_team_cartridge(profile)[0])


def _lane_model_for(a: argparse.Namespace) -> str:
    """Edge. `_lane_model` of the profile `a` names; no profile, or one that will not parse, reads `task`."""
    text = _read_text_or_none(_profile_path(a))
    try:
        profile = route.parse_profile(text) if text is not None else {}
    except route.ProfileError:
        return queue_rows.DEFAULT_LANE_MODEL
    return _lane_model(profile)


def _tracker_scope(profile: dict, repo: Path | None = None) -> tuple[str, dict, str | None]:
    """Edge. `(scope, epic_threshold, error)`, the cartridge's `policy.tracker_scope` over the profile's.
    An unresolvable cartridge changes nothing unless the profile itself narrows to `epics`, the one scope that
    needs the cartridge's `epic_threshold`: then the scope stays `epics` and `error` carries the resolver's reason.
    A cartridge naming a scope outside `route_sync.SCOPES` is also an `error`, with the scope fixed to `epics`.
    Otherwise `error` is None; the scope is `all` when neither side names one."""
    named = profile.get("tracker_scope")
    cartridge, error = _team_cartridge(profile, repo)
    if error is not None and named == "epics":
        return "epics", {}, error
    if error is not None:
        return named or "all", {}, None
    try:
        return route_sync.resolve_scope(named, cartridge), cartridge.get("epic_threshold") or {}, None
    except ValueError as exc:
        return "epics", {}, str(exc)


def _sync_items(a: argparse.Namespace, workspace: str) -> tuple[list[route_sync.Item], str, int | None, int]:
    """Edge. `(items, scope, rc, skipped_open)`: the items this sync mirrors under the resolved `tracker_scope`, an
    exit code to stop with when `rc` is set, after printing why, and how many open items it left out. Repos resolve
    through the profile's `repo_aliases`. An item whose repo does not resolve is dropped when it is done or dropped,
    and refused by name when open; the sync goes on without it. `--item` naming an item the scope leaves out exits 0."""
    text = _read_text_or_none(_profile_path(a))
    profile = route.parse_profile(text) if text is not None else {}
    scope, threshold, error = _tracker_scope(profile)
    if error:
        print(f"route sync: {error}; refusing rather than guessing a scope")
        return [], scope, 2, 0
    everything = route_sync_gh.items_from_store(workspace, lazy=True)
    _, refusal = _only_item(everything, a.item)
    if refusal:
        print(refusal)
        return [], scope, 2, 0
    scoped_items = route_sync.scoped(everything, scope, threshold)
    kept = route_sync.unbackfilled(scoped_items, everything, scope, profile.get("tracker_backfill") == "true")
    if a.item:
        rollup = route_sync.rollup_for(everything, scope, threshold, a.item)
        chosen = [item for item in kept if item.id == a.item] or ([rollup] if rollup else [])
    else:
        chosen = kept + route_sync.rollup_items(everything, scope, threshold)
    chosen = route_sync.with_checklists(chosen, everything, scope)
    if a.item and not chosen:
        why = ("is settled with no issue; tracker_backfill: true creates one" if any(i.id == a.item for i in scoped_items)
               else f"is out of scope under tracker_scope {scope}")
        print(f"route sync: {a.item} {why}; nothing to mirror")
        return [], scope, 0, 0
    if not a.item and len(kept) < len(scoped_items):
        print(f"route sync: left {len(scoped_items) - len(kept)} settled item(s) without an issue "
              "(tracker_backfill: true creates them)")
    resolved = route_sync_gh.resolve_repos(chosen, profile.get("repo_aliases") or {})
    items, settled, unsettled = route_sync.split_unresolved(resolved, everything)
    if a.item and not items and not unsettled:
        print(f"route sync: {a.item} is settled and its repo does not resolve; nothing to mirror")
        return [], scope, 0, 0
    for line in dict.fromkeys(item.repo_error for item in unsettled):
        print(line)
    skipped = collections.Counter(item.repo_named for item in [*settled, *unsettled])
    if skipped:
        listed = ", ".join(f"{repo} ({count})" for repo, count in skipped.items())
        print(f"route sync: skipped {sum(skipped.values())} item(s): {listed}")
    return items, scope, (2 if unsettled and not items else None), len(unsettled)


def _only_item(items: list[route_sync.Item], item_id: str | None) -> tuple[list[route_sync.Item], str | None]:
    """The items, narrowed to `item_id` when given, and a refusal line unless that leaves exactly one."""
    if not item_id:
        return items, None
    kept = [item for item in items if item.id == item_id]
    # Never fall through to the full listing: an unknown or shared id would run `item-list`.
    if len(kept) != 1:
        return kept, f"route sync: --item {item_id!r} matches {len(kept)} work-store items, expected exactly one"
    return kept, None


def _route_sync_registered(a: argparse.Namespace, name: str, mirror: object, workspace: str) -> int:
    """Drive a registered tracker through `sync`; no `gh` call is made on this path."""
    sync = getattr(mirror, "sync", None)
    if not callable(sync):
        print(f"route sync: tracker {name} has no sync(workspace, items, *, dry_run)")
        return 2
    items, _, rc, skipped = _sync_items(a, workspace)
    if rc is not None:
        return rc
    try:
        ok, lines = sync(workspace, items, dry_run=a.dry_run)
    except Exception as exc:
        print(f"route sync: tracker {name} failed: {type(exc).__name__}: {exc}")
        return 1
    for line in lines:
        print(line)
    return 1 if not ok else 2 if skipped else 0


def _route_sync_prune(a: argparse.Namespace, workspace: str, profile_path: Path) -> int:
    """Edge. `--prune-out-of-scope`: `--dry-run` prints the steps `route_sync.prune_steps` plans, a real run executes
    those same steps. Never creates a project; with none found, no card is listed."""
    text = _read_text_or_none(profile_path)
    profile = route.parse_profile(text) if text is not None else {}
    scope, _, error = _tracker_scope(profile)
    if error:
        print(f"route sync: {error}; refusing rather than guessing a scope")
        return 2
    if a.item:
        print("route sync: --prune-out-of-scope takes no --item")
        return 2
    items = [i for i in route_sync_gh.resolve_repos(route_sync_gh.items_from_store(workspace, lazy=True),
                                                    profile.get("repo_aliases") or {})
             if i.repo and not i.repo_error]
    if not route_sync_gh.auth_ok(bounded_run.completed):
        print("route sync: gh is not authenticated; run `gh auth login`")
        return 2
    # A copy with dry_run set, so resolving the project never creates one.
    project, rc = _resolve_sync_project(argparse.Namespace(**{**vars(a), "dry_run": True}), items,
                                        _route_sync_state_path(profile_path))
    if rc is not None:
        return rc
    ok, result = route_sync_gh.prune_candidates(bounded_run.completed, sorted({i.repo for i in items}), project)
    if not ok:
        print(f"route sync: {result}")
        return 2
    issues, cards = result
    steps = route_sync.prune_steps(items, issues, cards, scope)
    if not steps:
        print(f"route sync: nothing is out of scope under tracker_scope {scope}")
        return 0
    print("route sync: every open `coxswain`-labelled task issue counts as made by sync, one labelled by hand too")
    if a.dry_run:
        print("\n".join(route_sync.render(steps)))
        return 0
    log = route_sync_gh.execute(steps, subprocess.run, project or "", workspace, items)
    for kind, _, detail in log:
        print(f"{kind}: {detail}")
    return 1 if any(not step_ok for _, step_ok, _ in log) else 0


def _route_sync(a: argparse.Namespace) -> int:
    """Mirror the work store onto GitHub Projects: `--dry-run` renders the
    plan and touches nothing that outlives the run; otherwise `execute`
    runs it and stops at the first failed `gh` call. Refuses at once, exit
    2, when `gh` is not authenticated. The tracker is checked first: `none`
    exits 0, an unknown tracker exits 2, and a registered tracker is driven
    through its own `sync`, never through `gh`. An item's release is the line `Target release: <version>.`
    in its body; a `release:` front-matter key is not read. It sets the board's Release field, and an epic's
    release, the latest of its tasks', also sets the milestone of that name on the epic's issue. Before any
    issue is created, a State value with no board option stops the run, exit 1, naming the option."""
    context = _sync_context(a)
    if isinstance(context, str):
        print(f"route sync: {context}")
        return 2
    workspace, tracker_choice = context
    profile_path = _profile_path(a)
    if tracker_choice == "none":
        print("route sync: tracker is none; set tracker: github-projects in the profile to mirror the work store")
        return 0
    mirror = tracker.tracker_for(tracker_choice)
    if mirror is None:
        print(f"route sync: no tracker named {tracker_choice} (built in: none, github-projects)")
        return 2
    # `route_sync_gh` here is a lazy proxy, so the identity test runs against the real module.
    prune = getattr(a, "prune_out_of_scope", False)
    if mirror is not importlib.import_module("agent_tools.route_sync_gh"):
        if prune:
            print(f"route sync: --prune-out-of-scope needs the github-projects tracker, not {tracker_choice}")
            return 2
        return _route_sync_registered(a, tracker_choice, mirror, workspace)
    if prune:
        return _route_sync_prune(a, workspace, profile_path)
    # Items first: an unresolvable repo is refused by name before any `gh` call, `auth_ok` included.
    items, scope, rc, skipped = _sync_items(a, workspace)
    if rc is not None:
        return rc
    if not route_sync_gh.auth_ok(bounded_run.completed):
        print("route sync: gh is not authenticated; run `gh auth login`")
        return 2
    project, rc = _resolve_sync_project(a, items, _route_sync_state_path(profile_path))
    if rc is not None:
        return rc
    repo_names = sorted({item.repo for item in items if item.repo})
    if a.item and not items[0].id.startswith(route_sync.ROLLUP_PREFIX):
        ok, result = route_sync_gh.existing_item(bounded_run.completed, items[0].repo, items[0].issue, project)
    else:  # a roll-up has no issue number of its own to view: it is found by title among the listed issues
        ok, result = route_sync_gh.existing(bounded_run.completed, repo_names, project)
    if not ok:
        print(f"route sync: {result}")
        return 2
    issues, project_items, item_node_ids = result
    items = route_sync.link_rollups(items, issues)
    steps = route_sync.plan(items, issues, project_items, "github-projects", scope)
    if a.dry_run:
        print("\n".join(route_sync.render(steps)))
        return 2 if skipped else 0
    log = route_sync_gh.execute(steps, subprocess.run, project, workspace, items, item_node_ids)
    for kind, _, detail in log:
        print(f"{kind}: {detail}")
    hard_failure = any(not step_ok for kind, step_ok, _ in log if kind != "refuse")
    return 1 if hard_failure else 2 if skipped else 0


def _chair_max_in_flight(runs_dir: Path, profile: dict) -> int:
    """Edge. The lane cap `chair_capacity.chair_max_in_flight` reads, the same one the `cox dash` feed reads."""
    return chair_capacity.chair_max_in_flight(runs_dir, profile)


def _local_max_in_flight(runs_dir: Path, profile: dict, hostname: str) -> int:
    """The local lane cap: the hosts-table row's own `capacity` when one exists for `hostname`, else
    `_chair_max_in_flight`'s existing cartridge, then pacing-file, then default-3 chain."""
    row = next((r for r in run_store.hosts(runs_dir) if r.get("name") == hostname), None)
    return _chair_max_in_flight(runs_dir, profile) if row is None or row.get("capacity") is None else int(row["capacity"])


def _initiative_repos(initiative_files: Mapping[Path, str], home: str) -> dict[str, str]:
    """Pure. Initiative id to the `repo:` in its `initiative.md` text expanded under `home`, as `chair_facts.read_initiative_repos` reads it from disk."""
    fields = {p.parent.name: route.parse_frontmatter(text)[0] for p, text in initiative_files.items()}
    return {name: repo_path.expand(f["repo"], home) for name, f in fields.items() if isinstance(f.get("repo"), str) and f["repo"]}


def _initiative_land_at(initiative_files: Mapping[Path, str]) -> dict[str, str | None]:
    """Pure. Initiative id to the `land_at:` in its `initiative.md` text, None when the key is absent or not a string."""
    fields = {p.parent.name: route.parse_frontmatter(text)[0] for p, text in initiative_files.items()}
    return {name: f["land_at"] if isinstance(f.get("land_at"), str) else None for name, f in fields.items()}
