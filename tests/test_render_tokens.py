"""
Tests for the MOC's Tokens column and its session accounting.

The MOC stays a skills map and gains exactly one number. Tokens belong here
rather than only in the report because the MOC is the artifact that already
regenerates daily, so it's where a number gets seen.

Two things have to hold for the column to be trustworthy. Tokens join to skills
through the same canonicalization the counts use, or a namespaced
attributionSkill would strand its tokens on a row nobody owns. And a store
written before the usage stream existed has to render a dash rather than raise,
because that store is most of the history on any machine that has been
collecting since module 1.
"""

from datetime import datetime, timezone
from pathlib import Path

from skill_usage import event_thread, fmt_tokens, render_moc, run_render, write_store


def _ev(skill, channel="tool", session="S", ts="2026-06-17T00:00:00.000Z", **extra):
    event = {"skill": skill, "timestamp": ts, "session": session,
             "host": "h", "channel": channel}
    event.update(extra)
    return event


def _usage(skill, tokens=(10, 20, 30, 40), session="S", ts="2026-06-17T00:00:00.000Z"):
    """A usage row. `tokens` is (input, output, cache_creation, cache_read)."""
    return {
        "session": session, "host": "h", "thread": "main",
        "model": "claude-opus-5", "skill": skill, "messages": 1,
        "input_tokens": tokens[0], "output_tokens": tokens[1],
        "cache_creation_input_tokens": tokens[2],
        "cache_read_input_tokens": tokens[3],
        "timestamp": ts, "cc_version": "2.1.220",
    }


INVENTORY = {"humanizer": {"description": "polish prose",
                           "path": "/skills/humanizer/SKILL.md"}}


def _row_for(moc, skill="humanizer"):
    for line in moc.splitlines():
        if line.startswith("|") and skill in line and "Skill |" not in line:
            return [c.strip() for c in line.strip("|").split("|")]
    raise AssertionError(f"no table row for {skill} in:\n{moc}")


def test_the_table_gains_a_tokens_column():
    moc = render_moc([_ev("humanizer")], INVENTORY, usage_rows=[_usage("humanizer")],
                     links="wikilink", out_dir=Path("/skills"))

    assert "| Skill | Uses | Tool | Slash | Tokens | Last used | Description |" in moc


def test_the_token_cell_sums_all_four_buckets():
    # 10 + 20 + 30 + 40 = 100. The four-bucket total is the identity the design
    # verified against toolUseResult.totalTokens, so the column agrees with what
    # the agents stream reports for the same work.
    moc = render_moc([_ev("humanizer")], INVENTORY, usage_rows=[_usage("humanizer")],
                     links="wikilink", out_dir=Path("/skills"))

    assert _row_for(moc)[4] == "100"


def test_rows_for_one_skill_across_sessions_and_models_add_up():
    usage = [_usage("humanizer", (1000, 0, 0, 0), session="S1"),
             _usage("humanizer", (2000, 0, 0, 0), session="S2"),
             _usage("humanizer", (500, 0, 0, 0), session="S2")]

    moc = render_moc([_ev("humanizer")], INVENTORY, usage_rows=usage,
                     links="wikilink", out_dir=Path("/skills"))

    assert _row_for(moc)[4] == "3.5k"


def test_a_namespaced_usage_row_lands_on_the_bare_owned_row():
    # attributionSkill carries the plugin:skill id while the inventory is keyed
    # by the bare frontmatter name. Without the canonicalization these tokens
    # would vanish from the owned row entirely.
    usage = [_usage("superpowers:humanizer", (1000, 0, 0, 0))]

    moc = render_moc([_ev("humanizer")], INVENTORY, usage_rows=usage,
                     links="wikilink", out_dir=Path("/skills"))

    assert _row_for(moc)[4] == "1.0k"


def test_a_skill_with_no_usage_rows_shows_a_dash():
    # Not a zero. Nobody measured this skill at zero tokens; there's simply
    # nothing recorded for it, which is a different claim.
    moc = render_moc([_ev("humanizer")], INVENTORY, usage_rows=[],
                     links="wikilink", out_dir=Path("/skills"))

    assert _row_for(moc)[4] == "—"


def test_usage_rows_with_a_null_skill_are_ignored():
    # Most assistant turns spend tokens without any skill attributed. Those rows
    # are the majority of the usage store and must not land on a skill row.
    usage = [_usage(None, (999999, 0, 0, 0)), _usage("humanizer", (100, 0, 0, 0))]

    moc = render_moc([_ev("humanizer")], INVENTORY, usage_rows=usage,
                     links="wikilink", out_dir=Path("/skills"))

    assert _row_for(moc)[4] == "100"


def test_omitting_usage_rows_entirely_still_renders():
    # The signature default. A store predating the usage stream renders a column
    # of dashes rather than raising.
    moc = render_moc([_ev("humanizer")], INVENTORY, links="wikilink",
                     out_dir=Path("/skills"))

    assert _row_for(moc)[4] == "—"


def test_the_channel_columns_are_unchanged_by_the_new_one():
    # Guard against the Tokens column shifting the cells beside it.
    events = [_ev("humanizer", "tool"), _ev("humanizer", "tool"), _ev("humanizer", "slash")]

    row = _row_for(render_moc(events, INVENTORY, usage_rows=[_usage("humanizer")],
                              links="wikilink", out_dir=Path("/skills")))

    assert row[1] == "3" and row[2] == "2" and row[3] == "1"


def test_the_summary_splits_sessions_from_agent_runs():
    # An agent transcript is a session file, so the old count reported more
    # "sessions" than conversations you actually had.
    events = [_ev("humanizer", session="S1", thread="main"),
              _ev("humanizer", session="S2", thread="main"),
              _ev("humanizer", session="agent-x", thread="agent")]

    moc = render_moc(events, INVENTORY, links="wikilink", out_dir=Path("/skills"))

    assert "across 2 sessions + 1 agent runs" in moc


def test_the_summary_omits_agent_runs_when_there_are_none():
    moc = render_moc([_ev("humanizer", session="S1")], INVENTORY,
                     links="wikilink", out_dir=Path("/skills"))

    assert "across 1 sessions.*" in moc
    assert "agent runs" not in moc


def test_a_pre_enrichment_event_is_placed_by_its_session_stem():
    # Events collected before thread-tagging carry no `thread`, and once their
    # transcript ages off disk it can't be re-derived. The stem prefix is the
    # same tell the collector classifies on.
    assert event_thread({"session": "agent-abc"}) == "agent"
    assert event_thread({"session": "4a8928a3-2845"}) == "main"
    # An explicit field always wins over the inference.
    assert event_thread({"session": "agent-abc", "thread": "main"}) == "main"


def test_pre_enrichment_events_still_split_in_the_summary():
    events = [_ev("humanizer", session="S1"), _ev("humanizer", session="agent-old")]

    moc = render_moc(events, INVENTORY, links="wikilink", out_dir=Path("/skills"))

    assert "across 1 sessions + 1 agent runs" in moc


def test_run_render_reads_the_usage_store_beside_the_events_store(tmp_path):
    data_dir = tmp_path / "data"
    write_store(data_dir / "events" / "laptop.jsonl", [_ev("humanizer")])
    write_store(data_dir / "usage" / "laptop.jsonl", [_usage("humanizer", (1234, 0, 0, 0))])

    moc = run_render(data_dir, tmp_path / "out" / "moc.md", links="wikilink",
                     inventory=INVENTORY)

    assert _row_for(moc)[4] == "1.2k"


def test_run_render_survives_a_data_dir_with_no_usage_directory(tmp_path):
    # Exactly the shape of a store that has only ever run module 1.
    data_dir = tmp_path / "data"
    write_store(data_dir / "events" / "laptop.jsonl", [_ev("humanizer")])

    moc = run_render(data_dir, tmp_path / "out" / "moc.md", links="wikilink",
                     inventory=INVENTORY)

    assert _row_for(moc)[4] == "—"


def test_since_filters_usage_rows_through_the_same_cutoff(tmp_path):
    data_dir = tmp_path / "data"
    write_store(data_dir / "events" / "laptop.jsonl",
                [_ev("humanizer", ts="2026-06-20T00:00:00.000Z")])
    write_store(data_dir / "usage" / "laptop.jsonl", [
        _usage("humanizer", (1000, 0, 0, 0), ts="2026-06-01T00:00:00.000Z"),
        _usage("humanizer", (25, 0, 0, 0), ts="2026-06-20T00:00:00.000Z"),
    ])

    moc = run_render(data_dir, tmp_path / "out" / "moc.md", links="wikilink",
                     since=datetime(2026, 6, 15, tzinfo=timezone.utc),
                     inventory=INVENTORY)

    assert _row_for(moc)[4] == "25"


def test_fmt_tokens_scales_by_magnitude():
    assert fmt_tokens(999) == "999"
    assert fmt_tokens(1234) == "1.2k"
    assert fmt_tokens(52553) == "53k"
    assert fmt_tokens(4_100_000) == "4.1M"
    assert fmt_tokens(1_286_165_420) == "1.3B"


def test_fmt_tokens_treats_zero_and_absent_as_unmeasured():
    assert fmt_tokens(0) == "—"
    assert fmt_tokens(None) == "—"
