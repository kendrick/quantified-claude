"""
Tests for MOC rendering: the skill-name link formatter and the channel columns.

A MOC must live with the content it maps or its links go dead (design §2), so
the link format matters. --links relative is the portable default (works in any
editor and on GitHub); --links wikilink is for when the skills dir sits inside
an Obsidian vault.
"""

from pathlib import Path

from datetime import datetime, timezone

from skill_usage import format_skill_link, render_moc, run_render, write_store


def test_wikilink_format_for_an_owned_skill():
    inventory = {"humanizer": {"description": "", "path": "/skills/humanizer/SKILL.md"}}
    assert format_skill_link("humanizer", inventory, "wikilink", Path("/skills")) == "[[humanizer]]"


def test_relative_link_points_from_the_moc_to_the_skill_md(tmp_path):
    # The link is computed relative to where the MOC is written, so it resolves
    # no matter where the skills dir lives.
    skill_md = tmp_path / "skills" / "humanizer" / "SKILL.md"
    inventory = {"humanizer": {"description": "", "path": str(skill_md)}}

    link = format_skill_link("humanizer", inventory, "relative", tmp_path / "skills")

    assert link == "[humanizer](humanizer/SKILL.md)"


def test_unowned_skill_renders_as_a_plain_code_span():
    # Built-ins and plugin skills with no SKILL.md in our dirs have nothing to
    # link to, so they show as a code span rather than a dead link.
    assert format_skill_link("some-builtin", {}, "relative", Path("/skills")) == "`some-builtin`"


def _ev(skill, channel, ts="2026-06-17T00:00:00.000Z"):
    return {"skill": skill, "timestamp": ts, "session": "S", "host": "h", "channel": channel}


def test_moc_breaks_usage_out_into_tool_and_slash_columns():
    events = [
        _ev("humanizer", "tool"),
        _ev("humanizer", "tool"),
        _ev("speckit-plan", "slash"),
    ]
    inventory = {
        "humanizer": {"description": "humanize text", "path": "/skills/humanizer/SKILL.md"},
        "speckit-plan": {"description": "plan", "path": "/skills/speckit-plan/SKILL.md"},
    }

    moc = render_moc(events, inventory, links="wikilink", out_dir=Path("/skills"))

    assert "| Tool | Slash |" in moc
    # humanizer: 2 total, both tool
    assert "[[humanizer]] | 2 | 2 | 0 |" in moc
    # speckit-plan: 1 total, all slash
    assert "[[speckit-plan]] | 1 | 0 | 1 |" in moc


def test_run_render_writes_a_moc_built_from_the_unioned_store(tmp_path):
    data_dir = tmp_path / "data"
    write_store(data_dir / "events" / "laptop.jsonl", [_ev("humanizer", "tool")])
    out = tmp_path / "skills" / "skill-usage-moc.md"
    inventory = {"humanizer": {"description": "d", "path": str(tmp_path / "skills" / "humanizer" / "SKILL.md")}}

    run_render(data_dir, out, links="wikilink", inventory=inventory)

    assert out.exists()
    assert "[[humanizer]]" in out.read_text(encoding="utf-8")


def test_run_render_since_drops_events_before_the_cutoff(tmp_path):
    data_dir = tmp_path / "data"
    write_store(data_dir / "events" / "laptop.jsonl", [
        _ev("humanizer", "tool", ts="2026-06-01T00:00:00.000Z"),
        _ev("impeccable", "tool", ts="2026-06-20T00:00:00.000Z"),
    ])
    out = tmp_path / "skills" / "moc.md"
    inventory = {
        "humanizer": {"description": "", "path": str(tmp_path / "skills" / "humanizer" / "SKILL.md")},
        "impeccable": {"description": "", "path": str(tmp_path / "skills" / "impeccable" / "SKILL.md")},
    }

    run_render(data_dir, out, links="wikilink", since=datetime(2026, 6, 15, tzinfo=timezone.utc),
               inventory=inventory)

    text = out.read_text(encoding="utf-8")
    # impeccable (after the cutoff) is a used row; humanizer (before it) drops out
    # of the used table. It still appears under "Owned but never used" since it's
    # in the inventory — so assert specifically on the used-table row shape.
    assert "[[impeccable]] | 1 |" in text
    assert "[[humanizer]] |" not in text
