#!/usr/bin/env python3
"""Drive one epic through its routine steps without a model: merge approved levels, relaunch, run the gate.

The agent starts the job and reads the result. Everything routine is decided here, from files and git, and
announced on the courier bus. A model is only needed for what this script reports as needs_attention.

usage: epic_steward.py INITIATIVE --repo PATH --ticket-branch BRANCH [--once] [--apply] [--interval S]
                               [--to chair] [--gate-db-suffix _gate_test]

  --apply      do the actions. Without it the script prints what it would do and what it would publish, and
               sends nothing: a dry run.
  --once       one tick, then exit (default loops every --interval seconds until the epic is complete).
  --to LABEL   courier label that receives the events (default: chair).

Messages are `cox courier send coxswain://initiative/<id> --to <label> --note <json>`; the JSON carries
`event` and details. Events: needs_attention, level_merged, merge_failed, relaunched, stuck, gate_passed,
gate_failed, epic_complete. Each is sent once per distinct cause (deduped in runs/.steward-<id>.json).
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = Path(os.environ.get("COXSWAIN_WORKSPACE", Path.cwd())).resolve()   # holds work/ and runs/
LIVE_STATES = ("approved", "done")


# ---- pure core -----------------------------------------------------------------------------------

def fingerprint(tasks):
    """A short stable digest of every task's state; unchanged means nothing moved."""
    return hashlib.sha1(json.dumps(sorted((t["id"], t["state"]) for t in tasks)).encode()).hexdigest()[:10]


def mergeable_levels(tasks, merged):
    """Levels, in first-seen order, whose live tasks are all approved or done and that are not merged yet."""
    order, by = [], {}
    for t in tasks:
        if t["state"] == "dropped":
            continue
        if t["level"] not in by:
            order.append(t["level"])
        by.setdefault(t["level"], []).append(t["state"])
    return [lv for lv in order if all(s in LIVE_STATES for s in by[lv]) and not merged.get(lv, False)]


def decide(state):
    """state -> ordered actions. No I/O. Order matters: merges precede a relaunch, so a level that depends on a
    merged level is cut from the merged branch (a level that needs a merged level must be cut after the merge)."""
    tasks, sent = state["tasks"], state["sent"]
    actions = []

    def once(key, event, **detail):
        if key not in sent:
            actions.append({"do": "notify", "key": key, "event": event, **detail})

    for t in tasks:
        if t["state"] == "quarantined":
            once(f"quarantine:{t['id']}", "needs_attention", task=t["id"], level=t["level"],
                 why="quarantined; read `cox runs triage` and add retry notes")

    todo = mergeable_levels(tasks, state["merged"])
    if todo:
        actions.append({"do": "merge", "levels": todo})
        return actions   # relaunch only after the merge succeeded, on the next tick

    ready = [t for t in tasks if t["state"] == "ready"]
    quarantined = [t for t in tasks if t["state"] == "quarantined"]
    fp = fingerprint(tasks)
    if state["run_alive"]:
        return actions
    if ready and not quarantined:
        if state.get("last_relaunch_fp") == fp:
            once(f"stuck:{fp}", "stuck", why="the run exited and no task changed state since the last relaunch")
        else:
            actions.append({"do": "relaunch", "fp": fp, "ready": len(ready)})
        return actions
    if ready and quarantined:
        return actions   # ready tasks wait behind a quarantine; the operator decides
    all_done = tasks and all(t["state"] in (*LIVE_STATES, "dropped") for t in tasks)
    if all_done and all(state["merged"].get(lv, False) for lv in {t["level"] for t in tasks}):
        if state["head"] != state.get("gated_head"):
            actions.append({"do": "gate", "head": state["head"]})
        else:
            once(f"complete:{state['head']}", "epic_complete", head=state["head"])
    return actions


# ---- edges ---------------------------------------------------------------------------------------

def sh(cmd, cwd=None, check=False):
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=check, stdin=subprocess.DEVNULL)


def read_tasks(init):
    out = []
    for p in sorted((WORKSPACE / "work" / init).glob("*/*.md")):
        if p.name == "initiative.md":
            continue
        head = p.read_text().split("\n---\n", 1)[0]
        g = lambda k: re.search(rf"^{k}: (\S+)", head, re.M).group(1)
        out.append({"id": g("id"), "level": g("phase"), "state": g("state")})
    return out


def run_alive(init):
    pids = sorted((WORKSPACE / "runs").glob(f"{init}-*.pid"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    if not pids:
        return False
    try:
        os.kill(int(pids[-1].read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def collect(init, repo, ticket, memory):
    sh(["git", "-C", repo, "fetch", "-q", "origin"])
    tasks = read_tasks(init)
    merged = {lv: sh(["git", "-C", repo, "merge-base", "--is-ancestor", f"epic/{init}/{lv}", f"origin/{ticket}"]).returncode == 0
              for lv in {t["level"] for t in tasks}}
    head = sh(["git", "-C", repo, "rev-parse", "--short", f"origin/{ticket}"]).stdout.strip()
    return {"tasks": tasks, "merged": merged, "run_alive": run_alive(init), "head": head,
            "sent": set(memory.get("sent", [])), "last_relaunch_fp": memory.get("last_relaunch_fp"),
            "gated_head": memory.get("gated_head")}


def publish(init, to, action, apply):
    note = json.dumps({k: v for k, v in action.items() if k not in ("do", "key")}, sort_keys=True)
    line = f"courier send coxswain://initiative/{init} --to {to} --note {note}"
    if not apply:
        print("  [dry] would " + line)
        return True
    return sh(["cox", "courier", "send", f"coxswain://initiative/{init}", "--to", to, "--note", note]).returncode == 0


def gate_db_url(repo, init, suffix):
    base = os.environ.get("TEST_DATABASE_URL", "")
    if not base:
        for line in (Path(repo) / ".env").read_text().splitlines():
            if line.startswith("TEST_DATABASE_URL="):
                base = line.split("=", 1)[1]
    name = re.sub(r"[^a-z0-9]+", "_", init.lower())[:40] + suffix
    return re.sub(r"/[^/?]+(\?|$)", f"/{name}\\1", base, count=1), name


def perform(action, a, memory):
    kind = action["do"]
    if kind == "notify":
        if publish(a.initiative, a.to, action, a.apply):
            memory.setdefault("sent", []).append(action["key"])
    elif kind == "merge":
        cmd = [str(HERE / "merge-level.sh"), a.repo, a.initiative, a.ticket_branch, *action["levels"]]
        if not a.apply:
            print("  [dry] would run " + " ".join(cmd[:1] + [Path(a.repo).name, a.initiative, a.ticket_branch, *action["levels"], "--apply"]))
            return
        r = sh(cmd + ["--apply"])
        ok = r.returncode == 0
        publish(a.initiative, a.to, {"event": "level_merged" if ok else "merge_failed", "levels": action["levels"],
                                     "tail": (r.stdout + r.stderr).strip().splitlines()[-6:]}, True)
    elif kind == "relaunch":
        cmd = ["cox", "route", "launch", "epic", "--initiative", a.initiative, "--repo", a.repo,
               "--base", f"origin/{a.ticket_branch}", "--local"]
        if not a.apply:
            print("  [dry] would run (in work/) " + " ".join(cmd))
            return
        r = sh(cmd, cwd=WORKSPACE / "work")
        memory["last_relaunch_fp"] = action["fp"]
        run = next((l for l in r.stdout.splitlines() if l.startswith("run ")), "no run line")
        publish(a.initiative, a.to, {"event": "relaunched", "ready": action["ready"], "run": run}, True)
    elif kind == "gate":
        url, name = gate_db_url(a.repo, a.initiative, a.gate_db_suffix)
        if not a.apply:
            print(f"  [dry] would run the full integration suite serially on origin/{a.ticket_branch} ({action['head']}), "
                  f"private database {name}: go test -tags integration -p 1 -count=1 ./...")
            return
        wt = f"/tmp/steward-gate-{a.initiative[:30]}"
        sh(["git", "-C", a.repo, "worktree", "add", "-q", "--detach", wt, f"origin/{a.ticket_branch}"])
        try:
            r = subprocess.run(["go", "test", "-tags", "integration", "-p", "1", "-count=1", "./..."],
                               cwd=wt, text=True, capture_output=True, stdin=subprocess.DEVNULL,
                               env={**os.environ, "TEST_DATABASE_URL": url})
        finally:
            sh(["git", "-C", a.repo, "worktree", "remove", "--force", wt])
        fails = [l for l in r.stdout.splitlines() if l.startswith(("--- FAIL", "FAIL"))]
        memory["gated_head"] = action["head"]
        publish(a.initiative, a.to, {"event": "gate_passed" if r.returncode == 0 else "gate_failed",
                                     "head": action["head"], "failures": fails[:10]}, True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("initiative")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--ticket-branch", required=True)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--to", default="chair")
    ap.add_argument("--gate-db-suffix", default="_gate_test")
    a = ap.parse_args(argv)
    a.repo = str(Path(a.repo).expanduser())
    mem_path = WORKSPACE / "runs" / f".steward-{a.initiative}.json"
    memory = json.loads(mem_path.read_text()) if mem_path.exists() else {}
    while True:
        state = collect(a.initiative, a.repo, a.ticket_branch, memory)
        actions = decide(state)
        counts = {}
        for t in state["tasks"]:
            counts[t["state"]] = counts.get(t["state"], 0) + 1
        print(f"{time.strftime('%H:%M:%S')} tasks {counts}, levels merged "
              f"{sum(state['merged'].values())}/{len(state['merged'])}, run alive: {state['run_alive']}, "
              f"ticket head {state['head']}; {len(actions)} action(s)" + ("" if a.apply else "  [dry run]"))
        for act in actions:
            print("  action:", json.dumps(act, sort_keys=True))
            perform(act, a, memory)
        if a.apply:
            mem_path.write_text(json.dumps(memory, sort_keys=True))
        done = state["tasks"] and not actions and not state["run_alive"] and all(
            t["state"] in (*LIVE_STATES, "dropped") for t in state["tasks"]) and all(state["merged"].values())
        if a.once or done:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
