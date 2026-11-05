"""A cut records the rehearsal run it was gated on; a later call judges that run by id."""
import json

import pytest
from test_release import _MANIFEST_TOML, _REHEARSAL_NOTES_LINE, _TAGGED_SHA, V0, _checkout, _rehearsal_edge, _sha_of

from devtools import cli, release, release_rehearsal_gate, release_resume

_REAL_REHEARSAL_STEPS = cli._rehearsal_steps
_REF = release_resume.record_ref(V0)
_JOBS = [{"name": "fresh", "conclusion": "success"}, {"name": "upgrade", "conclusion": "success"},
         {"name": "migrate", "conclusion": "skipped"}]
_MOVED = "cd" * 20


class Edge:
    """The runner behind `cli._real_run` and `cli._stdout_run`: gh lists `runs`, a recorded run views as
    `conclusion` with `_JOBS`, origin's `refs/rehearsals/<V0>` holds `record` (None: absent), and a push of it
    returns `push_rc`. Every argv is kept. Anything else is the rehearsal edge's clean fake."""

    def __init__(self, runs=(), record=None, conclusion="success", push_rc=0, origin=None):
        self.runs, self.record, self.conclusion, self.push_rc, self.origin = runs, record, conclusion, push_rc, origin
        self.calls: list[list[str]] = []
        self.base = _rehearsal_edge(list(runs), _JOBS)

    def __call__(self, argv, cwd):
        self.calls.append(argv)
        if argv[:3] == ["gh", "run", "view"]:
            return 0, json.dumps({"conclusion": self.conclusion, "jobs": _JOBS})
        if f"+{_REF}:{_REF}" in argv:
            return (0, "") if self.record is not None else (1, "fatal: couldn't find remote ref")
        if argv[3:5] == ["cat-file", "tag"]:
            return 0, f"object {_sha_of('x')}\ntype commit\ntag t\n\n{self.record}\n"
        if argv[3:4] == ["push"] and any(_REF in part for part in argv):
            return self.push_rc, "! [rejected]"
        if argv[3:4] == ["show"] and argv[4].endswith(":manifest.toml"):
            return 0, _MANIFEST_TOML
        if self.origin and argv[3:4] == ["rev-parse"] and argv[-1].startswith("origin/"):
            return 0, self.origin + "\n"
        return self.base(argv, cwd)

    def pushes(self):
        return [argv for argv in self.calls if argv[3:4] == ["push"] and any(_REF in part for part in argv)]

    def tags(self):
        return [argv for argv in self.calls if argv[3:5] == ["tag", "-a"] and f"rehearsal-record-{V0}" in argv]

    def listings(self):
        return [argv for argv in self.calls if argv[:3] == ["gh", "run", "list"] and "release-rehearsal.yml" in argv]

    def views(self):
        return [argv for argv in self.calls if argv[:3] == ["gh", "run", "view"]]


def _runs(umbrella, run_id=7):
    title = release_rehearsal_gate.run_title(_sha_of(umbrella), "pinned", "v0.47.0", [])
    return [{"databaseId": run_id, "displayTitle": title, "conclusion": "success", "createdAt": "2026-10-01T10:00:00Z"}]


def _record(umbrella, run_id=7, sha=None):
    title = release_rehearsal_gate.run_title(sha or _sha_of(umbrella), "pinned", "v0.47.0", [])
    return release_rehearsal_gate.record_text(
        release_rehearsal_gate.new_record(V0, sha or _sha_of(umbrella), "", run_id, title))


@pytest.fixture
def cut(tmp_path, monkeypatch):
    """`cut(edge, *flags, notes="")`: `cli.main(["release", V0, ...])` with the real rehearsal gate on `edge`."""
    monkeypatch.setattr(cli, "_maintainer_remote_url", lambda directory: "git@github.com:ppfenning/coxswain.git")
    monkeypatch.setattr(cli, "_tap_state", lambda directory: "clean")
    monkeypatch.setattr(cli, "_repository_url", lambda: "https://github.com/ppfenning/coxswain")
    monkeypatch.setattr(cli, "_remote_tags", lambda repo: [])
    monkeypatch.setattr(cli, "_component_declares_tag_trigger", lambda directory: True)
    monkeypatch.setattr(cli, "_rehearsal_steps", _REAL_REHEARSAL_STEPS)
    monkeypatch.setattr(release, "_tap_formula_steps", lambda *a, **k: [])

    def run(edge, *flags, notes=""):
        umbrella = _checkout(tmp_path, notes=notes)
        monkeypatch.setattr(cli, "_real_run", edge)
        monkeypatch.setattr(cli, "_stdout_run", edge)
        return cli.main(["release", V0, *flags, "--manifest", str(umbrella / "manifest.toml")]), umbrella
    return run


def test_a_fresh_passing_cut_writes_one_record_with_the_run_id_and_shas(cut, tmp_path):
    edge = Edge(runs=_runs(tmp_path / "coxswain"))
    rc, umbrella = cut(edge)
    assert rc == 0
    assert len(edge.pushes()) == 1
    (tag,) = edge.tags()
    assert json.loads(tag[tag.index("-m") + 1]) == {
        "version": V0, "coxswain": _sha_of(umbrella), "dash": "", "run_id": 7,
        "run_title": release_rehearsal_gate.run_title(_sha_of(umbrella), "pinned", "v0.47.0", [])}
    assert tag[-1] == _sha_of(umbrella)


def test_the_record_is_written_before_the_first_release_step(cut, tmp_path):
    edge = Edge(runs=_runs(tmp_path / "coxswain"))
    cut(edge)
    first_release_step = next(i for i, argv in enumerate(edge.calls) if argv[3:4] == ["push"] and _REF not in argv[-1])
    assert edge.calls.index(edge.pushes()[0]) < first_release_step


def test_a_refused_rehearsal_writes_no_record(cut):
    edge = Edge(runs=[])
    rc, _ = cut(edge)
    assert rc == 2
    assert edge.pushes() == [] and edge.tags() == []


def test_a_dry_run_writes_no_record(cut, tmp_path):
    edge = Edge(runs=_runs(tmp_path / "coxswain"))
    rc, _ = cut(edge, "--dry-run")
    assert rc == 0
    assert edge.pushes() == [] and edge.tags() == []


def test_an_override_writes_no_record(cut):
    edge = Edge(runs=[])
    rc, _ = cut(edge, "--allow-no-rehearsal", "the runner image was unavailable", notes=f"{_REHEARSAL_NOTES_LINE}\n")
    assert rc == 0
    assert edge.pushes() == [] and edge.tags() == [] and edge.listings() == []


def test_a_failed_write_stops_the_cut_naming_the_ref(cut, tmp_path, capsys):
    edge = Edge(runs=_runs(tmp_path / "coxswain"), push_rc=1)
    rc, _ = cut(edge)
    out = capsys.readouterr().out
    assert rc == 2
    assert f"refuse rehearsal: could not record the rehearsal at {_REF}" in out
    assert "tag_self" not in out
    assert not any(argv[3:4] == ["push"] and _REF not in argv[-1] for argv in edge.calls)


def test_a_record_present_makes_no_title_search_and_writes_nothing(cut, tmp_path):
    edge = Edge(record=_record(tmp_path / "coxswain"))
    rc, _ = cut(edge)
    assert rc == 0
    assert edge.listings() == [] and edge.pushes() == [] and edge.tags() == []
    assert [argv[3] for argv in edge.views()] == [str(7)]


def test_a_dry_run_reads_a_record_and_judges_its_run_by_id(cut, tmp_path, capsys):
    edge = Edge(record=_record(tmp_path / "coxswain", run_id=41))
    rc, _ = cut(edge, "--dry-run")
    assert rc == 0
    assert "note rehearsal: rehearsal passed: gh run view 41" in capsys.readouterr().out
    assert edge.listings() == [] and edge.pushes() == []


def test_a_record_naming_a_failed_run_refuses(cut, tmp_path, capsys):
    edge = Edge(record=_record(tmp_path / "coxswain"), conclusion="failure")
    rc, _ = cut(edge)
    assert rc == 2
    assert "refuse rehearsal: The run concluded failure, not success. Run: gh run view 7." in capsys.readouterr().out
    assert edge.pushes() == []


def test_an_unparseable_record_is_no_record_and_the_write_then_refuses(cut, tmp_path, capsys):
    edge = Edge(runs=_runs(tmp_path / "coxswain"), record="not a record")
    rc, _ = cut(edge)
    assert rc == 2
    assert "already exists and differs" in capsys.readouterr().out
    assert len(edge.listings()) == 1 and edge.pushes() == []


def test_rehearsed_shas_are_the_recorded_commits_when_origin_has_moved(tmp_path):
    umbrella = _checkout(tmp_path, notes="")
    plan = release.release_plan({"coxswain": {"version": V0}, "components": {}}, V0, {"coxswain": []})
    edge = Edge(origin=_MOVED)
    record = release_rehearsal_gate.parse_record(_record(umbrella, sha=_TAGGED_SHA))
    assert cli._rehearsed_shas(plan, (), V0, str(tmp_path), {}, str(umbrella), edge) == {"coxswain": _MOVED}
    assert cli._rehearsed_shas(plan, (), V0, str(tmp_path), {}, str(umbrella), edge, None, record) == {
        "coxswain": _TAGGED_SHA}


def test_the_release_branch_is_cut_from_the_recorded_commit_when_origin_has_moved(cut, tmp_path):
    umbrella = tmp_path / "coxswain"
    edge = Edge(record=_record(umbrella, sha=_TAGGED_SHA), origin=_MOVED)
    rc, umbrella = cut(edge)
    assert rc == 0
    assert release.release_branch_push_argv(str(umbrella), _TAGGED_SHA, f"release/{V0}") in edge.calls
    assert not any(_MOVED in part for argv in edge.calls for part in argv)
