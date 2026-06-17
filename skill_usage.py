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

# Built-in CLI slash commands ride the same <command-name> rail as skills but
# aren't skills, so they'd otherwise show up as phantom MOC rows. This denylist
# is deliberately CONSERVATIVE: it lists only names that are always CLI built-ins
# and never skills. Notably absent are /review, /init, /security-review and the
# like — those ARE skills in this setup, so the safer filter for genuinely
# ambiguous names is render-time cross-reference against the skills inventory
# (§9), not this list. Kept as a frozenset for cheap membership tests.
BUILTIN_CLI_COMMANDS = frozenset({
    "add-dir", "agents", "bashes", "bug", "clear", "compact", "config", "cost",
    "doctor", "exit", "export", "help", "hooks", "ide", "login", "logout",
    "mcp", "memory", "model", "output-style", "permissions", "plugin",
    "pr-comments", "quit", "release-notes", "resume", "status", "statusline",
    "terminal-setup", "todos", "upgrade", "vim",
})


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


def _before_cutoff(ts: str | None, since: datetime) -> bool:
    """
    True only when `ts` is present, parseable, and strictly earlier than `since`.
    Deliberately lenient: a missing or unparseable timestamp is NEVER dropped —
    losing a real invocation to a format we failed to parse is worse than letting
    one slip past the --since filter. The "Z" -> "+00:00" swap is because
    fromisoformat didn't accept a bare Z until 3.11 and we target 3.9+.
    """
    if not ts:
        return False
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    return when < since


def parse_events(records, session: str, host: str, since: datetime | None = None,
                 builtins=BUILTIN_CLI_COMMANDS):
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
        # The cutoff is record-level: both channels share this timestamp, so one
        # check gates the whole record. Inclusive of the boundary day (only
        # strictly-earlier records are skipped).
        if since is not None and _before_cutoff(ts, since):
            continue
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
                    name = name.strip()
                    if name in builtins:
                        continue
                    events.append({
                        "skill": name,
                        "timestamp": ts,
                        "session": session,
                        "host": host,
                        "channel": "slash",
                    })
    return events


def run_collect(transcripts_root: Path, data_dir: Path, host: str,
                since: datetime | None = None) -> list:
    """
    The data flow of `collect`, with no git involved.

    Read this host's existing store, parse the transcripts currently on disk,
    overlay the re-read sessions onto the store, and write it back. Returns the
    merged events. cmd_collect wraps this with best-effort git pull/commit/push;
    keeping the git out here is what makes the whole pipeline unit-testable.
    """
    store_path = store_dir(data_dir) / f"{host}.jsonl"
    stored = read_store(store_path)
    fresh, found_sessions = collect_from_disk(transcripts_root, host, since)
    merged = merge_events(stored, fresh, found_sessions)
    write_store(store_path, merged)
    return merged


def collect_from_disk(transcripts_root: Path, host: str, since: datetime | None = None):
    """
    Walk a transcripts tree and reduce it to (events, found_sessions).

    Every *.jsonl under the root is one session, named by its stem (the session
    UUID). We record the session in `found_sessions` whether or not it yielded
    events — an eventless transcript is still "seen this run", and the overlay
    must treat it as authoritative rather than mistaking it for a pruned session
    whose history should be preserved.
    """
    fresh = []
    found_sessions = set()
    for jsonl in sorted(transcripts_root.rglob("*.jsonl")):
        session = jsonl.stem
        found_sessions.add(session)
        records = iter_jsonl(jsonl)
        fresh.extend(parse_events(records, session=session, host=host, since=since))
    return fresh, found_sessions


def read_store(path: Path) -> list:
    """
    Load an events store (events/<host>.jsonl) into a list of event dicts.

    A missing file is an empty store, not an error: the first collect on a new
    host has nothing to load, and render unions across hosts where some files may
    not exist yet. Reuses iter_jsonl so a torn final line (a collect killed
    mid-write) is skipped rather than fatal.
    """
    if not path.exists():
        return []
    return list(iter_jsonl(path))


def store_dir(data_dir: Path) -> Path:
    """The events/ subdir inside the private data repo. One file per host."""
    return data_dir / "events"


def union_stores(data_dir: Path) -> list:
    """
    Concatenate every host's store into the full cross-machine event set.

    render builds the MOC from this union, never from a single machine's file,
    which is why every machine's regenerated MOC reflects usage from all of them.
    Sorted by filename for deterministic output (stable diffs when rendered).
    """
    events = []
    for store in sorted(store_dir(data_dir).glob("*.jsonl")):
        events.extend(read_store(store))
    return events


def write_store(path: Path, events) -> None:
    """
    Write events to a store file as JSONL, creating the events/ tree if needed.

    One JSON object per line keeps the store diff-friendly (a new session is a
    block of added lines, not a rewrite of the whole file) and lets read_store
    skip a single corrupt line instead of losing the file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def merge_events(stored, fresh, found_sessions):
    """
    Overlay this run's freshly-parsed events onto the durable store.

    `found_sessions` is the set of session UUIDs actually present on disk this
    run. For those sessions the fresh parse is authoritative, so we drop their
    stored slice and substitute the fresh one — a wholesale replace, never an
    append. Two properties fall out of keying on the session:

      - Idempotent / self-healing. Re-reading a transcript replaces its slice
        with an identical one, so running collect twice equals running it once.
        There's no append seam to double-count across (the handoff's dedup fear
        only applied to a blind append).
      - Pruned sessions persist. A session in the store but no longer on disk is
        simply not in `found_sessions`, so its events are carried through
        untouched. That's what moves durability into the store and lets
        transcripts age out (§6).

    Fresh events are filtered to `found_sessions` defensively, so a caller can
    pass the full parse without a stray session leaking past the overlay.
    """
    kept = [e for e in stored if e["session"] not in found_sessions]
    overlaid = [e for e in fresh if e["session"] in found_sessions]
    return kept + overlaid


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