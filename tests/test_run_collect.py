"""
Integration test for run_collect: the end-to-end data flow of `collect`, minus
git. It threads the layers together — read the host store, parse the transcripts
on disk, overlay by session, write the store back — so the composition itself is
covered, not just the pieces. git sync is deliberately out of scope here; it's
thin best-effort glue layered on top in cmd_collect.
"""

import json

from skill_usage import read_store, run_collect


def _write(path, *records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _tool_use(skill):
    return {
        "type": "assistant",
        "timestamp": "2026-06-17T10:00:00.000Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Skill", "input": {"skill": skill}}],
        },
    }


def test_run_collect_writes_host_store_from_transcripts(tmp_path):
    transcripts = tmp_path / "projects"
    data_dir = tmp_path / "data"
    _write(transcripts / "-Users-me-repo" / "sess1.jsonl", _tool_use("humanizer"))

    run_collect(transcripts, data_dir, host="laptop")

    store = read_store(data_dir / "events" / "laptop.jsonl")
    assert {e["skill"] for e in store} == {"humanizer"}
    assert all(e["host"] == "laptop" for e in store)


def test_run_collect_is_idempotent_across_repeated_runs(tmp_path):
    # Running collect on an unchanged transcript tree twice must equal once — the
    # session-keyed overlay guarantees it. This is the property that makes a
    # scheduled collect safe to fire as often as it likes.
    transcripts = tmp_path / "projects"
    data_dir = tmp_path / "data"
    _write(transcripts / "-Users-me-repo" / "sess1.jsonl", _tool_use("humanizer"))

    run_collect(transcripts, data_dir, host="laptop")
    first = read_store(data_dir / "events" / "laptop.jsonl")
    run_collect(transcripts, data_dir, host="laptop")
    second = read_store(data_dir / "events" / "laptop.jsonl")

    assert first == second


def test_run_collect_preserves_a_pruned_sessions_history(tmp_path):
    # Simulate the retention story: a session was collected, then its transcript
    # aged off disk. A later collect (with only newer transcripts present) must
    # keep the old session's events in the store.
    transcripts = tmp_path / "projects"
    data_dir = tmp_path / "data"

    _write(transcripts / "-Users-me-repo" / "old.jsonl", _tool_use("humanizer"))
    run_collect(transcripts, data_dir, host="laptop")

    # old.jsonl is pruned; a new session appears.
    (transcripts / "-Users-me-repo" / "old.jsonl").unlink()
    _write(transcripts / "-Users-me-repo" / "new.jsonl", _tool_use("impeccable"))
    run_collect(transcripts, data_dir, host="laptop")

    store = read_store(data_dir / "events" / "laptop.jsonl")
    assert {e["skill"] for e in store} == {"humanizer", "impeccable"}
