#!/usr/bin/env python3
"""
skill_usage.py

Builds a Map of Content (MOC) for your Claude Code skills, annotated with your
own actual usage — counted retroactively from the session transcripts already
on disk. No instrumentation, no waiting, no telemetry export required.

Two subcommands, because usage rolls up across several machines (see the design
doc for why the split is inherent rather than incidental):

  collect  Parse the transcripts on this machine into usage events, merge them
           into events/<host>.jsonl in a private data repo (keyed by session
           UUID, so re-runs don't double-count), then git pull/commit/push.
           Running collect IS syncing.

  render   Union every host's events store, join it against your local skills
           inventory, and write the MOC. Auto-collects this machine first so the
           output is current the moment you generate it.

Usage:
    python3 skill_usage.py collect --data-dir ~/repos/quantified-claude-events
    python3 skill_usage.py render  --out ~/.claude/skills/skill-usage-moc.md
    python3 skill_usage.py render  --since 2026-01-01 --links wikilink
    python3 skill_usage.py render  --json     # dump the unioned events instead

The data repo is also read from $SKILL_MOC_DATA_DIR, so --data-dir is optional
once that's set. Assumes Python 3.9+. Pure stdlib, no dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from collections import Counter
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


def collect_inventory(skill_dirs=None):
    """
    Map skill-name -> {description, path} from every SKILL.md found.

    Walks with os.walk(followlinks=True) rather than Path.rglob because the
    personal skills under ~/.claude/skills are symlinks into a shared store
    (~/.agents/skills). rglob won't descend through a symlinked directory — and
    Python 3.13 made that the explicit default — so a plain rglob silently found
    zero personal skills. os.walk(followlinks=True) follows them and behaves the
    same across the 3.9+ range we target, where the rglob recurse_symlinks knob
    doesn't exist.
    """
    if skill_dirs is None:
        skill_dirs = SKILL_DIRS
    inventory = {}
    for root in skill_dirs:
        if not root.exists():
            continue
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=True):
            if "SKILL.md" not in filenames:
                continue
            skill_md = Path(dirpath) / "SKILL.md"
            fm = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
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


def format_skill_link(name: str, inventory: dict, links: str, out_dir: Path) -> str:
    """
    Render a skill name as a link to its SKILL.md, or a code span if we don't own
    it. Used-but-not-owned skills (built-ins, plugin skills not on disk) have no
    target, so linking them would just create dead links.

    `relative` links are computed from the MOC's own location so they resolve in
    any editor and on GitHub — the MOC has to ship next to the content it maps for
    this to hold (§2). `wikilink` is for when that content lives in an Obsidian
    vault, where [[name]] is the native cross-reference.
    """
    entry = inventory.get(name)
    if not entry:
        return f"`{name}`"
    if links == "wikilink":
        return f"[[{name}]]"
    rel = os.path.relpath(entry["path"], out_dir)
    return f"[{name}]({rel})"


def run_render(data_dir: Path, out_path: Path, links: str = "relative",
               since: datetime | None = None, inventory: dict | None = None) -> str:
    """
    The data flow of `render`, with no git: union every host's store, optionally
    drop events before --since, join against the skills inventory, and write the
    MOC. cmd_render layers a best-effort pull + auto-collect in front so the
    machine you're on is current before it renders.

    `inventory` is injectable so tests don't need a real ~/.claude/skills tree;
    in production it defaults to scanning SKILL_DIRS.
    """
    events = union_stores(data_dir)
    if since is not None:
        events = [e for e in events if not _before_cutoff(e.get("timestamp"), since)]
    if inventory is None:
        inventory = collect_inventory()
    moc = render_moc(events, inventory, links=links, out_dir=out_path.parent)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(moc, encoding="utf-8")
    return moc


def render_moc(events, inventory, links: str = "relative", out_dir: Path = Path(".")) -> str:
    counts = Counter(e["skill"] for e in events)
    # Per-channel tallies power the Tool/Slash split: how much of a skill's use is
    # Claude reaching for it (tool) vs you invoking it by hand (slash). .get()
    # tolerates an older event without a channel rather than KeyError-ing.
    tool_counts = Counter(e["skill"] for e in events if e.get("channel") == "tool")
    slash_counts = Counter(e["skill"] for e in events if e.get("channel") == "slash")

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

    def link(name):
        return format_skill_link(name, inventory, links, out_dir)

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
        "| Skill | Uses | Tool | Slash | Last used | Description |",
        "|---|---:|---:|---:|---|---|",
    ]
    for s in owned_and_used:
        lu = (last_used.get(s, "") or "")[:10]
        desc = short(inventory[s]["description"])
        lines.append(
            f"| {link(s)} | {counts[s]} | {tool_counts[s]} | {slash_counts[s]} | {lu} | {desc} |"
        )

    lines += ["", "## Owned but never used", ""]
    if owned_unused:
        for s in owned_unused:
            lines.append(f"- {link(s)} — {short(inventory[s]['description'])}")
    else:
        lines.append("*(none — you've exercised every skill in your dir)*")

    lines += ["", "## Used but not in your skills dir",
              "*(built-in skills, plugin skills, or renamed/removed ones)*", ""]
    if used_not_owned:
        for s in used_not_owned:
            lu = (last_used.get(s, "") or "")[:10]
            lines.append(f"- {link(s)} — {counts[s]} uses (last {lu})")
    else:
        lines.append("*(none)*")

    lines += ["", "---", "*Built by skill_usage.py*", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI glue: data-dir resolution, best-effort git, subcommands
# --------------------------------------------------------------------------- #

DEFAULT_OUT = Path("~/.claude/skills/skill-usage-moc.md")


def resolve_data_dir(arg: str | None) -> Path:
    """
    Locate the private events repo: --data-dir, then $SKILL_MOC_DATA_DIR.

    The public code carries zero knowledge of where private data lives, so this
    is required rather than defaulted — there's no sensible repo-relative guess
    that wouldn't risk writing telemetry into the wrong place.
    """
    if arg:
        return Path(arg).expanduser()
    env = os.environ.get("SKILL_MOC_DATA_DIR")
    if env:
        return Path(env).expanduser()
    sys.exit("No data dir. Pass --data-dir or set SKILL_MOC_DATA_DIR to the "
             "private events repo.")


def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        sys.exit(f"Bad --since date: {value!r} (use YYYY-MM-DD)")


def _git(data_dir: Path, *args: str) -> bool:
    """
    Run one git command in the data repo, best-effort.

    Sync is convenience, not correctness — the local store is already written by
    the time we get here. So a git failure (offline, no remote, nothing to
    commit) warns and returns False rather than aborting; the next collect picks
    up where this one left off because the merge is idempotent.
    """
    try:
        subprocess.run(["git", "-C", str(data_dir), *args],
                       check=True, capture_output=True, text=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        detail = (getattr(exc, "stderr", "") or str(exc)).strip()
        print(f"  ! git {' '.join(args)} skipped: {detail}", file=sys.stderr)
        return False


def git_pull(data_dir: Path) -> None:
    # --rebase because each host only ever writes its own events/<host>.jsonl, so
    # there's nothing to merge — a rebase onto the remote can't conflict.
    _git(data_dir, "pull", "--rebase")


def git_commit_push(data_dir: Path, host: str) -> None:
    _git(data_dir, "add", "events")
    # An empty commit (nothing changed) exits non-zero; best-effort swallows it.
    _git(data_dir, "commit", "-m", f"collect: update {host}")
    _git(data_dir, "push")


def cmd_collect(args) -> None:
    data_dir = resolve_data_dir(args.data_dir)
    host = args.host or socket.gethostname()
    git_pull(data_dir)
    merged = run_collect(TRANSCRIPTS_ROOT, data_dir, host)
    git_commit_push(data_dir, host)
    print(f"collect: {len(merged)} events in store for {host}", file=sys.stderr)


def cmd_render(args) -> None:
    data_dir = resolve_data_dir(args.data_dir)
    host = args.host or socket.gethostname()
    # render auto-collects first so the machine you're on is current the moment
    # you generate the MOC — no stale-by-one-run gap.
    git_pull(data_dir)
    run_collect(TRANSCRIPTS_ROOT, data_dir, host)
    git_commit_push(data_dir, host)

    if args.json:
        json.dump(union_stores(data_dir), sys.stdout, indent=2)
        print()
        return

    out = args.out.expanduser()
    run_render(data_dir, out, links=args.links, since=parse_since(args.since))
    print(f"render: wrote MOC -> {out}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(
        description="Quantify your Claude Code skill usage from session transcripts.")
    sub = ap.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("collect",
                        help="parse transcripts on disk, merge into the events store, sync")
    pc.add_argument("--data-dir", default=None, help="private events repo (or $SKILL_MOC_DATA_DIR)")
    pc.add_argument("--host", default=None, help="override the hostname stamped on events")
    pc.set_defaults(func=cmd_collect)

    pr = sub.add_parser("render",
                        help="union the events stores and write the MOC (auto-collects first)")
    pr.add_argument("--data-dir", default=None, help="private events repo (or $SKILL_MOC_DATA_DIR)")
    pr.add_argument("--host", default=None, help="override the hostname stamped on events")
    pr.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"MOC output path (default: {DEFAULT_OUT})")
    pr.add_argument("--since", default=None, help="only count usage on/after YYYY-MM-DD")
    pr.add_argument("--links", choices=["relative", "wikilink"], default="relative",
                    help="how skill names link to SKILL.md (default: relative)")
    pr.add_argument("--json", action="store_true",
                    help="dump the unioned events as JSON instead of writing a MOC")
    pr.set_defaults(func=cmd_render)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()