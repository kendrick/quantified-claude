"""
Tests for the audit subcommand.

Module 1 kept a loud <unknown> bucket so a parser regression after a Claude Code
bump surfaced immediately instead of quietly corrupting counts. The audit
extends that across all four streams and makes it rerunnable.

Read-only, always exit 0. It reports rather than judges, because most of these
counts move for legitimate reasons: a pruned transcript, a new agent type, a
model that genuinely got rerouted.

The token check is deliberately NOT an equality test. `totalTokens` is the LAST
message's usage block, so it measures the context a run ended at, not what it
consumed. Aggregate rows can't reconstruct "the last message", so the checkable
invariant is the ordering: final context can't exceed cumulative processed.
"""

from skill_usage import audit_report


def _usage(session, tokens):
    return {"session": session, "host": "h", "thread": "agent",
            "model": "claude-opus-5", "skill": None, "messages": 1,
            "input_tokens": tokens, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "timestamp": "2026-07-14T10:00:00.000Z", "cc_version": "2.1.220"}


def _spawn(agent_id="x", total_tokens=100, requested=None,
           resolved="claude-opus-5", status="completed"):
    return {"session": "S", "host": "h", "timestamp": "2026-07-14T10:00:00.000Z",
            "agent_id": agent_id, "agent_type": "Explore",
            "requested_model": requested, "resolved_model": resolved,
            "status": status, "total_tokens": total_tokens, "duration_ms": 1,
            "tool_use_count": 1, "tool_stats": {}, "cc_version": "2.1.220"}


def test_a_consistent_pair_reports_no_violation():
    report = audit_report([], [_usage("agent-x", 5000)], [_spawn(total_tokens=100)])

    assert "1 spawn/agent-file pairs compared, 0 where" in report


def test_a_final_context_larger_than_the_run_is_flagged():
    # The one thing that can't legitimately happen: a run can't end holding more
    # context than it ever processed.
    report = audit_report([], [_usage("agent-x", 50)], [_spawn(total_tokens=9000)])

    assert "1 where the reported final context exceeds" in report
    assert "agent-x: spawn reports 9000, usage rows only sum to 50" in report


def test_a_spawn_with_no_agent_file_is_counted_as_an_orphan():
    # The agent file was pruned but the spawn row survives, which is the whole
    # point of keeping the agentId link.
    report = audit_report([], [], [_spawn(agent_id="gone")])

    assert "- spawn rows whose agent file was never seen: 1" in report
    assert "No comparable pairs" in report


def test_a_shorthand_expanding_to_its_own_family_is_not_divergence():
    # You type "opus" and it becomes "claude-opus-5[1m]". Those differ as
    # strings on every single spawn, so a raw compare makes the tripwire useless.
    spawns = [_spawn(requested="opus", resolved="claude-opus-5[1m]"),
              _spawn(requested="haiku", resolved="claude-haiku-4-5-20251001"),
              _spawn(requested="sonnet", resolved="claude-sonnet-5")]

    report = audit_report([], [], spawns)

    assert "resolved model differs from the requested one: 0" in report


def test_a_shorthand_resolving_to_another_family_is_divergence():
    # Real rerouting: you asked for haiku and got opus.
    report = audit_report([], [], [_spawn(requested="haiku", resolved="claude-opus-5")])

    assert "resolved model differs from the requested one: 1" in report
    assert "requested haiku, resolved claude-opus-5" in report


def test_the_unknown_skill_bucket_is_reported():
    events = [{"skill": "<unknown>", "session": "S", "host": "h",
               "timestamp": "2026-07-14T10:00:00.000Z", "channel": "tool"}]

    assert "<unknown> bucket: 1" in audit_report(events, [], [])


def test_agent_events_missing_their_type_are_counted():
    # Pre-enrichment rows. Not a defect, but the count should fall as the
    # backfill reaches each host, so a rising number means something regressed.
    events = [{"skill": "humanizer", "session": "agent-old", "host": "h",
               "timestamp": "2026-06-01T00:00:00.000Z", "channel": "tool"}]

    assert "agent-thread events missing agent_type: 1" in audit_report(events, [], [])


def test_usage_rows_with_no_model_are_counted():
    row = dict(_usage("S", 10), model=None)

    assert "usage rows with no model: 1" in audit_report([], [row], [])


def test_an_entirely_empty_store_audits_without_crashing():
    report = audit_report([], [], [])

    assert "No comparable pairs" in report
    assert "- events: 0" in report
