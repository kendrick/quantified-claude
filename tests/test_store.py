"""
Unit tests for the events store reader/writer.

The store is one JSONL file per host (events/<host>.jsonl) — append-friendly,
diff-friendly, and trivial to union across machines. read_store is lenient by
design: a missing file is an empty store, not an error, because the very first
collect on a new host has nothing to load yet.
"""

from skill_usage import read_store, union_stores, write_store


def _ev(skill, session):
    return {
        "skill": skill,
        "timestamp": "2026-06-17T00:00:00.000Z",
        "session": session,
        "host": "laptop",
        "channel": "tool",
    }


def test_write_then_read_roundtrips_events(tmp_path):
    events = [_ev("humanizer", "S1"), _ev("impeccable", "S2")]
    path = tmp_path / "events" / "laptop.jsonl"

    write_store(path, events)

    assert read_store(path) == events


def test_write_store_creates_missing_parent_dirs(tmp_path):
    # collect shouldn't have to mkdir the events/ tree itself; write_store owns
    # that so a fresh data repo just works.
    path = tmp_path / "deeply" / "nested" / "laptop.jsonl"

    write_store(path, [_ev("humanizer", "S1")])

    assert path.exists()


def test_read_store_of_missing_file_is_empty(tmp_path):
    assert read_store(tmp_path / "events" / "never-written.jsonl") == []


def test_union_concatenates_every_host_store(tmp_path):
    # render's cross-machine rollup: each machine owns one events/<host>.jsonl,
    # and the union of them is the full picture. Every machine's MOC reflects all
    # hosts because it renders from this union, not just its own file.
    laptop = _ev("humanizer", "S1")
    desktop = _ev("impeccable", "S2")
    write_store(tmp_path / "events" / "laptop.jsonl", [laptop])
    write_store(tmp_path / "events" / "desktop.jsonl", [desktop])

    merged = union_stores(tmp_path)

    assert laptop in merged and desktop in merged and len(merged) == 2


def test_union_of_empty_data_dir_is_empty(tmp_path):
    assert union_stores(tmp_path) == []
