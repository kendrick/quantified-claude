"""
Unit tests for the session-stats parser.

parse_stats emits one row per transcript holding the counters the other three
streams discard: a per-tool census with error counts, compactions, interrupts,
API errors, and how many PRs the session opened.

Two of these counters are only correct because they refuse to pattern-match on
text, and both traps are live in real data on this machine:

  - "compact_boundary" appears inside tool_result content whenever a session
    reads a document that discusses compaction. Five such hits, zero real
    compactions. Only the structured system.subtype counts.
  - "[Request interrupted" appears in tool_result content for the same reason.
    Nineteen real markers in text blocks against four quoted ones.

This is the same discrimination the slash channel makes in parse_events, and
for the same reason: a substring is evidence of a mention, not of an event.

PRIVACY: pr-link records carry prUrl and prRepository, naming client repos. Only
the deduplicated count is stored (design §4).
"""

import json

from skill_usage import parse_stats


def _tool_use(name, tool_use_id, ts="2026-07-14T10:00:00.000Z"):
    return {
        "type": "assistant",
        "timestamp": ts,
        "version": "2.1.220",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tool_use_id, "name": name, "input": {}}]},
    }


def _tool_result(tool_use_id, is_error=False, content="ok",
                 ts="2026-07-14T10:00:01.000Z"):
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return {
        "type": "user",
        "timestamp": ts,
        "version": "2.1.220",
        "message": {"role": "user", "content": [block]},
    }


def _row(records, session="s", host="h", thread="main"):
    (row,) = parse_stats(records, session=session, host=host, thread=thread)
    return row


def test_a_tool_census_counts_calls_and_joins_errors_by_id():
    # ExitPlanMode's error count is effectively the number of rejected plans,
    # which is why the join has to be per-tool rather than a global error tally.
    records = [
        _tool_use("Read", "t1"), _tool_result("t1"),
        _tool_use("Bash", "t2"), _tool_result("t2", is_error=True),
        _tool_use("Bash", "t3"), _tool_result("t3"),
        _tool_use("ExitPlanMode", "t4"), _tool_result("t4", is_error=True),
    ]

    row = _row(records)

    assert row["tool_calls"] == {
        "Bash": {"calls": 2, "errors": 1},
        "ExitPlanMode": {"calls": 1, "errors": 1},
        "Read": {"calls": 1, "errors": 0},
    }


def test_mcp_tools_appear_in_the_census_without_special_casing():
    # MCP tools are named mcp__<server>__<tool>. They're ordinary tool_use blocks,
    # so the census picks them up for free, which is the point of a census.
    records = [_tool_use("mcp__gmail__search", "t1"), _tool_result("t1")]

    assert _row(records)["tool_calls"] == {"mcp__gmail__search": {"calls": 1, "errors": 0}}


def test_an_orphan_error_result_is_dropped_rather_than_invented():
    # A truncated transcript can hold a result whose tool_use is gone. There's no
    # tool name to charge the error to, and inventing an "<unknown>" bucket would
    # put a phantom row in the census.
    row = _row([_tool_result("vanished", is_error=True)])

    assert row["tool_calls"] == {}


def test_compactions_count_the_system_record_not_the_word():
    # The trap this parser exists to avoid. A tool_result that merely quotes
    # "compact_boundary" (reading a design doc, say) is a mention, not an event.
    real = {"type": "system", "subtype": "compact_boundary",
            "timestamp": "2026-07-14T10:00:00.000Z"}
    quoted = _tool_result("t1", content="the design doc mentions compact_boundary here")

    assert _row([real, quoted])["compactions"] == 1
    assert _row([quoted])["compactions"] == 0


def test_interrupts_count_text_blocks_not_quoted_transcript():
    # Same discrimination, different marker. The real one is written into a text
    # block; a session that reads a file discussing interrupts sees the identical
    # string arrive inside tool_result content.
    real = {
        "type": "user", "timestamp": "2026-07-14T10:00:00.000Z",
        "message": {"role": "user", "content": [
            {"type": "text", "text": "[Request interrupted by user]"}]},
    }
    quoted = _tool_result("t1", content="42\t[Request interrupted by user]\n43\t...")

    assert _row([real, quoted])["interrupts"] == 1
    assert _row([quoted])["interrupts"] == 0


def test_api_errors_are_counted_from_their_subtype():
    records = [
        {"type": "system", "subtype": "api_error", "timestamp": "2026-07-14T10:00:00.000Z"},
        {"type": "system", "subtype": "api_error", "timestamp": "2026-07-14T10:01:00.000Z"},
        {"type": "system", "subtype": "stop_hook_summary", "timestamp": "2026-07-14T10:02:00.000Z"},
    ]

    assert _row(records)["api_errors"] == 2


def test_the_same_pr_seen_many_times_counts_once():
    # The harness re-emits every PR on each subsequent turn. On this machine 381
    # raw records collapse to 17 unique, so the undeduped number runs 22x high.
    records = [
        {"type": "pr-link", "timestamp": "2026-07-14T10:00:00.000Z",
         "prNumber": 93, "prRepository": "kendrick/agent-guild",
         "prUrl": "https://github.com/kendrick/agent-guild/pull/93"}
        for _ in range(5)
    ]

    assert _row(records)["prs_created"] == 1


def test_distinct_prs_each_count():
    records = [
        {"type": "pr-link", "timestamp": "2026-07-14T10:00:00.000Z",
         "prNumber": n, "prRepository": "kendrick/agent-guild"}
        for n in (93, 94)
    ]

    assert _row(records)["prs_created"] == 2


def test_the_same_number_in_two_repos_counts_twice():
    # The identity is the pair. Keying on the number alone would merge PR #1 in
    # one repo with PR #1 in another.
    records = [
        {"type": "pr-link", "timestamp": "2026-07-14T10:00:00.000Z",
         "prNumber": 1, "prRepository": "kendrick/agent-guild"},
        {"type": "pr-link", "timestamp": "2026-07-14T10:00:00.000Z",
         "prNumber": 1, "prRepository": "kendrick/quantified-claude"},
    ]

    assert _row(records)["prs_created"] == 2


def test_no_pr_url_or_repository_string_reaches_the_row():
    # The store is a public-adjacent artifact that syncs between machines. A repo
    # name is exactly the kind of client-identifying string module 1 dropped
    # `project` to avoid.
    records = [{"type": "pr-link", "timestamp": "2026-07-14T10:00:00.000Z",
                "prNumber": 7, "prRepository": "ACME-CLIENT/secret-migration",
                "prUrl": "https://github.com/ACME-CLIENT/secret-migration/pull/7"}]

    rows = parse_stats(records, session="s", host="h")

    assert "ACME-CLIENT" not in json.dumps(rows)
    assert rows[0]["prs_created"] == 1


def test_cc_version_is_the_last_one_seen():
    # A session spanning an upgrade is attributed to the version it finished on,
    # which is the one whose parser behavior produced the final records.
    records = [_tool_use("Read", "t1"), _tool_use("Read", "t2")]
    records[0]["version"] = "2.1.219"
    records[1]["version"] = "2.1.220"

    assert _row(records)["cc_version"] == "2.1.220"


def test_a_transcript_with_nothing_interesting_still_yields_one_row():
    # One row per session file, always. A silent session is a real data point:
    # the overlay needs a row to replace, and a missing row would read as a
    # session that was never collected.
    row = _row([{"type": "user", "timestamp": "2026-07-14T10:00:00.000Z",
                 "message": {"role": "user", "content": "hello"}}])

    assert row["tool_calls"] == {}
    assert row["compactions"] == 0
    assert row["interrupts"] == 0
    assert row["api_errors"] == 0
    assert row["prs_created"] == 0
    assert row["session"] == "s" and row["thread"] == "main"


def test_an_empty_record_list_still_yields_a_row():
    row = _row([])

    assert row["timestamp"] is None
    assert row["cc_version"] is None
    assert row["tool_calls"] == {}


def test_the_tool_census_is_sorted_for_stable_diffs():
    # The store is a git repo, so key order decides whether a re-collect shows a
    # clean diff or a reshuffle.
    records = [_tool_use(name, f"t{i}") for i, name in enumerate(["Write", "Bash", "Read"])]

    assert list(_row(records)["tool_calls"]) == ["Bash", "Read", "Write"]
