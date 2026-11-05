from devtools import release, release_resume

_V = "2026.11.0"
_REF = f"refs/rehearsals/{_V}"
_SHA = "a" * 40
_TEXT = "rehearsal=ok\nsha=" + _SHA
_TAG_OBJECT = f"object {_SHA}\ntype commit\ntag {_V}\ntagger x <x@y> 1 +0000\n\n{_TEXT}\n"
_OK = (0, "")
_FAIL = (1, "fatal: nope")


class Fake:
    """A recording `run` that answers each call from a queue of `(rc, out)`; an empty queue answers success."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd):
        self.calls.append(argv)
        return self.answers.pop(0) if self.answers else _OK

    def pushes(self):
        return [argv for argv in self.calls if "push" in argv]


def test_record_ref_is_under_refs_rehearsals():
    assert release_resume.record_ref(_V) == _REF


def test_read_record_fetches_the_ref_then_returns_the_tag_message():
    run = Fake(_OK, (0, _TAG_OBJECT))
    assert release_resume.read_record("/r", _V, run) == _TEXT
    assert run.calls == [
        ["git", "-C", "/r", "fetch", "origin", f"+{_REF}:{_REF}"],
        ["git", "-C", "/r", "cat-file", "tag", _REF],
    ]


def test_read_record_is_none_when_the_fetch_fails():
    assert release_resume.read_record("/r", _V, Fake(_FAIL)) is None


def test_read_record_is_none_when_the_read_fails_or_is_not_a_tag_object():
    assert release_resume.read_record("/r", _V, Fake(_OK, _FAIL)) is None
    assert release_resume.read_record("/r", _V, Fake(_OK, (0, "garbage"))) is None


def test_write_record_tags_then_pushes_the_record_ref_and_drops_the_scratch_tag():
    run = Fake(_FAIL)
    assert release_resume.write_record("/r", _V, _SHA, _TEXT, run)[0] is True
    scratch = f"rehearsal-record-{_V}"
    assert run.calls[1] == ["git", "-C", "/r", "tag", "-a", "-f", "--cleanup=verbatim", "-m", _TEXT, scratch, _SHA]
    assert run.pushes() == [["git", "-C", "/r", "push", "origin", f"refs/tags/{scratch}:{_REF}"]]
    assert run.calls[-1] == ["git", "-C", "/r", "tag", "-d", scratch]


def test_a_second_identical_write_is_a_success_without_a_push():
    run = Fake(_OK, (0, _TAG_OBJECT))
    ok, _ = release_resume.write_record("/r", _V, _SHA, _TEXT, run)
    assert ok is True
    assert run.pushes() == []
    assert not any("tag" in argv[3:4] for argv in run.calls)


def test_a_conflicting_write_fails_naming_both_records_without_a_push():
    run = Fake(_OK, (0, _TAG_OBJECT))
    ok, message = release_resume.write_record("/r", _V, _SHA, "rehearsal=other", run)
    assert ok is False
    assert _TEXT in message and "rehearsal=other" in message
    assert run.pushes() == []


def test_write_record_fails_with_the_output_when_the_push_is_refused():
    ok, message = release_resume.write_record("/r", _V, _SHA, _TEXT, Fake(_FAIL, _OK, (1, "rejected")))
    assert ok is False
    assert "rejected" in message


def test_write_record_fails_when_the_tag_cannot_be_created():
    run = Fake(_FAIL, (1, "bad object"))
    ok, message = release_resume.write_record("/r", _V, _SHA, _TEXT, run)
    assert ok is False
    assert "bad object" in message
    assert run.pushes() == []


def test_file_at_runs_show_and_returns_the_file():
    run = Fake((0, "text"))
    assert release_resume.file_at("/r", "origin/release/x", "manifest.toml", run) == "text"
    assert run.calls == [release.show_file_argv("/r", "origin/release/x", "manifest.toml")]


def test_file_at_is_none_on_a_non_zero_exit():
    assert release_resume.file_at("/r", "abc", "manifest.toml", Fake(_FAIL)) is None


def test_release_branch_sha_reads_the_origin_branch():
    ref = f"refs/heads/release/{_V}"
    run = Fake((0, f"{_SHA}\t{ref}\n"))
    assert release_resume.release_branch_sha("/r", _V, run) == _SHA
    assert run.calls == [release.ls_remote_ref_argv("/r", ref)]


def test_release_branch_sha_is_none_on_a_non_zero_exit_or_no_match():
    assert release_resume.release_branch_sha("/r", _V, Fake(_FAIL)) is None
    assert release_resume.release_branch_sha("/r", _V, Fake(_OK)) is None
