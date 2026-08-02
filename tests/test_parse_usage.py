"""
Unit tests for the transcript -> token-usage parser.

parse_usage turns assistant records into per-model, per-skill token aggregates.
The load-bearing behavior is deduplication: streaming and content-block splits
repeat one `message.id` across up to 24 consecutive records, each carrying an
IDENTICAL usage block. Summing them naively inflates every token number in the
report by that factor, and a token count nobody can trust is worse than no token
count at all (design §4).

These fixtures use the real record shapes observed in
~/.claude/projects/**/*.jsonl, trimmed to the fields the parser reads. Real
usage blocks also carry server_tool_use, service_tier and a per-iteration
breakdown; the parser reads only the four counters that sum to the total.
"""

from datetime import datetime, timezone

from skill_usage import parse_usage


def _assistant(model="claude-opus-5", skill=None, message_id="msg_1",
               ts="2026-07-14T10:00:00.000Z", tokens=(10, 20, 30, 40), **extra):
    """An assistant record carrying one usage block.

    `tokens` is (input, output, cache_creation, cache_read), matching the order
    of USAGE_TOKEN_KEYS.
    """
    record = {
        "type": "assistant",
        "timestamp": ts,
        "attributionSkill": skill,
        "message": {
            "role": "assistant",
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": tokens[0],
                "output_tokens": tokens[1],
                "cache_creation_input_tokens": tokens[2],
                "cache_read_input_tokens": tokens[3],
                "service_tier": "standard",
            },
        },
    }
    record.update(extra)
    return record


def test_a_repeated_message_id_is_counted_exactly_once():
    # The whole reason this parser dedups. One logical assistant turn, split
    # across 24 records that each repeat the same usage block verbatim.
    records = [_assistant(message_id="msg_dup") for _ in range(24)]

    (row,) = parse_usage(records, session="s", host="h")

    assert row["messages"] == 1
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 20
    assert row["cache_creation_input_tokens"] == 30
    assert row["cache_read_input_tokens"] == 40


def test_distinct_message_ids_each_count():
    # The other side of dedup: two genuinely different turns must both land.
    records = [_assistant(message_id="msg_1"), _assistant(message_id="msg_2")]

    (row,) = parse_usage(records, session="s", host="h")

    assert row["messages"] == 2
    assert row["input_tokens"] == 20


def test_rows_are_grouped_by_model_and_skill():
    # The grouping that makes "tokens by skill" and "models by thread" possible.
    # Same model under two skills, and a second model, is three rows.
    records = [
        _assistant(model="claude-opus-5", skill="humanizer", message_id="a"),
        _assistant(model="claude-opus-5", skill="impeccable", message_id="b"),
        _assistant(model="claude-haiku-4-5-20251001", skill="humanizer", message_id="c"),
    ]

    rows = parse_usage(records, session="s", host="h")

    assert [(r["model"], r["skill"]) for r in rows] == [
        ("claude-haiku-4-5-20251001", "humanizer"),
        ("claude-opus-5", "humanizer"),
        ("claude-opus-5", "impeccable"),
    ]
    assert all(r["messages"] == 1 for r in rows)


def test_rows_sort_stably_when_model_or_skill_is_null():
    # Most assistant turns aren't inside a skill, so a null skill is the common
    # case, not an edge one. Sorting has to survive it or every collect on a
    # normal transcript raises. Nulls sort first, which keeps diffs stable.
    records = [
        _assistant(model="claude-opus-5", skill="humanizer", message_id="a"),
        _assistant(model="claude-opus-5", skill=None, message_id="b"),
    ]

    rows = parse_usage(records, session="s", host="h")

    assert [r["skill"] for r in rows] == [None, "humanizer"]


def test_synthetic_model_is_excluded():
    # "<synthetic>" is Claude Code's own bookkeeping rather than a billed call,
    # so counting it would inflate totals with tokens nobody spent.
    records = [_assistant(model="<synthetic>", message_id="a"),
               _assistant(model="claude-opus-5", message_id="b")]

    rows = parse_usage(records, session="s", host="h")

    assert [r["model"] for r in rows] == ["claude-opus-5"]


def test_records_without_a_usage_block_are_skipped():
    # User turns, tool results and system records all flow through the same
    # list. Only records carrying usage are billable events.
    user = {"type": "user", "timestamp": "2026-07-14T10:00:00.000Z",
            "message": {"role": "user", "content": "hello"}}
    no_message = {"type": "system", "timestamp": "2026-07-14T10:00:00.000Z"}

    assert parse_usage([user, no_message], session="s", host="h") == []


def test_a_record_with_no_message_id_is_still_counted():
    # An id-less record can't participate in dedup. Counting it risks a double
    # count on a malformed record; dropping it silently loses usage we know
    # happened, which is the worse failure for a trust tool.
    record = _assistant(message_id=None)
    del record["message"]["id"]

    (row,) = parse_usage([record], session="s", host="h")

    assert row["messages"] == 1


def test_missing_token_counters_default_to_zero():
    # Older records omit the cache counters entirely. A missing counter is zero
    # spend, not a crash, and not a null that breaks summing downstream.
    record = _assistant()
    del record["message"]["usage"]["cache_read_input_tokens"]

    (row,) = parse_usage([record], session="s", host="h")

    assert row["cache_read_input_tokens"] == 0
    assert row["input_tokens"] == 10


def test_row_timestamp_is_the_latest_seen_for_that_group():
    # One row summarizes many messages, so its timestamp answers "when was this
    # model last used for this skill" rather than when the group started.
    records = [
        _assistant(message_id="a", ts="2026-07-14T10:00:00.000Z"),
        _assistant(message_id="b", ts="2026-07-14T18:30:00.000Z"),
        _assistant(message_id="c", ts="2026-07-14T12:00:00.000Z"),
    ]

    (row,) = parse_usage(records, session="s", host="h")

    assert row["timestamp"] == "2026-07-14T18:30:00.000Z"


def test_since_drops_records_before_the_cutoff():
    # --since is applied in the parser so both streams honor one cutoff.
    records = [
        _assistant(message_id="old", ts="2026-06-01T00:00:00.000Z"),
        _assistant(message_id="new", ts="2026-07-20T00:00:00.000Z"),
    ]
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)

    (row,) = parse_usage(records, session="s", host="h", since=since)

    assert row["messages"] == 1
    assert row["timestamp"] == "2026-07-20T00:00:00.000Z"


def test_agent_thread_rows_carry_the_type_and_parent_session():
    # Same enrichment contract as the events stream, so a token row can be
    # joined back to the agent run and the session that spawned it.
    record = _assistant(message_id="a", attributionAgent="Explore",
                        sessionId="parent-uuid")

    (row,) = parse_usage([record], session="agent-x", host="h", thread="agent")

    assert row["thread"] == "agent"
    assert row["agent_type"] == "Explore"
    assert row["parent_session"] == "parent-uuid"
    assert row["session"] == "agent-x"


def test_main_thread_rows_gain_thread_and_nothing_else():
    # Enrichment stays agent-only. A session file has no subagent type and no
    # parent, and null keys on every row would bloat the store to describe an
    # absence.
    (row,) = parse_usage([_assistant()], session="s", host="h")

    assert row["thread"] == "main"
    assert "agent_type" not in row and "parent_session" not in row
