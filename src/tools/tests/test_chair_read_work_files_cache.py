from pathlib import Path

import pytest

from agent_tools.chair_read_cache import StampCache
from agent_tools.chair_read_quarantined import read_work_files

FILES = {
    "work/a/initiative.md": "initiative a\n",
    "work/a/p1/t1.md": "task one\n",
    "work/a/p1/t2.md": "task two\n",
    "work/b/p1/t3.md": "task three\n",
}


@pytest.fixture
def reads(monkeypatch):
    """Paths whose body was read, in order."""
    seen: list[Path] = []
    original = Path.read_text

    def counting(self, *args, **kwargs):
        seen.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    return seen


@pytest.fixture
def root(tmp_path):
    for rel, text in FILES.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    return tmp_path


def test_a_second_call_over_unchanged_files_reads_no_bodies(root, reads):
    cache = StampCache(64)
    first = read_work_files(root, cache)
    assert len(reads) == len(FILES)
    reads.clear()
    assert read_work_files(root, cache) == first
    assert reads == []
    assert cache.reads == (len(FILES), len(FILES))


def test_rewriting_one_file_rereads_only_that_file(root, reads):
    cache = StampCache(64)
    read_work_files(root, cache)
    reads.clear()
    target = root / "work/a/p1/t2.md"
    target.write_text("task two, rewritten longer\n", encoding="utf-8")
    result = read_work_files(root, cache)
    assert reads == [target]
    assert result[target] == "task two, rewritten longer\n"
    assert result[root / "work/a/p1/t1.md"] == "task one\n"


def test_a_deleted_file_leaves_the_result_and_the_cache(root, reads):
    cache = StampCache(64)
    read_work_files(root, cache)
    reads.clear()
    gone = root / "work/b/p1/t3.md"
    gone.unlink()
    result = read_work_files(root, cache)
    assert gone not in result
    assert len(cache) == len(FILES) - 1
    assert reads == []


def test_no_cache_and_a_cache_return_the_same_mapping_in_path_order(root):
    plain = read_work_files(root)
    cached = read_work_files(root, StampCache(64))
    assert cached == plain
    assert list(cached) == sorted(cached)


def test_an_unreadable_file_is_left_out_and_not_cached(root, monkeypatch):
    bad = root / "work/a/p1/t1.md"
    original = Path.read_text

    def failing(self, *args, **kwargs):
        if self == bad:
            raise OSError("unreadable")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing)
    cache = StampCache(64)
    result = read_work_files(root, cache)
    assert bad not in result
    assert len(result) == len(FILES) - 1
    assert len(cache) == len(FILES) - 1
