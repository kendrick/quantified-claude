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


def _usage(skill=None, message_id="msg_1", model="claude-opus-5", input_tokens=10):
    return {
        "type": "assistant",
        "timestamp": "2026-06-17T10:00:00.000Z",
        "attributionSkill": skill,
        "message": {
            "role": "assistant",
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": 20,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 40,
            },
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


def test_one_collect_writes_both_the_events_and_usage_stores(tmp_path):
    # The streams are siblings, not a stream and an afterthought: one walk of the
    # transcripts feeds both parsers, and one collect populates both stores.
    transcripts = tmp_path / "projects"
    data_dir = tmp_path / "data"
    _write(transcripts / "-Users-me-repo" / "sess1.jsonl",
           _tool_use("humanizer"), _usage(skill="humanizer"))

    run_collect(transcripts, data_dir, host="laptop")

    events = read_store(data_dir / "events" / "laptop.jsonl")
    usage = read_store(data_dir / "usage" / "laptop.jsonl")
    assert {e["skill"] for e in events} == {"humanizer"}
    assert [(r["model"], r["skill"], r["input_tokens"]) for r in usage] == [
        ("claude-opus-5", "humanizer", 10)
    ]
    assert all(r["session"] == "sess1" and r["host"] == "laptop" for r in usage)


def test_the_usage_store_is_idempotent_across_repeated_runs(tmp_path):
    # Aggregate rows are recomputed wholesale per session on every parse, so a
    # re-collect has to land on byte-identical numbers rather than accumulating.
    # This is what makes the scheduled run safe to fire as often as it likes.
    transcripts = tmp_path / "projects"
    data_dir = tmp_path / "data"
    _write(transcripts / "-Users-me-repo" / "sess1.jsonl",
           _usage(skill="humanizer", message_id="a"),
           _usage(skill="humanizer", message_id="b"))

    run_collect(transcripts, data_dir, host="laptop")
    first = read_store(data_dir / "usage" / "laptop.jsonl")
    run_collect(transcripts, data_dir, host="laptop")
    second = read_store(data_dir / "usage" / "laptop.jsonl")

    assert first == second
    assert [r["messages"] for r in first] == [2]


def test_a_pruned_sessions_usage_rows_survive(tmp_path):
    # The retention story applies to tokens exactly as it does to skill events:
    # once a session's usage is in the store it outlives the transcript, because
    # the pruned session simply isn't in found_sessions.
    transcripts = tmp_path / "projects"
    data_dir = tmp_path / "data"

    _write(transcripts / "-Users-me-repo" / "old.jsonl", _usage(skill="humanizer"))
    run_collect(transcripts, data_dir, host="laptop")

    (transcripts / "-Users-me-repo" / "old.jsonl").unlink()
    _write(transcripts / "-Users-me-repo" / "new.jsonl", _usage(skill="impeccable"))
    run_collect(transcripts, data_dir, host="laptop")

    usage = read_store(data_dir / "usage" / "laptop.jsonl")
    assert {r["skill"] for r in usage} == {"humanizer", "impeccable"}
    assert {r["session"] for r in usage} == {"old", "new"}


def test_a_session_that_yields_tokens_but_no_skill_events_still_writes_usage(tmp_path):
    # The common case, actually: most assistant turns spend tokens without any
    # skill being invoked. The usage store must not inherit the events store's
    # emptiness.
    transcripts = tmp_path / "projects"
    data_dir = tmp_path / "data"
    _write(transcripts / "-Users-me-repo" / "quiet.jsonl", _usage(skill=None))

    run_collect(transcripts, data_dir, host="laptop")

    assert read_store(data_dir / "events" / "laptop.jsonl") == []
    (row,) = read_store(data_dir / "usage" / "laptop.jsonl")
    assert row["skill"] is None and row["messages"] == 1
