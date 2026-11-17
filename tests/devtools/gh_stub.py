"""A stand-in for the `gh` executable, put first on PATH by the release end-to-end test.

It answers only the `gh` calls the release's merge-back and GitHub release steps make, against the real bare origins
the test built: a pull request, a release and an install-proof run are rows in a JSON state file, and `gh pr merge`
merges with real git into origin.
Every call is logged to that file. Nothing here reaches GitHub. Usage: `python gh_stub.py <gh args>`, with
GH_STUB_STATE naming the state file.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TIMEOUT = 60


def _git(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=_TIMEOUT)


def _flag(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def _origin(cwd: str) -> str:
    return _git("remote", "get-url", "origin", cwd=cwd).stdout.strip()


def _default(cwd: str) -> str:
    out = _git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=cwd).stdout.strip()
    return out.rsplit("/", 1)[-1] or "main"


def _current(cwd: str) -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd).stdout.strip()


def _open(state: dict, origin: str, head: str, base: str | None = None) -> list[dict]:
    return [pr for pr in state["prs"] if pr["origin"] == origin and pr["head"] == head and pr["state"] == "open"
            and (base is None or pr["base"] == base)]


def _ahead(origin: str, base: str, head: str) -> int:
    """Commits on origin's `head` that its `base` lacks, read from the bare origin itself."""
    out = _git("--git-dir", origin, "rev-list", "--count", f"refs/heads/{base}..refs/heads/{head}").stdout.strip()
    return int(out) if out.isdigit() else -1


def _merge(origin: str, pr: dict, method: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as scratch:
        steps = [("clone", "--quiet", "--branch", pr["base"], origin, scratch)]
        if method == "merge":
            steps.append(("-C", scratch, "merge", "--no-ff", "-m", f"Merge pull request #{pr['number']} from "
                          f"{pr['head']}", f"origin/{pr['head']}"))
        else:
            steps += [("-C", scratch, "merge", "--squash", f"origin/{pr['head']}"),
                      ("-C", scratch, "commit", "-m", f"{pr['title']} (#{pr['number']})")]
        steps += [("-C", scratch, "push", "origin", pr["base"]), ("-C", scratch, "push", "origin", "--delete", pr["head"])]
        for argv in steps:
            done = _git(*argv)
            if done.returncode != 0:
                return 1, done.stdout + done.stderr
    return 0, ""


def answer(args: list[str], cwd: str, state: dict) -> tuple[int, str]:
    """`(exit code, output)` for one `gh` call; `state` is changed in place."""
    verb = args[:2]
    if verb == ["api"] or args[:1] == ["api"]:
        return 1, "gh: Branch not protected (HTTP 404)"
    if verb == ["workflow", "run"]:
        state["proofs"].append(len(state["proofs"]) + 1)
        return 0, ""
    if verb == ["run", "list"]:
        proofs = state["proofs"] if "install-proof.yml" in args else []
        return 0, json.dumps([{"databaseId": i} for i in reversed(proofs)])
    if verb == ["run", "view"]:
        return 0, json.dumps({"databaseId": int(args[2]), "status": "completed", "conclusion": "success",
                              "url": f"https://ci.invalid/run/{args[2]}"})
    if verb == ["pr", "checks"]:
        return (0, json.dumps([{"name": "build", "state": "SUCCESS", "bucket": "pass"}]) if "--json" in args else
                "build\tpass\t1s\thttps://ci.invalid/1\n")
    origin = _origin(cwd)
    if verb == ["pr", "list"]:
        return 0, json.dumps([{"number": pr["number"]}
                              for pr in _open(state, origin, _flag(args, "--head"), _flag(args, "--base"))])
    if verb == ["pr", "create"]:
        head, base = _flag(args, "--head") or _current(cwd), _flag(args, "--base") or _default(cwd)
        if _open(state, origin, head):
            return 1, f"a pull request for branch \"{head}\" into branch \"{base}\" already exists"
        if _ahead(origin, base, head) <= 0:
            return 1, f"pull request create failed: No commits between {base} and {head}"
        number = len(state["prs"]) + 1
        state["prs"].append({"number": number, "origin": origin, "head": head, "base": base,
                             "title": _flag(args, "--title"), "state": "open"})
        return 0, f"https://forge.invalid/pull/{number}\n"
    if verb == ["pr", "merge"]:
        positional = args[2] if len(args) > 2 and not args[2].startswith("--") else None
        found = _open(state, origin, positional or _current(cwd))
        if not found:
            return 1, "no open pull request found"
        rc, out = _merge(origin, found[0], "merge" if "--merge" in args else "squash")
        if rc == 0:
            found[0]["state"] = "merged"
        return rc, out
    if verb == ["release", "view"]:
        body = state["releases"].get(f"{origin}#{args[2]}")
        if body is None:
            return 1, "release not found"
        return 0, body if "--json" in args else f"title:\t{args[2]}\n"
    if verb in (["release", "create"], ["release", "edit"]):
        state["releases"][f"{origin}#{args[2]}"] = Path(_flag(args, "--notes-file")).read_text()
        return 0, ""
    return 1, f"gh stub: no answer for {' '.join(args)}"


def main() -> int:
    path = Path(os.environ["GH_STUB_STATE"])
    state = json.loads(path.read_text()) if path.exists() else {"prs": [], "releases": {}, "proofs": [], "calls": []}
    args = sys.argv[1:]
    state["calls"].append(args)
    rc, out = answer(args, os.getcwd(), state)
    path.write_text(json.dumps(state, indent=1))
    (sys.stdout if rc == 0 else sys.stderr).write(out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
