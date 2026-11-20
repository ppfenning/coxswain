"""What the smoke gate needs on a lane host, judged from one probe's output. Pure except `probe_argv`'s script, which
the edges run (`cli_doctor`, `remote_doctor`, `chair_login_check`).

On 2026-11-19 a macOS lane host was quarantined five times on setup gaps, not code: Docker Desktop not running, its
keychain credential helper failing in an unattended session, `pg_dump` off the PATH, the pinned smoke images absent,
and a stale checkout (the last is `checkout_env`'s). Each cost a build and a review before the smoke gate failed in
fixture setup. The checks here name those gaps before the chair places a lane, so the host is skipped instead.

The probe is one POSIX sh script, so it needs no cox of any version on the host and reads the PATH the lane would. It
never pulls and never writes: an absent image is judged pullable by asking the credential helper `docker pull` would
ask, so running it twice changes nothing. Every command it runs has its own time limit (`LIMITS_S`) and runs in a
process group of its own, which the watchdog kills whole; the probe as a whole keeps to `BUDGET_S`, inside the
lane-host ssh bound. A hung Docker Desktop is a failing row naming the wait, never a probe the ssh call cut off.

Two kinds of check. Prerequisites (docker, the images, pg_dump, gh, a readable probe) are lasting: one failure blocks
placement and moves homed work. Room (temp headroom, available memory) dips and recovers: it blocks placement only
after `ROOM_BLOCK_AFTER` failing checks in a row and never moves homed work. A host short on room keeps its
capabilities: they say what it can run, not whether it has room now.

`capabilities` turns the rows into the tokens a host may advertise; `docker` only when every check it needs passes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from agent_tools.chair_login_watch import is_unreachable_reply, versions_of
from agent_tools.chair_types import Action, ActionKind
from agent_tools.remote_argv import LANE_HOST_TIMEOUT_S, login_shell_argv

# The smoke suite's pinned images (smoke/plan.py, which the package cannot import); a test keeps the two equal.
POSTGRES_IMAGE = "postgres:16@sha256:dd2bf3ce2dc83a81674cf7f217fcfd8a5009a4b325dda5b843bfee82a06cff6f"
FORGEJO_IMAGE = "data.forgejo.org/forgejo/forgejo:11@sha256:946243edbab116d5bb78b73ea68af6f3d69229ba1b1ed958dd82c3481167f3e0"
GARAGE_IMAGE = "dxflrs/garage:v2.1.0@sha256:4c9b34c113e61358466e83fd6e7d66e6d18657ede14b776eb78a93ee8da7cf6a"
SMOKE_IMAGES = (POSTGRES_IMAGE, FORGEJO_IMAGE, GARAGE_IMAGE)
# The store fixtures back a database up with pg_dump on the host (smoke/conftest.py); psql runs inside the container.
CLIENT_TOOLS = ("pg_dump",)

DOCKER_CAPABILITY = "docker"
CAUSE = "lane_prereqs"
OK_KEY, REASON_KEY, CAPABILITIES_KEY = "lane_ok", "lane_reason", "lane_capabilities"
ROOM_OK_KEY, ROOM_REASON_KEY, ROOM_FAILURES_KEY = "room_ok", "room_reason", "room_failures"
ROOM_BLOCK_AFTER = 2

DOCKER_CHECK = "docker daemon"
GH_CHECK = "gh"
REACHABLE_CHECK = "host reachable"
MEMORY_CHECK = "free memory"
TMP_CHECK_PREFIX = "tmp headroom"
PROBE_CHECK = "lane probe"
# A lane's builds, test runs and containers need room. On 2026-11-20 a RAM-backed /tmp filled with several builds at
# once and the machine rebooted; a host below either floor gets no lane until a later check finds room again.
TMP_MIN_FREE_MB = 2048
MEMORY_MIN_FREE_MB = 2048
# Seconds each probed command may take: docker info; one image inspect; one credential helper get; anything else (gh,
# df, the memory reading). A command that runs over is killed and reads as exit 124. One slow docker or helper call
# stops the calls of its kind after it, so the worst case stays well inside the lane-host ssh bound.
LIMITS_S = (6, 3, 3, 3)
TIMED_OUT = 124
# The whole probe's budget, in seconds, under the lane-host ssh bound: a call that would end past it is skipped, so calls
# slow enough to add up but each inside its limit cannot run the probe into the ssh cut-off.
BUDGET_S = LANE_HOST_TIMEOUT_S - 5
SKIPPED = 125
_MARK = "cox-lane-prereq"
# One doctor row: `check`, `ok` and `detail`, the shape `doctor.checks` gives.
Row = dict[str, object]
_OUTPUT_CHARS = 160

# Positional args: the four limits, the budget, the client tools, `--`, then the images. Every line it prints starts
# with the marker, so a login shell's startup noise around them is ignored.
_SCRIPT = r"""m=cox-lane-prereq
nl='
'
first() { printf '%s' "${1%%"$nl"*}"; }
sleep_path=$(command -v sleep 2>/dev/null)
setsid_path=$(command -v setsid 2>/dev/null)
if [ -z "$sleep_path" ]; then
  printf '%s\tnosleep\n%s\tend\n' "$m" "$m"
  exit 0
fi
# limited SECONDS CMD...: CMD under a watchdog; exit 124 when the watchdog had to kill it. CMD runs in a process group
# of its own, and the watchdog kills the whole group, so a grandchild (a wrapper's child, a docker plugin, a
# credential sub-helper) cannot hold the caller's pipe open. The group comes from setsid where it exists (Linux): with
# job control off the job is no group leader, so setsid runs it in place and its pid is the group. Without setsid
# (macOS) it comes from job control in this subshell, which bash, the macOS sh, grants with no terminal. The wrong
# belief was "set -m gives every sh a process group": under dash with no terminal (CI's /bin/sh) the job stayed in the
# shell's group and a wrapper's hung child kept the pipe open, so setsid is chosen first. A background job reads
# /dev/null unless stdin is redirected, so stdin goes over on fd 3; the shell's own job notices go to /dev/null.
limited() (
  secs=$1
  shift
  exec 3<&0 4>&2 2>/dev/null
  if [ -n "$setsid_path" ]; then
    "$setsid_path" "$@" <&3 2>&4 3<&- 4>&- &
  else
    set -m
    "$@" <&3 2>&4 3<&- 4>&- &
  fi
  pid=$!
  ( "$sleep_path" "$secs"; kill -9 -"$pid" || kill -9 "$pid" ) >/dev/null 3<&- 4>&- &
  dog=$!
  wait "$pid"
  rc=$?
  if kill -0 "$dog"; then
    kill -9 -"$dog" || kill -9 "$dog"
    exit "$rc"
  fi
  exit 124
)
docker_s=$1 image_s=$2 helper_s=$3 other_s=$4 budget_s=$5
shift 5
# fits SECONDS WHAT: whether a call bounded by SECONDS still ends inside the budget; it charges the call, or notes WHAT
# as skipped. The clock decides; without `date` the limits charged so far stand in for the time spent.
start=$(date +%s 2>/dev/null)
spent=0
skipped=
fits() {
  now=$(date +%s 2>/dev/null)
  if [ -n "$start" ] && [ -n "$now" ]; then used=$((now - start)); else used=$spent; fi
  if [ $((used + $1)) -gt "$budget_s" ]; then
    skipped="$skipped $2"
    return 1
  fi
  spent=$((spent + $1))
}
while [ "$#" -gt 0 ] && [ "$1" != -- ]; do
  printf '%s\ttool\t%s\t%s\n' "$m" "$1" "$(command -v "$1" 2>/dev/null)"
  shift
done
[ "$#" -gt 0 ] && shift
docker_path=$(command -v docker 2>/dev/null)
docker_code=127
docker_out=
if [ -n "$docker_path" ]; then
  if fits "$docker_s" docker; then
    docker_out=$(limited "$docker_s" "$docker_path" info --format '{{.ServerVersion}}' 2>&1)
    docker_code=$?
  else
    docker_code=125
  fi
fi
printf '%s\tdocker\t%s\t%s\t%s\n' "$m" "$docker_code" "$docker_path" "$(first "$docker_out")"
config="${DOCKER_CONFIG:-$HOME/.docker}/config.json"
store=
if [ -r "$config" ]; then
  text=
  while IFS= read -r line || [ -n "$line" ]; do text="$text $line"; done < "$config"
  case "$text" in
    *'"credsStore"'*) store=${text#*\"credsStore\"}; store=${store#*\"}; store=${store%%\"*} ;;
  esac
fi
helper=
[ -n "$store" ] && helper=$(command -v "docker-credential-$store" 2>/dev/null)
printf '%s\tcreds\t%s\t%s\n' "$m" "$store" "$helper"
inspect_hung=
helper_hung=
for image in "$@"; do
  if [ "$docker_code" -ne 0 ] || [ -n "$inspect_hung" ]; then
    printf '%s\timage\tunchecked\t-\t%s\t\n' "$m" "$image"
    continue
  fi
  if ! fits "$image_s" "inspect:$image"; then
    printf '%s\timage\tskipped\t-\t%s\t\n' "$m" "$image"
    continue
  fi
  limited "$image_s" "$docker_path" image inspect --format '{{.Id}}' "$image" >/dev/null 2>&1
  inspect_code=$?
  if [ "$inspect_code" -eq 0 ]; then
    printf '%s\timage\tpresent\t-\t%s\t\n' "$m" "$image"
    continue
  fi
  if [ "$inspect_code" -eq 124 ]; then
    inspect_hung=1
    printf '%s\timage\thung\t-\t%s\t\n' "$m" "$image"
    continue
  fi
  if [ -z "$store" ] || [ -z "$helper" ]; then
    printf '%s\timage\tabsent\t-\t%s\t\n' "$m" "$image"
    continue
  fi
  if [ -n "$helper_hung" ]; then
    printf '%s\timage\tabsent\t124\t%s\t\n' "$m" "$image"
    continue
  fi
  if ! fits "$helper_s" "helper:$image"; then
    printf '%s\timage\tskipped\t-\t%s\t\n' "$m" "$image"
    continue
  fi
  server=${image%%/*}
  case "$image" in
    */*) case "$server" in *.*|*:*|localhost) ;; *) server=https://index.docker.io/v1/ ;; esac ;;
    *) server=https://index.docker.io/v1/ ;;
  esac
  helper_out=$(printf '%s' "$server" | limited "$helper_s" "$helper" get 2>&1)
  helper_code=$?
  [ "$helper_code" -eq 124 ] && helper_hung=1
  printf '%s\timage\tabsent\t%s\t%s\t%s\n' "$m" "$helper_code" "$image" "$(first "$helper_out")"
done
gh_path=$(command -v gh 2>/dev/null)
gh_code=127
gh_out=
if [ -n "$gh_path" ]; then
  if fits "$other_s" gh; then
    gh_out=$(limited "$other_s" "$gh_path" --version 2>&1)
    gh_code=$?
  else
    gh_code=125
  fi
fi
printf '%s\tgh\t%s\t%s\t%s\n' "$m" "$gh_code" "$gh_path" "$(first "$gh_out")"
dir=${TMPDIR:-/tmp}
df_out=
fits "$other_s" df && df_out=$(limited "$other_s" df -Pk "$dir" 2>/dev/null)
printf '%s\tspace\t%s\t%s\n' "$m" "$dir" "${df_out##*"$nl"}"
emit_mem() {
  while IFS= read -r line; do
    case "$line" in *"$1"*|*"$2"*) printf '%s\tmemline\t%s\n' "$m" "$line" ;; esac
  done
}
if command -v memory_pressure >/dev/null 2>&1; then
  fits "$other_s" memory && limited "$other_s" memory_pressure 2>/dev/null | emit_mem "The system has" "memory free percentage"
elif command -v free >/dev/null 2>&1; then
  fits "$other_s" memory && limited "$other_s" free -k 2>/dev/null | emit_mem "available" "Mem:"
elif [ -r /proc/meminfo ]; then
  emit_mem "MemAvailable:" "MemAvailable:" < /proc/meminfo
fi
[ -n "$skipped" ] && printf '%s\tskipped\t%s\t%s\n' "$m" "$budget_s" "${skipped# }"
printf '%s\tend\n' "$m"
"""


@dataclass(frozen=True)
class Ran:
    """One probed command: where it resolved ("" when not on the PATH), its exit code and its first output line."""

    path: str
    code: int
    line: str


@dataclass(frozen=True)
class Image:
    """`state` is present, absent, hung (its inspect ran over its limit) or unchecked (docker did not answer, or an
    earlier inspect hung). An absent image carries the credential helper's `get` exit code and first line,
    `helper_code` None when no helper was asked."""

    image: str
    state: str
    helper_code: int | None
    line: str


@dataclass(frozen=True)
class Probe:
    tools: Mapping[str, str]
    docker: Ran
    creds_store: str
    helper_path: str
    images: tuple[Image, ...]
    gh: Ran
    space: tuple[tuple[str, str], ...] = ()  # (the temp directory, the last line `df -Pk` printed for it)
    memory: tuple[str, ...] = ()  # memory_pressure's, free's or /proc/meminfo's lines that carry the reading
    skipped: tuple[str, str] | None = None  # (the budget in seconds, the calls left out once it ran out)


def script_argv(
    images: Sequence[str] = SMOKE_IMAGES, tools: Sequence[str] = CLIENT_TOOLS, budget_s: int = BUDGET_S,
) -> list[str]:
    """The probe for this machine, under this process's PATH."""
    limits = [str(s) for s in (*LIMITS_S, budget_s)]
    return ["/bin/sh", "-c", _SCRIPT, "lane-prereqs", *limits, *tools, "--", *images]


def probe_argv(images: Sequence[str] = SMOKE_IMAGES, tools: Sequence[str] = CLIENT_TOOLS) -> list[str]:
    """The probe in the login shell a lane runs under on a remote host; wrap it in `ssh_argv` to reach one."""
    return login_shell_argv(script_argv(images, tools))


def _code(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


# How many tab-separated fields follow each kind; the last one is free text and may hold tabs itself.
_ARITY = {
    "tool": 2, "docker": 3, "creds": 2, "image": 4, "gh": 3, "space": 2, "memline": 1, "skipped": 2, "nosleep": 0,
    "end": 0,
}


def _fields(output: str) -> list[tuple[str, list[str]]]:
    """(kind, fields) for each marked line of a known kind with its full field count; other lines are dropped."""
    marked = [line.rstrip("\r").split("\t", 1)[1] for line in output.splitlines() if line.startswith(f"{_MARK}\t")]
    split = [(kind, rest.split("\t", _ARITY[kind] - 1) if _ARITY[kind] else [])
             for line in marked for kind, _, rest in [line.partition("\t")] if kind in _ARITY]
    return [(kind, fields) for kind, fields in split if len(fields) == _ARITY[kind]]


def parse_probe(output: str) -> Probe | None:
    """The probe's report, or None when its lines are missing, cut short (no end line) or malformed."""
    lines = _fields(output)
    by_kind: dict[str, list[list[str]]] = {}
    for kind, fields in lines:
        by_kind.setdefault(kind, []).append(fields)
    singles = {kind: by_kind.get(kind, []) for kind in ("docker", "creds", "gh", "end")}
    if any(len(found) != 1 for found in singles.values()):
        return None
    (docker_code, docker_path, docker_line), (store, helper), (gh_code, gh_path, gh_line) = (
        singles["docker"][0], singles["creds"][0], singles["gh"][0]
    )
    docker_rc, gh_rc = _code(docker_code), _code(gh_code)
    if docker_rc is None or gh_rc is None:
        return None
    tools = {fields[0]: fields[1] for fields in by_kind.get("tool", [])}
    images = tuple(Image(image, state, _code(code), line) for state, code, image, line in by_kind.get("image", []))
    space = tuple((directory, line) for directory, line in by_kind.get("space", []))
    memory = tuple(line for (line,) in by_kind.get("memline", []))
    skipped = next(((budget, calls) for budget, calls in by_kind.get("skipped", [])), None)
    return Probe(tools, Ran(docker_path, docker_rc, docker_line), store, helper, images, Ran(gh_path, gh_rc, gh_line),
                 space, memory, skipped)


def _row(check: str, ok: bool, detail: str) -> Row:
    return {"check": check, "ok": ok, "detail": detail}


def _cut(text: str) -> str:
    return " ".join(text.split())[:_OUTPUT_CHARS]


def docker_row(docker: Ran) -> Row:
    if not docker.path:
        return _row(DOCKER_CHECK, False, "no docker on the lane PATH: the smoke gate starts its fixtures with docker")
    if docker.code == SKIPPED:
        return _row(DOCKER_CHECK, False, "not checked: the probe ran past its budget")
    if docker.code == TIMED_OUT:
        return _row(DOCKER_CHECK, False, f"docker did not answer within {LIMITS_S[0]}s; restart it")
    if docker.code != 0:
        return _row(DOCKER_CHECK, False, f"the docker daemon did not answer (exit {docker.code}): {_cut(docker.line)}; start it")
    return _row(DOCKER_CHECK, True, f"server {docker.line.strip()} via {docker.path}")


def image_check(image: str) -> str:
    """`smoke image <name>`: the last path part of the repository, without tag or digest."""
    return f"smoke image {image.split('@')[0].rsplit('/', 1)[-1].split(':')[0]}"


def _helper_failed(image: Image) -> bool:
    """A helper that answers `credentials not found` lets an anonymous pull go on; any other failure stops it."""
    return image.helper_code not in (None, 0) and "credentials not found" not in image.line.lower()


def image_row(image: Image, creds_store: str, helper_path: str) -> Row:
    check = image_check(image.image)
    if image.state == "present":
        return _row(check, True, "present in the local store")
    if image.state == "hung":
        return _row(check, False, f"docker did not answer within {LIMITS_S[1]}s (docker image inspect); restart it")
    if image.state == "skipped":
        return _row(check, False, "not checked: the probe ran past its budget")
    if image.state == "unchecked":
        return _row(check, False, "not checked: docker did not answer")
    helper = f"docker-credential-{creds_store}"
    if creds_store and not helper_path:
        return _row(check, False, f"absent, and docker pull would fail: {helper} (credsStore) is not on the lane PATH")
    if creds_store and image.helper_code == TIMED_OUT:
        return _row(check, False, f"absent, and {helper} did not answer within {LIMITS_S[2]}s, as docker pull would "
                                  "need it to; pull it once in a session that can reach the credential store")
    if creds_store and _helper_failed(image):
        return _row(check, False, f"absent, and docker pull would fail in {helper} (exit {image.helper_code}): "
                                  f"{_cut(image.line)}; pull it once in a session that can reach the credential store")
    return _row(check, True, "absent; docker pull can fetch it without a prompt")


def tool_row(name: str, path: str) -> Row:
    if path:
        return _row(name, True, path)
    return _row(name, False, f"not on the lane PATH: the smoke gate's store fixtures run {name}; put the PostgreSQL "
                             "client tools' folder on the PATH the lane's login shell sets")


def gh_row(gh: Ran) -> Row:
    """A real gh answers `gh --version` with `gh version ...`; a version-manager shim with no version set does not."""
    if not gh.path:
        return _row(GH_CHECK, False, "not on the lane PATH")
    if gh.code == SKIPPED:
        return _row(GH_CHECK, False, "not checked: the probe ran past its budget")
    if gh.code == TIMED_OUT:
        return _row(GH_CHECK, False, f"{gh.path} did not answer `gh --version` within {LIMITS_S[3]}s")
    if gh.code != 0 or not gh.line.startswith("gh version"):
        return _row(GH_CHECK, False, f"{gh.path} is not a usable gh (exit {gh.code}): {_cut(gh.line)}")
    return _row(GH_CHECK, True, f"{gh.line.strip()} at {gh.path}")


def df_available_kb(line: str) -> int | None:
    """The Available column (KiB) of one `df -Pk` data line; None for a header, an empty or an unreadable line."""
    fields = line.split()
    return int(fields[3]) if len(fields) >= 6 and fields[3].isdigit() else None


def tmp_row(directory: str, df_line: str) -> Row:
    """Headroom on the directory the lane's temp files go to: `$TMPDIR` when set, else /tmp."""
    check = f"{TMP_CHECK_PREFIX} {directory}"
    free_kb = df_available_kb(df_line)
    if free_kb is None:
        return _row(check, False, f"not measured: df -Pk {directory} gave {_cut(df_line) or 'nothing'}")
    free_mb = free_kb // 1024
    if free_mb < TMP_MIN_FREE_MB:
        return _row(check, False, f"{free_mb} MiB free, under the {TMP_MIN_FREE_MB} MiB a lane needs; clear old "
                                  f"build and test directories under {directory}")
    return _row(check, True, f"at least {TMP_MIN_FREE_MB} MiB free")


def _first_int(text: str) -> int | None:
    word = next((w for w in text.replace("%", " ").split() if w.isdigit()), None)
    return int(word) if word is not None else None


def available_memory_kb(lines: Sequence[str]) -> int | None:
    """KiB available, from whichever reading the probe found.

    macOS: `memory_pressure`'s total (`The system has <bytes> (...)`) times its `System-wide memory free percentage`.
    That percentage is the kernel's own (`kern.memorystatus_level`), the figure its pressure levels act on; it counts
    free, purgeable, file-backed and compressible memory once each. Summing vm_stat's counters instead double counts:
    file-backed pages are also inside its active and inactive counts.
    Linux: the `available` column of `free -k` (found by its header), else `MemAvailable` from /proc/meminfo."""
    total = next((_first_int(line.split("The system has", 1)[1]) for line in lines if "The system has" in line), None)
    percent = next((_first_int(line.split(":", 1)[1]) for line in lines if "memory free percentage" in line), None)
    if total is not None and percent is not None:
        return total * percent // 100 // 1024
    header = next((line.split() for line in lines if "available" in line.split()), None)
    row = next((line.split() for line in lines if line.startswith("Mem:")), None)
    if header is not None and row is not None and len(row) > header.index("available") + 1:
        value = row[header.index("available") + 1]
        return int(value) if value.isdigit() else None
    meminfo = next((line.split()[1:2] for line in lines if line.startswith("MemAvailable:")), None)
    return int(meminfo[0]) if meminfo and meminfo[0].isdigit() else None


def memory_row(lines: Sequence[str]) -> Row:
    free_kb = available_memory_kb(lines)
    if free_kb is None:
        return _row(MEMORY_CHECK, False, "not measured: memory_pressure, free and /proc/meminfo gave no reading")
    free_mb = free_kb // 1024
    if free_mb < MEMORY_MIN_FREE_MB:
        return _row(MEMORY_CHECK, False, f"{free_mb} MiB available, under the {MEMORY_MIN_FREE_MB} MiB a lane needs")
    return _row(MEMORY_CHECK, True, f"at least {MEMORY_MIN_FREE_MB} MiB available")


def probe_rows(probe: Probe) -> list[Row]:
    images = [image_row(image, probe.creds_store, probe.helper_path) for image in probe.images]
    tools = [tool_row(name, path) for name, path in probe.tools.items()]
    space = [tmp_row(directory, line) for directory, line in probe.space[:1]] or [tmp_row("/tmp", "")]
    budget = [] if probe.skipped is None else [
        _row(PROBE_CHECK, False, f"ran past its {probe.skipped[0]}s budget; skipped {probe.skipped[1]}")]
    return [docker_row(probe.docker), *images, *tools, gh_row(probe.gh), *space, memory_row(probe.memory), *budget]


def reply_rows(returncode: int, output: str, *, remote: bool) -> list[Row]:
    """The rows for one run of the probe. With `remote`, a `host reachable` row comes first, and an unreachable reply
    has only that failing row: the doctor's view. Without it (this machine, and the chair's login check, whose
    readiness check already judged reachability) an unreachable, timed-out or unreadable reply is one failing
    `lane probe` row: the host is blocked, and its reachability is left to the readiness check."""
    unreachable = remote and is_unreachable_reply(returncode, output)
    reach = [_row(REACHABLE_CHECK, not unreachable, _cut(output) if unreachable else "answered")] if remote else []
    if unreachable:
        return reach
    if any(kind == "nosleep" for kind, _ in _fields(output)):
        return [*reach, _row(PROBE_CHECK, False, "no sleep on the lane PATH, so no command could be time-limited; "
                                                 "nothing was probed")]
    probe = parse_probe(output)
    if probe is None:
        return [*reach, _row(PROBE_CHECK, False, f"unreadable (exit {returncode}): {_cut(output)}")]
    return [*reach, *probe_rows(probe)]


def is_room_row(row: Mapping[str, object]) -> bool:
    """Temp headroom and available memory: conditions that dip and recover, judged apart from the prerequisites."""
    check = str(row["check"])
    return check == MEMORY_CHECK or check.startswith(TMP_CHECK_PREFIX)


def is_lane_line(line: str) -> bool:
    """Whether a printed doctor line is one of these rows (a table row or `row_line`) or the capabilities line."""
    names = (DOCKER_CHECK, "smoke image", *CLIENT_TOOLS, GH_CHECK, TMP_CHECK_PREFIX, MEMORY_CHECK, PROBE_CHECK)
    return line.startswith("lane capabilities:") or any(
        line.startswith(name) and line[len(name):len(name) + 1] in (" ", ":") for name in names
    )


def capabilities(rows: Sequence[Mapping[str, object]]) -> frozenset[str]:
    """`docker` when the daemon, every smoke image and every client tool passed; nothing otherwise."""
    names = {str(row["check"]) for row in rows}
    needed = {DOCKER_CHECK, *CLIENT_TOOLS, *(image_check(image) for image in SMOKE_IMAGES)}
    passed = {str(row["check"]) for row in rows if row["ok"] is True}
    return frozenset({DOCKER_CAPABILITY}) if needed <= names and needed <= passed else frozenset()


def blocking_reason(rows: Sequence[Mapping[str, object]]) -> str | None:
    """`<check>: <detail>` of every failing row, joined; None when all pass."""
    failing = [f"{row['check']}: {row['detail']}" for row in rows if row["ok"] is not True]
    return "; ".join(failing) or None


def row_line(row: Mapping[str, object]) -> str:
    """One doctor line, `<check>  ok (<detail>)` or `<check>: <detail>` for a failing row."""
    return f"{row['check']:<16} ok ({row['detail']})" if row["ok"] is True else f"{row['check']}: {row['detail']}"


def _count(versions: Mapping[str, object], key: str) -> int:
    stored = versions.get(key)
    return stored if isinstance(stored, int) and not isinstance(stored, bool) and stored > 0 else 0


def merge_versions(versions: Mapping[str, object], rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """`versions` with the check's record: `lane_ok` and `lane_reason` for the prerequisites, `room_ok`,
    `room_reason` and the consecutive `room_failures` for room, and `lane_capabilities`. A passing half drops its
    reason. The same rows on the same record give the same prerequisites; only the room streak counts on.

    Rows with no room reading at all (an unreadable probe) leave the stored room record as it was: a probe that
    could not be read says nothing about room, so it neither resets a streak nor adds to one."""
    room_rows = [row for row in rows if is_room_row(row)]
    prereq = blocking_reason([row for row in rows if not is_room_row(row)])
    room = blocking_reason(room_rows)
    dropped = (REASON_KEY, ROOM_REASON_KEY) if room_rows else (REASON_KEY,)
    kept = {key: value for key, value in versions.items() if key not in dropped}
    room_record: dict[str, object] = {
        ROOM_OK_KEY: room is None,
        ROOM_FAILURES_KEY: 0 if room is None else _count(versions, ROOM_FAILURES_KEY) + 1,
    } if room_rows else {}
    merged: dict[str, object] = {**kept, OK_KEY: prereq is None, **room_record, CAPABILITIES_KEY: sorted(capabilities(rows))}
    reasons = {REASON_KEY: prereq, ROOM_REASON_KEY: room if room_rows else None}
    return {**merged, **{key: reason for key, reason in reasons.items() if reason is not None}}


def _active(hosts: Sequence[Mapping[str, object]]) -> list[tuple[str, dict[str, object]]]:
    return [(str(row.get("name") or ""), versions_of(row)) for row in hosts if row.get("state") == "active"]


def lane_stuck(hosts: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """`active` host name to its stored reason, for each whose prerequisites failed (`lane_ok` False exactly): a
    lasting condition, so work homed there moves. A host never checked is not stuck, and neither is one whose own
    beat replaced its versions since."""
    return {
        name: str(versions.get(REASON_KEY) or "lane prerequisites failed")
        for name, versions in _active(hosts)
        if versions.get(OK_KEY) is False
    }


def lane_blocked(hosts: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """`lane_stuck`, plus each `active` host short on room `ROOM_BLOCK_AFTER` checks in a row: no new lane goes to
    either. A single short reading blocks nothing."""
    short = {
        name: str(versions.get(ROOM_REASON_KEY) or "short on room")
        for name, versions in _active(hosts)
        if _count(versions, ROOM_FAILURES_KEY) >= ROOM_BLOCK_AFTER
    }
    stuck = lane_stuck(hosts)
    return {name: "; ".join(r for r in (stuck.get(name), short.get(name)) if r) for name in {**stuck, **short}}


def gated(blocked: Mapping[str, str], gate: bool) -> Mapping[str, str]:
    """`blocked` with the gate on; nothing with it off (`policy.dispatch.lane_prereqs: off`), the switch that keeps
    an unreadable probe from grounding every host."""
    return blocked if gate else {}


def lane_needs_chair(hosts: Sequence[Mapping[str, object]]) -> list[Action]:
    """One `lane_prereqs` needs_chair per blocked host, sorted by name, its reason the failing checks."""
    return [
        {"kind": ActionKind.NEEDS_CHAIR, "host": name, "cause": CAUSE, "reason": reason}
        for name, reason in sorted(lane_blocked(hosts).items())
    ]
