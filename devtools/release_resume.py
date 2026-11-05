"""The git edge of a release resume: the rehearsal record and the files a resume compares it with.

Every function takes `run(argv, cwd) -> (rc, out)` and does nothing else to the world. The record
text is opaque here; its format belongs to `release_rehearsal_gate`.
"""

from collections.abc import Callable

from devtools import release

Run = Callable[[list[str], str], tuple[int, str]]


def record_ref(version: str) -> str:
    """`refs/rehearsals/<version>`, the ref that holds the release record."""
    return f"refs/rehearsals/{version}"


def _scratch_tag(version: str) -> str:
    """The local tag name the record is built under before it is pushed to `record_ref`."""
    return f"rehearsal-record-{version}"


def _fetch_argv(directory: str, version: str) -> list[str]:
    ref = record_ref(version)
    return ["git", "-C", directory, "fetch", "origin", f"+{ref}:{ref}"]


def _read_argv(directory: str, version: str) -> list[str]:
    return ["git", "-C", directory, "cat-file", "tag", record_ref(version)]


# `run` has no stdin, so the tag object is made by `git tag -a` under a scratch name, then pushed to the record ref.
def _tag_argv(directory: str, version: str, sha: str, text: str) -> list[str]:
    return ["git", "-C", directory, "tag", "-a", "-f", "--cleanup=verbatim", "-m", text, _scratch_tag(version), sha]


def _push_argv(directory: str, version: str) -> list[str]:
    return ["git", "-C", directory, "push", "origin", f"refs/tags/{_scratch_tag(version)}:{record_ref(version)}"]


def _untag_argv(directory: str, version: str) -> list[str]:
    return ["git", "-C", directory, "tag", "-d", _scratch_tag(version)]


def read_record(directory: str, version: str, run: Run) -> str | None:
    """The record text at origin's `record_ref`, without the newline git adds; None when absent or unreadable."""
    rc, _ = run(_fetch_argv(directory, version), directory)
    if rc != 0:
        return None
    rc, out = run(_read_argv(directory, version), directory)
    _, sep, message = out.partition("\n\n")
    return message.removesuffix("\n") if rc == 0 and sep else None


def write_record(directory: str, version: str, sha: str, text: str, run: Run) -> tuple[bool, str]:
    """Store `text` as an annotated tag at `record_ref` pointing at `sha` and push it.

    An identical record already at origin is success without a push. A different one is a failure naming both.
    """
    existing = read_record(directory, version, run)
    if existing == text.removesuffix("\n"):
        return True, f"record {record_ref(version)} already present"
    if existing is not None:
        return False, f"record {record_ref(version)} already exists and differs.\nexisting:\n{existing}\nnew:\n{text}"
    rc, out = run(_tag_argv(directory, version, sha, text), directory)
    if rc != 0:
        return False, f"could not create the record tag: {out.strip()}"
    rc, out = run(_push_argv(directory, version), directory)
    run(_untag_argv(directory, version), directory)
    if rc != 0:
        return False, f"could not push {record_ref(version)}: {out.strip()}"
    return True, f"record pushed to {record_ref(version)}"


def file_at(directory: str, ref: str, path: str, run: Run) -> str | None:
    """`path` as it is at `ref`; None when git cannot show it."""
    rc, out = run(release.show_file_argv(directory, ref, path), directory)
    return out if rc == 0 else None


def release_branch_sha(directory: str, version: str, run: Run) -> str | None:
    """The commit origin's `release/<version>` branch points at; None when absent or unreadable."""
    ref = f"refs/heads/{release.release_branch_name(version)}"
    rc, out = run(release.ls_remote_ref_argv(directory, ref), directory)
    return release.parse_ls_remote_ref(out, ref) if rc == 0 else None
