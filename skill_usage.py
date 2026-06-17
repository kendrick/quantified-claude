#!/usr/bin/env python3
"""
skill_usage.py

Builds a Map of Content (MOC) for your Claude Code skills, annotated with your
own actual usage — counted retroactively from the session transcripts already
on disk. No instrumentation, no waiting, no telemetry export required.

What it does:
  1. Walks ~/.claude/projects/**/*.jsonl  -> every Skill tool invocation, with
     timestamps, the project it happened in, and the skill name.
  2. Walks your skills directories       -> inventory of SKILL.md files (name +
     description from the YAML frontmatter).
  3. Joins the two and writes a markdown MOC sorted by frequency, including:
       - skills you own and use (with counts + last-used date)
       - skills you own but have NEVER used
       - skills you've used that aren't in your dir (built-ins, plugin skills)

Usage:
    python3 skill_usage.py
    python3 skill_usage.py --out ~/vault/40-MOCs/claude-skills.md
    python3 skill_usage.py --since 2026-01-01
    python3 skill_usage.py --json            # dump raw events instead of a MOC

Assumes Python 3.9+. Pure stdlib, no dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #

HOME = Path.home()
TRANSCRIPTS_ROOT = HOME / ".claude" / "projects"

# Where skills can live. ~/.claude/skills is the one you asked about; the others
# are included so "used but not in my dir" is accurate rather than noisy.
SKILL_DIRS = [
    HOME / ".claude" / "skills",                 # your personal skills
    HOME / ".claude" / "plugins",                # installed plugin skills (nested)
]

# Keys the Skill tool's input might use to name the skill. The schema isn't
# formally documented and has shifted across versions, so we try several and
# fall back to scanning the whole input blob.
SKILL_NAME_KEYS = ("command", "name", "skill", "skill_name", "skillName")

# The slash channel: when the user types /skillname, Claude Code records it as a
# user message whose content holds a <command-name>/skillname</command-name> tag.
# The optional leading slash is captured-and-discarded; the inner [^<]+ stops at
# the closing tag. We match anywhere in the string rather than anchoring, because
# the sibling <command-message>/<command-args> tags sometimes precede it.
COMMAND_NAME_RE = re.compile(r"<command-name>\s*/?([^<]+?)\s*</command-name>")


# --------------------------------------------------------------------------- #
# Transcript parsing
# --------------------------------------------------------------------------- #

def iter_jsonl(path: Path):
    """Yield parsed JSON objects from a .jsonl file, skipping bad lines."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def find_tool_uses(obj, target_name: str):
    """
    Recursively walk an arbitrary JSON structure and yield every dict that looks
    like a tool_use block for `target_name`. Defensive on purpose: the record
    shape varies by Claude Code version, so we don't assume a fixed path.
    """
    if isinstance(obj, dict):
        is_tool_use = obj.get("type") == "tool_use" or "tool_use" in str(obj.get("type", ""))
        if is_tool_use and obj.get("name") == target_name:
            yield obj
        for v in obj.values():
            yield from find_tool_uses(v, target_name)
    elif isinstance(obj, list):
        for item in obj:
            yield from find_tool_uses(item, target_name)


def extract_skill_name(tool_use: dict) -> str:
    """Pull the skill identifier out of a Skill tool_use block."""
    inp = tool_use.get("input", {}) or {}
    if isinstance(inp, dict):
        for key in SKILL_NAME_KEYS:
            val = inp.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        # last resort: a short string value somewhere in the input
        for val in inp.values():
            if isinstance(val, str) and 0 < len(val) < 120 and "\n" not in val:
                return val.strip()
    elif isinstance(inp, str) and inp.strip():
        return inp.strip()
    return "<unknown>"


def record_timestamp(record: dict) -> str | None:
    """Best-effort timestamp from a transcript record."""
    for key in ("timestamp", "ts", "createdAt", "created_at", "time"):
        val = record.get(key)
        if isinstance(val, str):
            return val
    return None


def parse_events(records, session: str, host: str, since: datetime | None = None):
    """
    Reduce one session's transcript records into usage events.

    This is the testable core of `collect`. It's deliberately decoupled from the
    filesystem: callers hand it already-parsed JSON records plus the session id
    and host (which the disk layout supplies), so the parsing logic can be
    exercised against fixture lines without staging a fake ~/.claude tree.

    Each event is the minimal tuple the events store needs:
        {skill, timestamp, session, host, channel}

    `project` is intentionally NOT recorded. The MOC counts by skill, never by
    project, so it was dead weight — and it happened to encode client names
    (the consultant-privacy leak). Dropping it deletes the liability outright
    rather than guarding it (design doc §4).
    """
    events = []
    for record in records:
        ts = record_timestamp(record)
        # channel="tool": the model called the Skill tool. find_tool_uses walks
        # the record defensively because the tool_use block's depth shifts across
        # Claude Code versions.
        for tu in find_tool_uses(record, "Skill"):
            events.append({
                "skill": extract_skill_name(tu),
                "timestamp": ts,
                "session": session,
                "host": host,
                "channel": "tool",
            })

        # channel="slash": the user typed /skillname. We only trust USER messages
        # with STRING content. That single rule rejects every false positive seen
        # in real data: assistant prose that mentions the tag arrives as a content
        # *list*, and tool_result echoes that quote transcript data are also lists.
        # So a literal "<command-name>" substring outside a user+str record is
        # always a mention, never an invocation.
        if record.get("type") == "user":
            content = record.get("message", {}).get("content")
            if isinstance(content, str):
                for name in COMMAND_NAME_RE.findall(content):
                    events.append({
                        "skill": name.strip(),
                        "timestamp": ts,
                        "session": session,
                        "host": host,
                        "channel": "slash",
                    })
    return events


def collect_events(since: datetime | None):
    """Return a list of {skill, project, timestamp, session} usage events."""
    events = []
    if not TRANSCRIPTS_ROOT.exists():
        print(f"  ! No transcripts found at {TRANSCRIPTS_ROOT}", file=sys.stderr)
        return events

    for jsonl in TRANSCRIPTS_ROOT.rglob("*.jsonl"):
        project = jsonl.parent.name  # encoded cwd; decoded below
        session = jsonl.stem
        for record in iter_jsonl(jsonl):
            ts = record_timestamp(record)
            for tu in find_tool_uses(record, "Skill"):
                if since and ts:
                    try:
                        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if when < since:
                            continue
                    except ValueError:
                        pass
                events.append({
                    "skill": extract_skill_name(tu),
                    "project": decode_project(project),
                    "timestamp": ts,
                    "session": session,
                })
    return events


def decode_project(encoded: str) -> str:
    """
    Claude Code encodes the project cwd into the directory name (slashes -> '-').
    We can't perfectly recover it, but the basename is the useful part.
    """
    parts = [p for p in encoded.split("-") if p]
    return parts[-1] if parts else encoded


# --------------------------------------------------------------------------- #
# Skill inventory
# --------------------------------------------------------------------------- #

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def parse_frontmatter(text: str) -> dict:
    """Minimal YAML-ish frontmatter parse (name + description only)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def collect_inventory():
    """Map skill-name -> {description, path} from every SKILL.md found."""
    inventory = {}
    for root in SKILL_DIRS:
        if not root.exists():
            continue
        for skill_md in root.rglob("SKILL.md"):
            text = skill_md.read_text(encoding="utf-8", errors="replace")
            fm = parse_frontmatter(text)
            name = fm.get("name") or skill_md.parent.name
            inventory[name] = {
                "description": fm.get("description", ""),
                "path": str(skill_md),
            }
    return inventory


# --------------------------------------------------------------------------- #
# MOC rendering
# --------------------------------------------------------------------------- #

def short(text: str, n: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def render_moc(events, inventory) -> str:
    counts = Counter(e["skill"] for e in events)
    last_used = {}
    for e in events:
        ts = e["timestamp"]
        if ts and (e["skill"] not in last_used or ts > last_used[e["skill"]]):
            last_used[e["skill"]] = ts

    used = set(counts)
    owned = set(inventory)
    owned_and_used = sorted(owned & used, key=lambda s: -counts[s])
    owned_unused = sorted(owned - used)
    used_not_owned = sorted(used - owned, key=lambda s: -counts[s])

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        "---",
        "title: Claude Code Skills — Usage MOC",
        f"generated: {now}",
        f"total_invocations: {sum(counts.values())}",
        "tags: [moc, claude-code, telemetry]",
        "---",
        "",
        "# Claude Code Skills — Usage MOC",
        "",
        f"*Generated {now} from {len(events)} skill invocations across "
        f"{len({e['session'] for e in events})} sessions.*",
        "",
        "## Used & owned",
        "",
        "| Skill | Uses | Last used | Description |",
        "|---|---:|---|---|",
    ]
    for s in owned_and_used:
        lu = (last_used.get(s, "") or "")[:10]
        desc = short(inventory[s]["description"])
        lines.append(f"| `{s}` | {counts[s]} | {lu} | {desc} |")

    lines += ["", "## Owned but never used", ""]
    if owned_unused:
        for s in owned_unused:
            lines.append(f"- `{s}` — {short(inventory[s]['description'])}")
    else:
        lines.append("*(none — you've exercised every skill in your dir)*")

    lines += ["", "## Used but not in your skills dir",
              "*(built-in skills, plugin skills, or renamed/removed ones)*", ""]
    if used_not_owned:
        for s in used_not_owned:
            lu = (last_used.get(s, "") or "")[:10]
            lines.append(f"- `{s}` — {counts[s]} uses (last {lu})")
    else:
        lines.append("*(none)*")

    lines += ["", "---", "*Built by skill-usage-moc.py*", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Build a Claude Code skills usage MOC.")
    ap.add_argument("--out", type=Path, default=Path("claude-skills-moc.md"),
                    help="Output markdown path (default: ./claude-skills-moc.md)")
    ap.add_argument("--since", type=str, default=None,
                    help="Only count usage on/after this date, e.g. 2026-01-01")
    ap.add_argument("--json", action="store_true",
                    help="Print raw usage events as JSON instead of writing a MOC")
    args = ap.parse_args()

    since = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        except ValueError:
            sys.exit(f"Bad --since date: {args.since!r} (use YYYY-MM-DD)")

    print(f"Scanning {TRANSCRIPTS_ROOT} ...", file=sys.stderr)
    events = collect_events(since)
    print(f"  found {len(events)} skill invocations", file=sys.stderr)

    if args.json:
        json.dump(events, sys.stdout, indent=2)
        print()
        return

    inventory = collect_inventory()
    print(f"  found {len(inventory)} skills in your dirs", file=sys.stderr)

    moc = render_moc(events, inventory)
    args.out.expanduser().write_text(moc, encoding="utf-8")
    print(f"Wrote MOC -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()