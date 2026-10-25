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


def test_row_timestamp_is_the_latest_seen_within_that_hour():
    # One row now summarizes one hour of one model under one skill, so its
    # timestamp is the last message inside that hour. Field meanings are added
    # to, never repurposed (dataContracts), so `timestamp` keeps "latest seen".
    records = [
        _assistant(message_id="a", ts="2026-07-14T10:00:00.000Z"),
        _assistant(message_id="b", ts="2026-07-14T10:45:00.000Z"),
        _assistant(message_id="c", ts="2026-07-14T10:15:00.000Z"),
    ]

    (row,) = parse_usage(records, session="s", host="h")

    assert row["hour"] == "2026-07-14T10"
    assert row["timestamp"] == "2026-07-14T10:45:00.000Z"


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


def test_tokens_land_in_the_hour_they_were_spent():
    # Issue #12's reproduction. Before hour buckets, all 1,010 tokens landed on
    # the 13th because the one row was stamped at the session's last message,
    # and about a third of real token spend sat on the wrong day as a result.
    records = [
        _assistant(message_id="m1", ts="2026-09-10T09:00:00.000Z", tokens=(0, 1000, 0, 0)),
        _assistant(message_id="m2", ts="2026-09-13T17:00:00.000Z", tokens=(0, 10, 0, 0)),
    ]

    rows = parse_usage(records, session="s1", host="h")

    assert [(r["hour"], r["output_tokens"]) for r in rows] == [
        ("2026-09-10T09", 1000),
        ("2026-09-13T17", 10),
    ]


def test_splitting_by_hour_never_changes_the_totals():
    # Every reader of usage/ sums rows with +=, which is why this is a parser
    # change and not a rendering one. That only holds if the split is exact.
    records = [
        _assistant(model="claude-opus-5", skill="humanizer", message_id="a",
                   ts="2026-07-14T09:10:00.000Z", tokens=(1, 2, 3, 4)),
        _assistant(model="claude-opus-5", skill="humanizer", message_id="b",
                   ts="2026-07-14T09:50:00.000Z", tokens=(5, 6, 7, 8)),
        _assistant(model="claude-opus-5", skill="humanizer", message_id="c",
                   ts="2026-07-14T11:00:00.000Z", tokens=(9, 10, 11, 12)),
        _assistant(model="claude-haiku-4-5-20251001", skill=None, message_id="d",
                   ts="2026-07-15T02:00:00.000Z", tokens=(13, 14, 15, 16)),
    ]
    fields = ("messages", "input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens")

    rows = parse_usage(records, session="s", host="h")

    # Totals settled from the fixture, not from the parser.
    assert {f: sum(r[f] for r in rows) for f in fields} == {
        "messages": 4, "input_tokens": 28, "output_tokens": 32,
        "cache_creation_input_tokens": 36, "cache_read_input_tokens": 40,
    }
    assert len(rows) == 3


def test_messages_in_the_same_hour_share_one_row():
    # Buckets come from observed records, so a busy hour is one row, not one
    # per message. That's what keeps the store near 1.5x rather than the
    # per-message size antipatterns.md rules out.
    records = [
        _assistant(message_id="a", ts="2026-07-14T10:00:00.000Z"),
        _assistant(message_id="b", ts="2026-07-14T10:59:59.999Z"),
    ]

    (row,) = parse_usage(records, session="s", host="h")

    assert row["messages"] == 2


def test_an_unparseable_timestamp_keeps_its_tokens_in_a_null_hour():
    # Same lenient posture as --since: a timestamp we can't read never costs
    # us tokens we know were spent.
    records = [
        _assistant(message_id="a", ts="yesterday-ish", tokens=(0, 7, 0, 0)),
        _assistant(message_id="b", ts="2026-07-14T10:00:00.000Z", tokens=(0, 5, 0, 0)),
    ]

    rows = parse_usage(records, session="s", host="h")

    assert [(r["hour"], r["output_tokens"]) for r in rows] == [
        (None, 7),
        ("2026-07-14T10", 5),
    ]


def test_hour_is_normalized_to_utc():
    # Real transcripts write a bare Z, but an offset timestamp must still land
    # in the same hour bucket as its UTC equivalent or one hour splits in two.
    records = [
        _assistant(message_id="a", ts="2026-07-14T05:30:00.000-05:00"),
        _assistant(message_id="b", ts="2026-07-14T10:10:00.000Z"),
    ]

    (row,) = parse_usage(records, session="s", host="h")

    assert row["hour"] == "2026-07-14T10"
    assert row["messages"] == 2
