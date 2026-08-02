"""
Tests for the usage report, the second local artifact.

The MOC stays a skills map with one number added. This is where the exploration
lives: per-model behavior split across the spawn boundary, and per-agent-type
cost and completion.

Models × Thread is the section the module exists for. The main thread and the
subagents it spawns don't run the same mix of models, and a single per-model
number averages that difference away into something that describes neither side.

Every section has to survive an empty stream, because a fresh host has no stores
at all and still has to render a file rather than raise.
"""

from datetime import datetime, timezone
from pathlib import Path

from skill_usage import (render_agent_types, render_models_by_thread,
                         render_report, run_render, short_model, write_store)


def _usage(model="claude-opus-5", thread="main", messages=1,
           tokens=(10, 20, 30, 40), session="S", skill=None,
           ts="2026-07-14T10:00:00.000Z"):
    return {
        "session": session, "host": "h", "thread": thread, "model": model,
        "skill": skill, "messages": messages,
        "input_tokens": tokens[0], "output_tokens": tokens[1],
        "cache_creation_input_tokens": tokens[2],
        "cache_read_input_tokens": tokens[3],
        "timestamp": ts, "cc_version": "2.1.220",
    }


def _spawn(agent_type="Explore", status="completed", total_tokens=1000,
           agent_id="a1", added=0, removed=0, ts="2026-07-14T10:00:00.000Z"):
    return {
        "session": "S", "host": "h", "timestamp": ts, "agent_id": agent_id,
        "agent_type": agent_type, "requested_model": None,
        "resolved_model": "claude-opus-5", "status": status,
        "total_tokens": total_tokens, "duration_ms": 1000, "tool_use_count": 5,
        "tool_stats": {"linesAdded": added, "linesRemoved": removed},
        "cc_version": "2.1.220",
    }


def _cells(lines, label):
    for line in lines:
        if line.startswith("|") and line.split("|")[1].strip() == label:
            return [c.strip() for c in line.strip("|").split("|")]
    raise AssertionError(f"no row for {label!r} in:\n" + "\n".join(lines))


def test_short_model_strips_the_prefix_and_the_date():
    assert short_model("claude-haiku-4-5-20251001") == "haiku-4-5"
    assert short_model("claude-opus-5") == "opus-5"
    # A model that doesn't follow the convention passes through untouched rather
    # than getting mangled by a greedy rule.
    assert short_model("some-other-model") == "some-other-model"
    assert short_model(None) == "<unknown>"


def test_models_by_thread_splits_one_model_across_the_boundary():
    rows = [
        _usage(thread="main", messages=3, tokens=(100, 0, 0, 0)),
        _usage(thread="agent", messages=7, tokens=(50, 0, 0, 0)),
    ]

    cells = _cells(render_models_by_thread(rows), "opus-5")

    assert cells[1] == "3" and cells[2] == "100"
    assert cells[3] == "7" and cells[4] == "50"
    assert cells[5] == "150"


def test_models_by_thread_shows_a_divergent_mix():
    # The finding the section exists to surface: opus on the main thread, haiku
    # inside the subagents. A single per-model row would hide this.
    rows = [
        _usage(model="claude-opus-5", thread="main", messages=10, tokens=(1000, 0, 0, 0)),
        _usage(model="claude-haiku-4-5-20251001", thread="agent", messages=40,
               tokens=(400, 0, 0, 0)),
    ]

    lines = render_models_by_thread(rows)

    assert _cells(lines, "opus-5")[3] == "0"       # no agent messages
    assert _cells(lines, "haiku-4-5")[1] == "0"    # no main messages


def test_models_by_thread_rows_sort_by_total_tokens():
    rows = [_usage(model="claude-haiku-4-5", tokens=(1, 0, 0, 0)),
            _usage(model="claude-opus-5", tokens=(9000, 0, 0, 0))]

    labels = [l.split("|")[1].strip() for l in render_models_by_thread(rows)
              if l.startswith("|") and "Model" not in l and "---" not in l]

    assert labels == ["opus-5", "haiku-4-5"]


def test_a_usage_row_with_an_unknown_thread_is_skipped_not_crashed():
    # Forward compatibility: a future thread value shouldn't take the section
    # down, and it has nowhere honest to go in a two-column split.
    rows = [_usage(thread="main", messages=1), _usage(thread="sidechain", messages=99)]

    assert _cells(render_models_by_thread(rows), "opus-5")[1] == "1"


def test_completion_is_measured_over_resolved_runs_only():
    # An async_launched spawn is one whose parent recorded the launch and never
    # saw the outcome. Counting it as not-completed turns "we didn't look" into
    # "it failed": here 2 of 3 RESOLVED runs completed, so 66%, not 50%.
    spawns = [_spawn(status="completed"), _spawn(status="completed"),
              _spawn(status="async_launched"), _spawn(status="error")]

    cells = _cells(render_agent_types(spawns), "Explore")

    assert cells[1] == "4"      # spawns
    assert cells[2] == "1"      # async
    assert cells[3] == "66%"    # 2 completed of 3 resolved


def test_an_all_async_type_reports_no_completion_rate():
    # 0% would claim they all failed. Nothing resolved, so there's nothing to
    # rate, and a dash says exactly that.
    cells = _cells(render_agent_types([_spawn(status="async_launched")] * 3), "Explore")

    assert cells[1] == "3" and cells[2] == "3" and cells[3] == "—"


def test_agent_types_median_with_an_odd_count():
    spawns = [_spawn(total_tokens=t) for t in (100, 300, 200)]

    assert _cells(render_agent_types(spawns), "Explore")[4] == "200"


def test_agent_types_median_with_an_even_count():
    # Averages the middle pair: (200 + 300) / 2 = 250.
    spawns = [_spawn(total_tokens=t) for t in (100, 200, 300, 400)]

    assert _cells(render_agent_types(spawns), "Explore")[4] == "250"


def test_agent_token_totals_fall_back_to_the_agents_own_usage_rows():
    # Async spawns report no totals. Without this fallback a whole class of run
    # would read as free.
    spawns = [_spawn(status="async_launched", total_tokens=None, agent_id="x")]
    usage = [_usage(session="agent-x", tokens=(500, 500, 0, 0))]

    cells = _cells(render_agent_types(spawns, usage), "Explore")

    assert cells[5] == "1.0k"


def test_a_spawn_row_with_tokens_is_not_overridden_by_the_fallback():
    spawns = [_spawn(total_tokens=42, agent_id="x")]
    usage = [_usage(session="agent-x", tokens=(9999, 0, 0, 0))]

    assert _cells(render_agent_types(spawns, usage), "Explore")[5] == "42"


def test_agent_types_sums_lines_touched_from_tool_stats():
    spawns = [_spawn(added=100, removed=5), _spawn(added=20, removed=1)]

    assert _cells(render_agent_types(spawns), "Explore")[6] == "+120 / -6"


def test_agent_types_groups_by_type_and_sorts_by_spawn_count():
    spawns = ([_spawn(agent_type="worker-standard")] * 3
              + [_spawn(agent_type="Explore")])

    labels = [l.split("|")[1].strip() for l in render_agent_types(spawns)
              if l.startswith("|") and "Agent type" not in l and "---" not in l]

    assert labels == ["worker-standard", "Explore"]


def test_a_spawn_with_no_type_lands_in_an_unknown_bucket():
    # Loud rather than dropped, same reasoning as the <unknown> skill bucket.
    assert _cells(render_agent_types([_spawn(agent_type=None)]), "<unknown>")[1] == "1"


def test_the_report_renders_both_section_headings():
    report = render_report([_usage()], [_spawn()], [])

    assert "## Models × Thread" in report
    assert "## Agent Types" in report


def test_the_report_survives_every_stream_being_empty():
    # A fresh host. Nothing collected yet, and render still has to write a file.
    report = render_report([], [], [])

    assert "## Models × Thread" in report
    assert "*(no usage rows yet)*" in report
    assert "*(no spawn rows yet)*" in report


def test_run_render_writes_the_report_beside_the_moc(tmp_path):
    data_dir = tmp_path / "data"
    write_store(data_dir / "events" / "h.jsonl",
                [{"skill": "humanizer", "timestamp": "2026-07-14T10:00:00.000Z",
                  "session": "S", "host": "h", "channel": "tool", "thread": "main"}])
    write_store(data_dir / "usage" / "h.jsonl", [_usage()])
    write_store(data_dir / "agents" / "h.jsonl", [_spawn()])
    report = tmp_path / "out" / "report.md"

    run_render(data_dir, tmp_path / "out" / "moc.md", inventory={},
               report_path=report)

    assert "## Agent Types" in report.read_text(encoding="utf-8")


def test_run_render_without_a_report_path_writes_only_the_moc(tmp_path):
    data_dir = tmp_path / "data"
    write_store(data_dir / "events" / "h.jsonl", [])

    run_render(data_dir, tmp_path / "out" / "moc.md", inventory={})

    assert not (tmp_path / "out" / "report.md").exists()


def test_since_filters_the_spawn_rows_the_report_sees(tmp_path):
    data_dir = tmp_path / "data"
    write_store(data_dir / "events" / "h.jsonl", [])
    write_store(data_dir / "agents" / "h.jsonl", [
        _spawn(agent_id="old", ts="2026-06-01T00:00:00.000Z"),
        _spawn(agent_id="new", ts="2026-07-20T00:00:00.000Z"),
    ])
    report = tmp_path / "out" / "report.md"

    run_render(data_dir, tmp_path / "out" / "moc.md", inventory={},
               since=datetime(2026, 7, 1, tzinfo=timezone.utc), report_path=report)

    assert _cells(report.read_text(encoding="utf-8").splitlines(), "Explore")[1] == "1"
