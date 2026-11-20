"""What the smoke gate needs on a lane host, judged from one probe's output. Pure except `probe_argv`'s script, which
the edges run (`cli_doctor`, `remote_doctor`, `chair_login_check`).

On 2026-11-19 a macOS lane host was quarantined five times on setup gaps, not code: Docker Desktop not running, its
keychain credential helper failing in an unattended session, `pg_dump` off the PATH, the pinned smoke images absent,
and a stale checkout (the last is `checkout_env`'s). Each cost a build and a review before the smoke gate failed in
fixture setup. The checks here name those gaps before the chair places a lane, so the host is skipped instead.

The probe is one POSIX sh script of shell builtins and `command -v`, so it needs no cox of any version on the host and
reads the PATH the lane would. It never pulls and never writes: an absent image is judged pullable by asking the
credential helper `docker pull` would ask, so running it twice changes nothing.

Room to work is checked too: free space on /tmp (and on $TMPDIR when the lane sets one) and available memory. A
host short on either gets no lane, but keeps its capabilities: they say what it can run, not whether it has room now.

`capabilities` turns the rows into the tokens a host may advertise; `docker` only when every check it needs passes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from agent_tools.chair_login_watch import is_unreachable_reply, versions_of
from agent_tools.chair_types import Action, ActionKind
from agent_tools.remote_argv import login_shell_argv

# The smoke suite's pinned images (smoke/plan.py, which the package cannot import); a test keeps the two equal.
POSTGRES_IMAGE = "postgres:16@sha256:dd2bf3ce2dc83a81674cf7f217fcfd8a5009a4b325dda5b843bfee82a06cff6f"
FORGEJO_IMAGE = "data.forgejo.org/forgejo/forgejo:11@sha256:946243edbab116d5bb78b73ea68af6f3d69229ba1b1ed958dd82c3481167f3e0"
GARAGE_IMAGE = "dxflrs/garage:v2.1.0@sha256:4c9b34c113e61358466e83fd6e7d66e6d18657ede14b776eb78a93ee8da7cf6a"
SMOKE_IMAGES = (POSTGRES_IMAGE, FORGEJO_IMAGE, GARAGE_IMAGE)
# The store fixtures back a database up with pg_dump and the suite reads it with psql.
CLIENT_TOOLS = ("pg_dump", "psql")

DOCKER_CAPABILITY = "docker"
CAUSE = "lane_prereqs"
OK_KEY, REASON_KEY, CAPABILITIES_KEY = "lane_ok", "lane_reason", "lane_capabilities"

DOCKER_CHECK = "docker daemon"
GH_CHECK = "gh"
REACHABLE_CHECK = "host reachable"
MEMORY_CHECK = "free memory"
# A lane's builds, test runs and containers need room. On 2026-11-20 a RAM-backed /tmp filled with several builds at
# once and the machine rebooted; a host below either floor gets no lane until a later check finds room again.
TMP_MIN_FREE_MB = 2048
MEMORY_MIN_FREE_MB = 2048
PROBE_CHECK = "lane probe"
_MARK = "cox-lane-prereq"
# One doctor row: `check`, `ok` and `detail`, the shape `doctor.checks` gives.
Row = dict[str, object]
_OUTPUT_CHARS = 160

# Positional args: the client tools, `--`, then the images. Every line it prints starts with the marker, so a login
# shell's startup noise around them is ignored. Only builtins and `command -v` run, apart from the probed tools.
_SCRIPT = r"""m=cox-lane-prereq
nl='
'
first() { printf '%s' "${1%%"$nl"*}"; }
while [ "$#" -gt 0 ] && [ "$1" != -- ]; do
  printf '%s\ttool\t%s\t%s\n' "$m" "$1" "$(command -v "$1" 2>/dev/null)"
  shift
done
[ "$#" -gt 0 ] && shift
docker_path=$(command -v docker 2>/dev/null)
docker_code=127
docker_out=
if [ -n "$docker_path" ]; then
  docker_out=$("$docker_path" info --format '{{.ServerVersion}}' 2>&1)
  docker_code=$?
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
for image in "$@"; do
  if [ "$docker_code" -ne 0 ]; then
    printf '%s\timage\tunchecked\t-\t%s\t\n' "$m" "$image"
    continue
  fi
  if "$docker_path" image inspect --format '{{.Id}}' "$image" >/dev/null 2>&1; then
    printf '%s\timage\tpresent\t-\t%s\t\n' "$m" "$image"
    continue
  fi
  if [ -z "$store" ] || [ -z "$helper" ]; then
    printf '%s\timage\tabsent\t-\t%s\t\n' "$m" "$image"
    continue
  fi
  server=${image%%/*}
  case "$image" in
    */*) case "$server" in *.*|*:*|localhost) ;; *) server=https://index.docker.io/v1/ ;; esac ;;
    *) server=https://index.docker.io/v1/ ;;
  esac
  helper_out=$(printf '%s' "$server" | "$helper" get 2>&1)
  helper_code=$?
  printf '%s\timage\tabsent\t%s\t%s\t%s\n' "$m" "$helper_code" "$image" "$(first "$helper_out")"
done
gh_path=$(command -v gh 2>/dev/null)
gh_code=127
gh_out=
if [ -n "$gh_path" ]; then
  gh_out=$("$gh_path" --version 2>&1)
  gh_code=$?
fi
printf '%s\tgh\t%s\t%s\t%s\n' "$m" "$gh_code" "$gh_path" "$(first "$gh_out")"
seen_tmp=
for dir in /tmp ${TMPDIR:+"$TMPDIR"}; do
  [ "$dir" = /tmp ] && [ -n "$seen_tmp" ] && continue
  [ "$dir" = /tmp ] && seen_tmp=1
  df_out=$(df -Pk "$dir" 2>/dev/null)
  printf '%s\tspace\t%s\t%s\n' "$m" "$dir" "${df_out##*"$nl"}"
done
if [ -r /proc/meminfo ]; then
  while IFS= read -r line; do
    case "$line" in MemAvailable:*) printf '%s\tmemline\t%s\n' "$m" "$line" ;; esac
  done < /proc/meminfo
elif vm_out=$(vm_stat 2>/dev/null); then
  printf '%s\n' "$vm_out" | while IFS= read -r line; do
    case "$line" in
      *"page size of"*|"Pages free:"*|"Pages inactive:"*|"Pages speculative:"*) printf '%s\tmemline\t%s\n' "$m" "$line" ;;
    esac
  done
fi
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
    """`state` is present, absent or unchecked (docker did not answer). An absent image carries the credential
    helper's `get` exit code and first line, `helper_code` None when no helper was asked."""

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
    space: tuple[tuple[str, str], ...] = ()  # (directory, the last line `df -Pk` printed for it)
    memory: tuple[str, ...] = ()  # the MemAvailable line of /proc/meminfo, or vm_stat's page lines on macOS


def script_argv(images: Sequence[str] = SMOKE_IMAGES, tools: Sequence[str] = CLIENT_TOOLS) -> list[str]:
    """The probe for this machine, under this process's PATH."""
    return ["/bin/sh", "-c", _SCRIPT, "lane-prereqs", *tools, "--", *images]


def probe_argv(images: Sequence[str] = SMOKE_IMAGES, tools: Sequence[str] = CLIENT_TOOLS) -> list[str]:
    """The probe in the login shell a lane runs under on a remote host; wrap it in `ssh_argv` to reach one."""
    return login_shell_argv(script_argv(images, tools))


def _code(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


# How many tab-separated fields follow each kind; the last one is free text and may hold tabs itself.
_ARITY = {"tool": 2, "docker": 3, "creds": 2, "image": 4, "gh": 3, "space": 2, "memline": 1, "end": 0}


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
    return Probe(tools, Ran(docker_path, docker_rc, docker_line), store, helper, images, Ran(gh_path, gh_rc, gh_line),
                 space, memory)


def _row(check: str, ok: bool, detail: str) -> Row:
    return {"check": check, "ok": ok, "detail": detail}


def _cut(text: str) -> str:
    return " ".join(text.split())[:_OUTPUT_CHARS]


def docker_row(docker: Ran) -> Row:
    if not docker.path:
        return _row(DOCKER_CHECK, False, "no docker on the lane PATH: the smoke gate starts its fixtures with docker")
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
    if image.state == "unchecked":
        return _row(check, False, "not checked: the docker daemon did not answer")
    helper = f"docker-credential-{creds_store}"
    if creds_store and not helper_path:
        return _row(check, False, f"absent, and docker pull would fail: {helper} (credsStore) is not on the lane PATH")
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
    if gh.code != 0 or not gh.line.startswith("gh version"):
        return _row(GH_CHECK, False, f"{gh.path} is not a usable gh (exit {gh.code}): {_cut(gh.line)}")
    return _row(GH_CHECK, True, f"{gh.line.strip()} at {gh.path}")


def df_available_kb(line: str) -> int | None:
    """The Available column (KiB) of one `df -Pk` data line; None for a header, an empty or an unreadable line."""
    fields = line.split()
    return int(fields[3]) if len(fields) >= 6 and fields[3].isdigit() else None


def tmp_row(directory: str, df_line: str) -> Row:
    check = f"tmp headroom {directory}"
    free_kb = df_available_kb(df_line)
    if free_kb is None:
        return _row(check, False, f"not measured: df -Pk {directory} gave {_cut(df_line) or 'nothing'}")
    free_mb = free_kb // 1024
    if free_mb < TMP_MIN_FREE_MB:
        return _row(check, False, f"{free_mb} MiB free, under the {TMP_MIN_FREE_MB} MiB a lane needs; clear old "
                                  f"build and test directories under {directory}")
    return _row(check, True, f"at least {TMP_MIN_FREE_MB} MiB free")


def available_memory_kb(lines: Sequence[str]) -> int | None:
    """KiB available: `MemAvailable` from /proc/meminfo, else free, inactive and speculative pages from vm_stat."""
    for line in lines:
        if line.startswith("MemAvailable:"):
            value = line.split()[1:2]
            return int(value[0]) if value and value[0].isdigit() else None
    page = next((line.split("page size of", 1)[1].split()[0] for line in lines if "page size of" in line), "")
    pages = [line.split(":", 1)[1].strip().rstrip(".") for line in lines if line.startswith("Pages ")]
    if not page.isdigit() or not pages or not all(p.isdigit() for p in pages):
        return None
    return sum(int(p) for p in pages) * int(page) // 1024


def memory_row(lines: Sequence[str]) -> Row:
    free_kb = available_memory_kb(lines)
    if free_kb is None:
        return _row(MEMORY_CHECK, False, "not measured: no MemAvailable in /proc/meminfo and no vm_stat")
    free_mb = free_kb // 1024
    if free_mb < MEMORY_MIN_FREE_MB:
        return _row(MEMORY_CHECK, False, f"{free_mb} MiB available, under the {MEMORY_MIN_FREE_MB} MiB a lane needs")
    return _row(MEMORY_CHECK, True, f"at least {MEMORY_MIN_FREE_MB} MiB available")


def probe_rows(probe: Probe) -> list[Row]:
    images = [image_row(image, probe.creds_store, probe.helper_path) for image in probe.images]
    tools = [tool_row(name, path) for name, path in probe.tools.items()]
    space = [tmp_row(directory, line) for directory, line in probe.space] or [tmp_row("/tmp", "")]
    return [docker_row(probe.docker), *images, *tools, gh_row(probe.gh), *space, memory_row(probe.memory)]


def reply_rows(returncode: int, output: str, *, remote: bool) -> list[Row]:
    """The rows for one run of the probe: one `host reachable` row first when `remote`, then the checks. An
    unreachable reply has only the failing reachability row; an unreadable one fails as `lane probe`."""
    unreachable = remote and is_unreachable_reply(returncode, output)
    reach = [_row(REACHABLE_CHECK, not unreachable, _cut(output) if unreachable else "answered")] if remote else []
    if unreachable:
        return reach
    probe = parse_probe(output)
    if probe is None:
        return [*reach, _row(PROBE_CHECK, False, f"unreadable (exit {returncode}): {_cut(output)}")]
    return [*reach, *probe_rows(probe)]


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


def merge_versions(versions: Mapping[str, object], rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """`versions` with `lane_ok`, `lane_capabilities` and, on a failure, `lane_reason` set; a pass drops the reason.
    The same rows give the same record, so a second check rewrites nothing."""
    reason = blocking_reason(rows)
    kept = {key: value for key, value in versions.items() if key != REASON_KEY}
    merged = {**kept, OK_KEY: reason is None, CAPABILITIES_KEY: sorted(capabilities(rows))}
    return merged if reason is None else {**merged, REASON_KEY: reason}


def lane_blocked(hosts: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """`active` host name to its stored reason, for each whose `lane_ok` is False exactly. A host never checked is
    not blocked, and neither is one whose own beat replaced its versions since."""
    active = [(str(row.get("name") or ""), versions_of(row)) for row in hosts if row.get("state") == "active"]
    return {
        name: str(versions.get(REASON_KEY) or "lane prerequisites failed")
        for name, versions in active
        if versions.get(OK_KEY) is False
    }


def lane_needs_chair(hosts: Sequence[Mapping[str, object]]) -> list[Action]:
    """One `lane_prereqs` needs_chair per blocked host, sorted by name, its reason the failing checks."""
    return [
        {"kind": ActionKind.NEEDS_CHAIR, "host": name, "cause": CAUSE, "reason": reason}
        for name, reason in sorted(lane_blocked(hosts).items())
    ]
