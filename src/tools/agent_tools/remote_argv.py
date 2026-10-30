"""Pure argv builders for ssh, rsync and git fetch; a location is a caller-formed string."""

import json
import re
import shlex

# Every lane-host call must give up within this many seconds.
LANE_HOST_TIMEOUT_S = 30
_ALIVE_INTERVAL_S = 10
_ALIVE_COUNT_MAX = LANE_HOST_TIMEOUT_S // _ALIVE_INTERVAL_S
_CONNECT_OPTION = f"ConnectTimeout={LANE_HOST_TIMEOUT_S}"
# No BatchMode: Tailscale SSH check mode must still print its login URL.
_SSH_BOUND_OPTIONS = [
    "-o", _CONNECT_OPTION,
    "-o", f"ServerAliveInterval={_ALIVE_INTERVAL_S}",
    "-o", f"ServerAliveCountMax={_ALIVE_COUNT_MAX}",
]
_RSYNC_BOUND_OPTIONS = ["-a", f"--timeout={LANE_HOST_TIMEOUT_S}", "-e", f"ssh -o {_CONNECT_OPTION}"]


def login_shell_argv(remote_argv: list[str]) -> list[str]:
    """Run the quoted command in `$SHELL -l -i -c` so startup files that export a token are read."""
    script = 'exec "${SHELL:-/bin/sh}" -l -i -c "$1"'
    return ["/bin/sh", "-c", script, "login-shell", shlex.join(remote_argv)]


def _wants_login_shell(remote_argv: list[str]) -> bool:
    return remote_argv[:1] == ["claude"] or remote_argv[:3] == ["cox", "route", "launch"]


def _as_binary(remote_argv: list[str]) -> list[str]:
    """claude run as the binary, past any shell function or alias of that name. A lane runs the binary as a
    subprocess of cox, which inherits only exported variables; a startup file's `claude()` wrapper that injected
    a token made the login check pass while every lane on the host failed to authenticate (2026-10-23)."""
    return ["command", *remote_argv] if remote_argv[:1] == ["claude"] else remote_argv


def ssh_argv(ssh: str, remote_argv: list[str]) -> list[str]:
    """`ssh` is a destination such as user@host; the remote shell sees one quoted command."""
    inner = login_shell_argv(_as_binary(remote_argv)) if _wants_login_shell(remote_argv) else remote_argv
    return ["ssh", *_SSH_BOUND_OPTIONS, ssh, shlex.join(inner)]


def doctor_argv() -> list[str]:
    return ["cox", "setup", "doctor"]


def auth_status_argv() -> list[str]:
    return ["claude", "auth", "status"]


def json_object_in(output: str) -> dict | None:
    """The first JSON object in `output`, or None. A login shell can print startup noise around it."""
    decoder = json.JSONDecoder()
    for start, char in enumerate(output):
        if char != "{" or (start and output[start - 1] != "\n"):
            continue
        try:
            value, _ = decoder.raw_decode(output[start:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def env_check_argv(var: str) -> list[str]:
    return ["printenv", var]


def launch_argv(initiative: str, run_id: str, label: str) -> list[str]:
    return [
        "cox", "route", "launch", "epic",
        "--initiative", initiative,
        "--run-id", run_id,
        "--label", label,
        "--no-claim",
    ]


def rsync_push_argv(src_dir: str, dest_location: str, *, mirror: bool = False) -> list[str]:
    """Trailing slashes on both ends copy directory contents; deletes at the destination only when `mirror`."""
    return ["rsync", *_RSYNC_BOUND_OPTIONS, *(["--delete"] if mirror else []), src_dir.rstrip("/") + "/", dest_location.rstrip("/") + "/"]


def rsync_pull_argv(src_location: str, dest: str) -> list[str]:
    """One directory or one file, exactly as the caller formed it."""
    return ["rsync", *_RSYNC_BOUND_OPTIONS, src_location, dest]


def git_fetch_argv(repo_location: str, run: str) -> list[str]:
    """The initiative is `run` without a trailing `-<digits>` phase/attempt suffix."""
    initiative = re.sub(r"-\d+$", "", run)
    run_refspec = f"refs/heads/agents/{run}/*:refs/heads/agents/{run}/*"
    phase_refspec = f"+refs/heads/epic/{initiative}/*:refs/heads/epic/{initiative}/*"
    return ["git", "fetch", repo_location, run_refspec, phase_refspec]


# Wrong belief (2026-10-30): "this fast-forwards the lane's repo to main". It only follows origin's default branch
# from a checkout already on it, and a chair repo detached at a release tag pinned the host there instead. The launcher
# uses `base_sync_argv` below whenever it can resolve the chair's origin/<base>; this one is the fallback.
# Each guard exits before any later line runs: a missing path must not fall through to
# whatever repository the ssh session starts in. Untracked files are not "dirty" here.
_SYNC_SCRIPT = "\n".join((
    'cd "$1" 2>/dev/null || { echo "sync: cannot enter $1" >&2; exit 1; }',
    'top=$(git rev-parse --show-toplevel 2>/dev/null)',
    '[ "$top" = "$(pwd -P)" ] || { echo "sync: $1 is not the root of a git checkout" >&2; exit 1; }',
    "default=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')",
    'default=${default:-main}',
    'current=$(git symbolic-ref --short -q HEAD)',
    '[ "$current" = "$default" ] || '
    '{ echo "sync: checkout is on ${current:-a detached HEAD}, not $default" >&2; exit 1; }',
    '[ -z "$(git status --porcelain --untracked-files=no)" ] || '
    '{ echo "sync: tracked changes in the working tree" >&2; exit 1; }',
    'git fetch origin && git merge --ff-only "origin/$default"',
))


def sync_argv(repo_path: str) -> list[str]:
    """Fast-forward `repo_path` to origin's default branch, or print one reason to stderr and exit 1."""
    return ["bash", "-c", _SYNC_SCRIPT, "sync", repo_path]


# The repo sync behind the base guard. `sync_argv` stops at a detached HEAD and follows origin's default branch; a
# checkout left detached at a release tag by an earlier harness sync therefore never reached main. This script goes
# to the commit the chair resolved ($3, its origin/<base> after a fetch) and exits 1 unless HEAD is that commit.
# Every exit after the checkout is found prints `head <sha>` on stdout; the reason is a `sync: ...` line on stderr.
_BASE_SYNC_SCRIPT = "\n".join((
    'cd "$1" 2>/dev/null || { echo "sync: cannot enter $1" >&2; exit 1; }',
    'top=$(git rev-parse --show-toplevel 2>/dev/null)',
    '[ "$top" = "$(pwd -P)" ] || { echo "sync: $1 is not the root of a git checkout" >&2; exit 1; }',
    'fail() { echo "sync: $1" >&2; echo "head $(git rev-parse HEAD 2>/dev/null)"; exit 1; }',
    '[ -z "$(git status --porcelain --untracked-files=no)" ] || fail "tracked changes in the working tree"',
    'git fetch --tags origin >/dev/null 2>&1 || fail "git fetch origin failed"',
    'git checkout -q "$2" >/dev/null 2>&1 || fail "cannot check out $2"',
    'git merge --ff-only -q "$3" >/dev/null 2>&1 || fail "$2 cannot fast-forward to $3"',
    '[ "$(git rev-parse HEAD)" = "$3" ] || fail "$2 is not at $3 after the fast-forward"',
    'echo "head $3"',
))


def base_sync_argv(repo_path: str, base: str, sha: str) -> list[str]:
    """Bring `repo_path` to `sha`, the chair's `origin/<base>` commit, on branch `base`; exit 1 with one reason if it cannot."""
    return ["bash", "-c", _BASE_SYNC_SCRIPT, "sync", repo_path, base, sha]


def epic_refspec(initiative: str) -> str:
    return f"refs/heads/epic/{initiative}/*:refs/heads/epic/{initiative}/*"


def agent_refspec(run: str, task: str) -> str:
    return f"refs/heads/agents/{run}/{task}:refs/heads/agents/{run}/{task}"


def branch_push_argv(local_repo: str, ssh: str, repo_path: str, refspec: str) -> list[str]:
    """`-C local_repo` because the executor's cwd is the work store, not the repo holding the branches."""
    return ["git", "-C", local_repo, "push", "--force", f"ssh://{ssh}/{repo_path.lstrip('/')}", refspec]
