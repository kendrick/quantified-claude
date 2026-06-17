"""
Unit tests for the transcript -> events parser.

The parser is the heart of `collect`: it reduces a session's raw transcript
records into the small event dicts that get merged into the durable events
store. Per the design doc (§9, verified against real data), it MUST harvest two
distinct invocation channels and tag each event with which one it came from:

  - channel="tool"  -> Claude invoked the Skill tool (a tool_use block)
  - channel="slash" -> the user typed /skillname (a <command-name> tag)

These fixtures use the real record shapes observed in
~/.claude/projects/**/*.jsonl, trimmed to the fields the parser reads.
"""

from skill_usage import BUILTIN_CLI_COMMANDS, parse_events


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
