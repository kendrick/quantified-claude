"""
Unit tests for the transcript-tree walk that feeds collect.

collect_from_disk is the bridge between the filesystem and the pure parser: it
rglobs a transcripts root, parses each session file, and reports both the events
and the set of session UUIDs it saw. That found-set is what merge_events needs
to tell "re-read this run" apart from "pruned, leave alone".
"""

import json

from skill_usage import collect_from_disk


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


def _slash(skill):
    return {
        "type": "user",
        "timestamp": "2026-06-17T11:00:00.000Z",
        "message": {"role": "user", "content": f"<command-name>/{skill}</command-name>"},
    }


def test_walks_every_session_and_reports_the_found_set(tmp_path):
    root = tmp_path / "projects"
    # Two sessions under one encoded-project dir; the session UUID is the stem.
    _write(root / "-Users-me-repo" / "sess1.jsonl", _tool_use("humanizer"))
    _write(root / "-Users-me-repo" / "sess2.jsonl", _slash("doc-coauthoring"))

    events, found = collect_from_disk(root, host="laptop")

    assert found == {"sess1", "sess2"}
    assert {e["skill"] for e in events} == {"humanizer", "doc-coauthoring"}
    # The host is stamped on every event from the machine that parsed it.
    assert all(e["host"] == "laptop" for e in events)
    # Session id is carried from the filename so the merge can key on it.
    assert {e["session"] for e in events} == {"sess1", "sess2"}


def test_session_present_but_eventless_still_counts_as_found(tmp_path):
    # A transcript with no skill usage must still land in found_sessions, so the
    # overlay treats it as authoritative rather than mistaking it for pruned.
    root = tmp_path / "projects"
    _write(root / "-Users-me-repo" / "quiet.jsonl",
           {"type": "user", "timestamp": "2026-06-17T09:00:00.000Z",
            "message": {"role": "user", "content": "just chatting, no skills"}})

    events, found = collect_from_disk(root, host="laptop")

    assert found == {"quiet"}
    assert events == []
