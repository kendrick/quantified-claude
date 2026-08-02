"""
Unit tests for the transcript -> events parser.

The parser is the heart of `collect`: it reduces a session's raw transcript
records into the small event dicts that get merged into the durable events
store. Per the design doc (§9, verified against real data), it MUST harvest two
distinct invocation channels and tag each event with which one it came from:

  - channel="tool"  -> Claude invoked the Skill tool (a tool_use block)
  - channel="slash" -> the user typed /skillname (a <command-name> tag)

It also has to say which side of the spawn boundary an event came from (§5).
Subagent transcripts have always been in the walk, so 39 of 363 historical tool
events were fired by a subagent and silently counted as main-thread usage.
`thread` (plus `agent_type` and `parent_session`) is what makes "which skills
does Claude reach for inside an Explore agent" answerable instead of averaged
away.

These fixtures use the real record shapes observed in
~/.claude/projects/**/*.jsonl, trimmed to the fields the parser reads.
"""

from datetime import datetime, timezone

from skill_usage import BUILTIN_CLI_COMMANDS, parse_events


def _agent_tool_use(skill, ts="2026-07-14T10:00:00.000Z", **extra):
    """Build a subagent-transcript record carrying one Skill tool_use.

    Agent files self-identify on every record: `sessionId` is the PARENT
    session's UUID, `attributionAgent` the subagent type, `isSidechain` the flag
    that marks the file as off the main thread.
    """
    record = {
        "type": "assistant",
        "timestamp": ts,
        "isSidechain": True,
        "sessionId": "f7a73a51-parent-uuid",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Skill", "input": {"skill": skill}}],
        },
    }
    record.update(extra)
    return record


def _slash(name, ts="2026-06-17T12:00:00.000Z"):
    """Build a user record carrying a single /name slash invocation."""
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": f"<command-name>/{name}</command-name>"},
    }


def test_tool_use_block_yields_one_tool_channel_event():
    # The shape Claude Code writes when the model calls the Skill tool: an
    # assistant message whose content array holds a tool_use block named
    # "Skill", with the skill id under input.skill.
    record = {
        "type": "assistant",
        "timestamp": "2026-06-17T10:00:00.000Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "Skill", "input": {"skill": "humanizer"}}
            ],
        },
    }

    events = parse_events([record], session="sess-abc", host="laptop")

    assert events == [
        {
            "skill": "humanizer",
            "timestamp": "2026-06-17T10:00:00.000Z",
            "session": "sess-abc",
            "host": "laptop",
            "channel": "tool",
            "thread": "main",
            "cc_version": None,
        }
    ]


def test_slash_command_in_user_message_yields_slash_channel_event():
    # When the user types /humanizer, Claude Code records it as a user message
    # whose string content carries a <command-name> tag (plus sibling
    # <command-message>/<command-args> tags we don't need). This is the bypass
    # path the reference parser missed entirely (§9) — on the spec-kit workflow
    # it was the bulk of real usage, e.g. 34 slash vs 0 tool for speckit-plan.
    record = {
        "type": "user",
        "timestamp": "2026-06-17T11:00:00.000Z",
        "message": {
            "role": "user",
            "content": (
                "<command-name>/humanizer</command-name>\n"
                "<command-args>polish this</command-args>"
            ),
        },
    }

    events = parse_events([record], session="sess-abc", host="laptop")

    assert events == [
        {
            "skill": "humanizer",
            "timestamp": "2026-06-17T11:00:00.000Z",
            "session": "sess-abc",
            "host": "laptop",
            "channel": "slash",
            "thread": "main",
            "cc_version": None,
        }
    ]


def test_command_name_mention_in_assistant_prose_is_not_counted():
    # The discrimination that keeps the slash count honest: only USER messages
    # with STRING content are real invocations. Assistant prose that merely
    # mentions the tag (list-shaped content) and tool_result echoes that quote
    # transcript data both contain the literal "<command-name>" substring but
    # are NOT invocations. Verified against real data: every assistant+list and
    # user+list match was a false positive.
    prose = {
        "type": "assistant",
        "timestamp": "2026-06-17T11:01:00.000Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "The <command-name> tag means the skill is loaded."}
            ],
        },
    }
    tool_echo = {
        "type": "user",
        "timestamp": "2026-06-17T11:02:00.000Z",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "content": "output quoting <command-name>/humanizer</command-name>"}
            ],
        },
    }

    assert parse_events([prose, tool_echo], session="s", host="h") == []


def test_builtin_cli_command_is_filtered_from_slash_channel():
    # /model, /compact, /mcp and friends ride the same <command-name> rail as a
    # real skill, but they're CLI built-ins, not skills (§9). Counting them would
    # pollute the MOC with rows for things that have no SKILL.md. The denylist is
    # injectable so this test pins the mechanism without depending on the exact
    # shipped contents.
    events = parse_events([_slash("model")], session="s", host="h", builtins={"model"})
    assert events == []


def test_real_skill_survives_when_a_different_command_is_denylisted():
    # Guard the other side: filtering /model must not swallow a genuine skill
    # invoked in the same run.
    events = parse_events([_slash("humanizer")], session="s", host="h", builtins={"model"})
    assert [e["skill"] for e in events] == ["humanizer"]


def test_default_denylist_covers_common_builtins():
    # The shipped default must actually catch the built-ins seen in real data,
    # so production `collect` filters them without the caller passing a denylist.
    for builtin in ("model", "compact", "mcp", "clear"):
        assert builtin in BUILTIN_CLI_COMMANDS


def test_plugin_namespaced_skill_name_is_preserved_verbatim():
    # Plugin skills carry a "plugin:skill" id (verified in real data, e.g.
    # superpowers:systematic-debugging). The colon-namespaced form is the skill's
    # true identity, so we keep it intact rather than splitting to the bare name —
    # two plugins could both ship a "systematic-debugging".
    tool_record = {
        "type": "assistant",
        "timestamp": "2026-06-17T13:00:00.000Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "Skill",
                 "input": {"skill": "superpowers:systematic-debugging"}}
            ],
        },
    }
    slash_record = _slash("superpowers:test-driven-development")

    events = parse_events([tool_record, slash_record], session="s", host="h")

    assert {e["skill"] for e in events} == {
        "superpowers:systematic-debugging",
        "superpowers:test-driven-development",
    }


def test_skill_tool_use_with_no_recognizable_name_falls_back_to_unknown():
    # A loud <unknown> bucket is a deliberate tripwire (design §3): if a Claude
    # Code version bump changes the input schema so the skill id stops extracting,
    # counts crater into <unknown> visibly instead of silently vanishing.
    record = {
        "type": "assistant",
        "timestamp": "2026-06-17T13:05:00.000Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Skill", "input": {}}],
        },
    }

    events = parse_events([record], session="s", host="h")

    assert [e["skill"] for e in events] == ["<unknown>"]


def test_since_drops_events_before_cutoff_and_keeps_the_boundary_day():
    # --since powers "usage in the last N days" views. The cutoff is inclusive:
    # an event whose timestamp equals `since` is kept, only strictly-earlier ones
    # are dropped. Filtering happens in the parser so both channels honor it.
    before = _slash("humanizer", ts="2026-06-10T00:00:00.000Z")
    on_cutoff = _slash("doc-coauthoring", ts="2026-06-15T00:00:00.000Z")
    after = _slash("impeccable", ts="2026-06-20T00:00:00.000Z")
    since = datetime(2026, 6, 15, tzinfo=timezone.utc)

    events = parse_events([before, on_cutoff, after], session="s", host="h", since=since)

    assert {e["skill"] for e in events} == {"doc-coauthoring", "impeccable"}


def test_agent_thread_event_carries_the_type_and_the_parent_session():
    # The payload the whole thread dimension exists for. Note what `session` is:
    # the agent FILE's stem, not the parent UUID. merge_events overlays on that
    # key, so two agent files sharing a parent would clobber each other if the
    # parent's UUID went there. It gets its own field instead (§5).
    events = parse_events(
        [_agent_tool_use("systematic-debugging", attributionAgent="Explore")],
        session="agent-a0d980fade", host="laptop", thread="agent",
    )

    assert events == [
        {
            "skill": "systematic-debugging",
            "timestamp": "2026-07-14T10:00:00.000Z",
            "session": "agent-a0d980fade",
            "host": "laptop",
            "channel": "tool",
            "thread": "agent",
            "cc_version": None,
            "agent_type": "Explore",
            "parent_session": "f7a73a51-parent-uuid",
        }
    ]


def test_main_thread_event_gains_thread_and_nothing_else():
    # The other half of the contract. Enrichment is agent-only: a session file
    # has no subagent type and no parent, and writing null keys for both would
    # bloat every row in the store to describe an absence.
    record = {
        "type": "assistant",
        "timestamp": "2026-07-14T10:00:00.000Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "humanizer"}}],
        },
    }

    (event,) = parse_events([record], session="sess-abc", host="laptop")

    assert event["thread"] == "main"
    assert "agent_type" not in event and "parent_session" not in event


def test_agent_file_with_no_attribution_yields_a_null_type_rather_than_crashing():
    # Coverage is 290/297 in real data; the misses are all 2.1.92-era files that
    # predate attributionAgent. Those events are still real usage, so null is the
    # honest answer. The field stays present so readers needn't special-case it.
    record = _agent_tool_use("humanizer")
    assert "attributionAgent" not in record

    (event,) = parse_events([record], session="agent-old", host="h", thread="agent")

    assert event["agent_type"] is None
    assert event["parent_session"] == "f7a73a51-parent-uuid"


def test_attribution_appearing_after_the_first_event_still_stamps_the_whole_file():
    # Why the stamp happens after the walk instead of inline. Both fields are
    # constant per file, but attributionAgent isn't guaranteed to ride the record
    # that carries the first skill call. Stamping inline would leave every event
    # before its first sighting unattributed.
    early = _agent_tool_use("humanizer", ts="2026-07-14T10:00:00.000Z")
    late = _agent_tool_use("impeccable", ts="2026-07-14T10:05:00.000Z",
                           attributionAgent="general-purpose")

    events = parse_events([early, late], session="agent-x", host="h", thread="agent")

    assert [e["skill"] for e in events] == ["humanizer", "impeccable"]
    assert all(e["agent_type"] == "general-purpose" for e in events)


def test_a_since_cutoff_cannot_cost_the_file_its_identity():
    # The enrichment scan runs ahead of the --since check on purpose. If the only
    # record carrying attributionAgent falls before the cutoff, the events that
    # survive it must still know which agent produced them.
    old = _agent_tool_use("humanizer", ts="2026-06-01T00:00:00.000Z",
                          attributionAgent="Explore")
    recent = _agent_tool_use("impeccable", ts="2026-07-20T00:00:00.000Z")
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)

    events = parse_events([old, recent], session="agent-x", host="h",
                          since=since, thread="agent")

    assert [e["skill"] for e in events] == ["impeccable"]
    assert events[0]["agent_type"] == "Explore"


def test_slash_events_on_the_agent_thread_are_enriched_too():
    # Enrichment is stamped per event, not per channel. A /command inside a
    # subagent is rare but the store shouldn't have a hole where its agent_type
    # should be.
    slash = _slash("humanizer")
    slash["sessionId"] = "parent-uuid"
    slash["attributionAgent"] = "worker-standard"

    (event,) = parse_events([slash], session="agent-x", host="h", thread="agent")

    assert event["channel"] == "slash"
    assert event["agent_type"] == "worker-standard"
    assert event["parent_session"] == "parent-uuid"
