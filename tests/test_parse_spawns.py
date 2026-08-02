"""
Unit tests for the subagent-spawn parser.

parse_spawns reads a PARENT session's records and emits one row per subagent it
spawned, joining the `Agent` tool_use (what was asked for) to its
`toolUseResult` (what happened).

Two things make this stream worth its own parser. Its coverage is partial, so it
enriches rather than defines: `agentType` and `totalTokens` are missing from
async results, and the agent's own transcript holds the authoritative tokens and
model. But `toolStats`, `status`, and the `agentId` link exist nowhere else, and
that link survives the agent file being pruned.

The privacy constraint is the sharpest one in the codebase. `toolUseResult`
carries `prompt`, `description`, and `content`, and in real transcripts those
hold client work verbatim. The store is a git repo that syncs between machines,
so a leak there is durable and hard to walk back (design §4).
"""

import json

from skill_usage import parse_spawns


def _spawn_tool_use(tool_use_id="toolu_1", subagent_type="Explore", model=None,
                    prompt="ACME-CLIENT migration plan",
                    description="ACME-CLIENT audit"):
    """The assistant record that requests a subagent.

    prompt and description default to sentinel-bearing text: every fixture here
    is therefore also a privacy fixture.
    """
    return {
        "type": "assistant",
        "timestamp": "2026-07-14T10:00:00.000Z",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": "Agent",
                "input": {
                    "subagent_type": subagent_type,
                    "model": model,
                    "prompt": prompt,
                    "description": description,
                },
            }],
        },
    }


def _spawn_result(tool_use_id="toolu_1", ts="2026-07-14T10:05:00.000Z", **fields):
    """The user record carrying the spawn's toolUseResult summary."""
    result = {"status": "completed", "agentId": "a0d980fade", "prompt": "ACME-CLIENT brief"}
    result.update(fields)
    return {
        "type": "user",
        "timestamp": ts,
        "toolUseResult": result,
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": "ACME-CLIENT summary text",
            }],
        },
    }


def test_a_full_result_populates_every_field():
    records = [
        _spawn_tool_use(model="claude-haiku-4-5-20251001"),
        _spawn_result(
            agentType="Explore",
            resolvedModel="claude-haiku-4-5-20251001",
            totalTokens=52553,
            totalDurationMs=123456,
            totalToolUseCount=45,
            toolStats={"readCount": 25, "bashCount": 20,
                       "linesAdded": 0, "linesRemoved": 0},
        ),
    ]

    (row,) = parse_spawns(records, session="sess1", host="laptop")

    assert row == {
        "session": "sess1",
        "host": "laptop",
        "timestamp": "2026-07-14T10:05:00.000Z",
        "agent_id": "a0d980fade",
        "agent_type": "Explore",
        "requested_model": "claude-haiku-4-5-20251001",
        "resolved_model": "claude-haiku-4-5-20251001",
        "status": "completed",
        "total_tokens": 52553,
        "duration_ms": 123456,
        "tool_use_count": 45,
        "tool_stats": {"readCount": 25, "bashCount": 20,
                       "linesAdded": 0, "linesRemoved": 0},
    }


def test_no_prompt_description_or_content_reaches_the_store():
    # The constraint that matters most. Every fixture helper seeds ACME-CLIENT
    # into prompt, description and tool_result content, so a single serialized
    # search over the output catches a leak from any of the three at once.
    records = [_spawn_tool_use(), _spawn_result(agentType="Explore")]

    rows = parse_spawns(records, session="sess1", host="laptop")

    assert "ACME-CLIENT" not in json.dumps(rows)
    assert not any(k in rows[0] for k in ("prompt", "description", "content"))


def test_a_minimal_result_degrades_to_nulls_instead_of_crashing():
    # Real async spawns carry only status and agentId out of the fields we read.
    # They're still real runs and must land, with absences recorded as null.
    (row,) = parse_spawns([_spawn_result()], session="sess1", host="laptop")

    assert row["agent_id"] == "a0d980fade"
    assert row["status"] == "completed"
    for field in ("agent_type", "requested_model", "resolved_model",
                  "total_tokens", "duration_ms", "tool_use_count", "tool_stats"):
        assert row[field] is None, field


def test_agent_type_falls_back_to_the_requested_subagent_type():
    # agentType is absent from async results. The spawning tool_use always named
    # one, and joining back to it is what restores full coverage on this field.
    records = [_spawn_tool_use(subagent_type="worker-standard"),
               _spawn_result()]

    (row,) = parse_spawns(records, session="sess1", host="laptop")

    assert row["agent_type"] == "worker-standard"


def test_the_results_own_agent_type_wins_over_the_request():
    # When the harness reports what actually ran, believe it over what was asked.
    records = [_spawn_tool_use(subagent_type="general-purpose"),
               _spawn_result(agentType="Explore")]

    (row,) = parse_spawns(records, session="sess1", host="laptop")

    assert row["agent_type"] == "Explore"


def test_the_legacy_task_tool_name_is_still_recognized():
    # "Task" is the pre-rename spelling. Fresh transcripts don't use it, but old
    # ones on disk do, and an unrecognized spawn is a run missing from the store.
    tool_use = _spawn_tool_use(subagent_type="Explore")
    tool_use["message"]["content"][0]["name"] = "Task"

    (row,) = parse_spawns([tool_use, _spawn_result()], session="s", host="h")

    assert row["agent_type"] == "Explore"


def test_a_result_seen_before_its_tool_use_still_joins():
    # Why the parser makes two passes. A partial or reordered file can present
    # the result first; a single forward pass would silently drop the request
    # side and null out requested_model.
    records = [_spawn_result(), _spawn_tool_use(model="claude-opus-5")]

    (row,) = parse_spawns(records, session="s", host="h")

    assert row["requested_model"] == "claude-opus-5"


def test_an_unjoinable_result_still_produces_a_row():
    # A spawn whose tool_use aged out of a truncated transcript is still a run
    # that happened. Record it with the request side null rather than dropping it.
    (row,) = parse_spawns([_spawn_result(tool_use_id="toolu_missing",
                                         agentType="Explore")],
                          session="s", host="h")

    assert row["agent_type"] == "Explore"
    assert row["requested_model"] is None


def test_records_without_an_agent_id_are_not_spawns():
    # Every tool result flows through here. Only the ones carrying agentId are
    # subagent runs; a Read or Bash result must not become a row.
    bash_result = {
        "type": "user",
        "timestamp": "2026-07-14T10:00:00.000Z",
        "toolUseResult": {"stdout": "hello", "stderr": ""},
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_9", "content": "hello"}]},
    }

    assert parse_spawns([bash_result], session="s", host="h") == []


def test_a_string_tool_use_result_is_tolerated():
    # toolUseResult isn't always a dict; plenty of tools return a bare string.
    # Membership-testing a string would raise, so the type check comes first.
    record = {"type": "user", "timestamp": "2026-07-14T10:00:00.000Z",
              "toolUseResult": "agentId was never here"}

    assert parse_spawns([record], session="s", host="h") == []


def test_two_spawns_in_one_session_produce_two_joined_rows():
    # The ordinary case: a session that fans out. Each row must keep its own
    # request side rather than inheriting the last one indexed.
    records = [
        _spawn_tool_use(tool_use_id="t1", subagent_type="Explore", model="m-1"),
        _spawn_tool_use(tool_use_id="t2", subagent_type="worker-craft", model="m-2"),
        _spawn_result(tool_use_id="t1", agentId="agent-1"),
        _spawn_result(tool_use_id="t2", agentId="agent-2"),
    ]

    rows = parse_spawns(records, session="s", host="h")

    assert [(r["agent_id"], r["agent_type"], r["requested_model"]) for r in rows] == [
        ("agent-1", "Explore", "m-1"),
        ("agent-2", "worker-craft", "m-2"),
    ]


def test_tool_stats_passes_through_verbatim():
    # The harness owns this counter map. Freezing its keys here would silently
    # drop new ones on a version bump, so it's stored whole.
    stats = {"readCount": 3, "someFutureCounter": 99}

    (row,) = parse_spawns([_spawn_result(toolStats=stats)], session="s", host="h")

    assert row["tool_stats"] == stats
