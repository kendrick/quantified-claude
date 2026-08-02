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
once that's set. $SKILL_MOC_HOST does the same for --host, which matters on any
machine whose hostname isn't stable. Assumes Python 3.9+. Pure stdlib, no
dependencies.
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

# The four token counters, named exactly as message.usage names them, so
# verifying a number against a transcript is a grep rather than a translation.
# usage carries more (server_tool_use, service_tier, a per-iteration breakdown),
# but these four are the ones that sum to a run's total: the design doc verified
# totalTokens == input + output + cache_creation + cache_read (§3).
USAGE_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

# Spawning a subagent is an "Agent" tool_use. "Task" is the pre-rename spelling
# and no longer appears in fresh transcripts, but old ones on disk still carry
# it, and a spawn we fail to recognize is a run missing from the store.
AGENT_TOOL_NAMES = ("Agent", "Task")

# Claude Code writes this literal into a text block when a turn is cut short.
# It has to be matched inside `text` blocks only. The same string turns up inside
# tool_result content whenever a session reads a file that discusses it (4 such
# hits against 19 real ones on this machine), which is the same false positive
# the slash-channel filter guards against.
INTERRUPT_MARKER = "[Request interrupted"


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


def find_tool_uses(obj, target_name):
    """
    Recursively walk an arbitrary JSON structure and yield every dict that looks
    like a tool_use block for `target_name`. Defensive on purpose: the record
    shape varies by Claude Code version, so we don't assume a fixed path.

    `target_name` is one name or a collection of them. The collection form is
    for tools that have been renamed across versions, where old transcripts
    still hold the old spelling and both have to keep counting.
    """
    names = {target_name} if isinstance(target_name, str) else set(target_name)
    yield from _walk_tool_uses(obj, names)


def _walk_tool_uses(obj, names):
    if isinstance(obj, dict):
        is_tool_use = obj.get("type") == "tool_use" or "tool_use" in str(obj.get("type", ""))
        if is_tool_use and obj.get("name") in names:
            yield obj
        for v in obj.values():
            yield from _walk_tool_uses(v, names)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_tool_uses(item, names)


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
                 builtins=BUILTIN_CLI_COMMANDS, thread: str = "main"):
    """
    Reduce one session's transcript records into usage events.

    This is the testable core of `collect`. It's deliberately decoupled from the
    filesystem: callers hand it already-parsed JSON records plus the session id
    and host (which the disk layout supplies), so the parsing logic can be
    exercised against fixture lines without staging a fake ~/.claude tree.

    Each event is the minimal tuple the events store needs:
        {skill, timestamp, session, host, channel, thread}
    and agent-thread events carry {agent_type, parent_session} on top.

    `thread` says which side of the spawn boundary these records came from.
    collect_from_disk has been parsing subagent transcripts all along — 39 of
    363 historical tool events came from inside one — and folding them silently
    into the main-thread counts throws away the answer to "which skills does
    Claude reach for inside an Explore agent versus on the main thread" (§5).

    `project` is intentionally NOT recorded. The MOC counts by skill, never by
    project, so it was dead weight — and it happened to encode client names
    (the consultant-privacy leak). Dropping it deletes the liability outright
    rather than guarding it (design doc §4).
    """
    events = []
    agent_type = None
    parent_session = None
    cc_version = None
    for record in records:
        # Stamped so a parser regression is traceable to the Claude Code release
        # that caused it. Last one seen wins: a session spanning an upgrade is
        # attributed to the version it finished on.
        version = record.get("version")
        if isinstance(version, str):
            cc_version = version
        # Both fields are constant across an agent file, but attributionAgent
        # can first appear on a record *after* an early skill event, so scan for
        # them and stamp every event once the file is done. This runs ahead of
        # the --since check on purpose: a cutoff that skips the opening records
        # must not also cost us the file's identity.
        if thread == "agent":
            if agent_type is None:
                agent_type = record.get("attributionAgent")
            if parent_session is None:
                parent_session = record.get("sessionId")

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
                "thread": thread,
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
                        "thread": thread,
                    })

    # Stamped after the walk rather than inline, so an attributionAgent that
    # shows up late still reaches the events that preceded it. A 2.1.92-era file
    # has no attributionAgent at all; null is the honest answer there, not a
    # guess, and it keeps the field present so readers don't special-case it.
    for event in events:
        event["cc_version"] = cc_version
        if thread == "agent":
            event["agent_type"] = agent_type
            event["parent_session"] = parent_session
    return events


def parse_usage(records, session: str, host: str, thread: str = "main",
                since: datetime | None = None):
    """
    Reduce one session's transcript records into per-model, per-skill token rows.

    Assistant records carry both `message.model` and `message.usage`, and
    `attributionSkill` rides on exactly those records, so tokens-per-skill and
    tokens-per-model fall straight out of what's already on disk.

    The one thing that makes or breaks the count is deduplication. Streaming and
    content-block splits mean a single `message.id` can appear on up to 24
    consecutive records, each repeating an identical usage block. Summing
    naively inflates every number by that factor, so the first sighting of an id
    wins and the rest are skipped.

    Rows are aggregates per (model, skill), not one row per message. The
    session-keyed overlay recomputes a session's whole slice on every re-parse,
    so aggregating early costs no auditability while keeping the store small and
    its diffs readable (design doc §4).
    """
    agent_type = None
    parent_session = None
    seen_message_ids = set()
    buckets = {}
    cc_version = None

    for record in records:
        # Same enrichment scan as parse_events, and ahead of the cutoff for the
        # same reason: a --since that skips the opening records mustn't cost the
        # file its identity.
        if thread == "agent":
            if agent_type is None:
                agent_type = record.get("attributionAgent")
            if parent_session is None:
                parent_session = record.get("sessionId")
        version = record.get("version")
        if isinstance(version, str):
            cc_version = version

        ts = record_timestamp(record)
        if since is not None and _before_cutoff(ts, since):
            continue

        message = record.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue

        # "<synthetic>" is Claude Code's own bookkeeping, not a billed call.
        model = message.get("model")
        if model == "<synthetic>":
            continue

        message_id = message.get("id")
        if message_id is not None:
            if message_id in seen_message_ids:
                continue
            seen_message_ids.add(message_id)
        # An id-less record can't be deduped, so it's counted. Double-counting a
        # rare malformed record beats dropping usage we know happened.

        key = (model, record.get("attributionSkill"))
        bucket = buckets.get(key)
        if bucket is None:
            bucket = buckets[key] = dict.fromkeys(USAGE_TOKEN_KEYS, 0)
            bucket["messages"] = 0
            bucket["timestamp"] = None
        bucket["messages"] += 1
        for field in USAGE_TOKEN_KEYS:
            value = usage.get(field)
            bucket[field] += value if isinstance(value, int) else 0
        # Lexicographic max works because these are ISO-8601 in a fixed format.
        if ts is not None and (bucket["timestamp"] is None or ts > bucket["timestamp"]):
            bucket["timestamp"] = ts

    rows = []
    # None sorts against str in 3.9 only if we coerce, and a null skill is the
    # common case (most assistant turns aren't inside a skill).
    for (model, skill) in sorted(buckets, key=lambda k: (k[0] or "", k[1] or "")):
        bucket = buckets[(model, skill)]
        row = {
            "session": session,
            "host": host,
            "thread": thread,
            "model": model,
            "skill": skill,
            "messages": bucket["messages"],
        }
        row.update({field: bucket[field] for field in USAGE_TOKEN_KEYS})
        row["timestamp"] = bucket["timestamp"]
        row["cc_version"] = cc_version
        if thread == "agent":
            row["agent_type"] = agent_type
            row["parent_session"] = parent_session
        rows.append(row)
    return rows


def _walk_blocks(obj):
    """Yield every dict carrying a `type`, at any depth in a record.

    Same defensive posture as find_tool_uses: block depth has moved across
    Claude Code versions, so the census walks rather than indexing a fixed path.
    """
    if isinstance(obj, dict):
        if "type" in obj:
            yield obj
        for value in obj.values():
            yield from _walk_blocks(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_blocks(item)


def parse_stats(records, session: str, host: str, thread: str = "main"):
    """
    Reduce one transcript into a single row of session-level counters.

    Everything here is signal the other three streams throw away: which tools ran
    and how often they errored (MCP tools included, and an ExitPlanMode error is
    effectively a rejected plan), how often a session compacted or hit an API
    error, how often you interrupted it, and how many PRs it opened.

    No `since` parameter, unlike the other parsers. These are whole-file
    counters, and a cutoff would silently turn "this session ran 45 tools" into
    a partial number wearing a total's clothing.

    Returns a list holding exactly one row, so the caller extends a stream with
    it exactly like the others.

    PRIVACY: pr-link records carry `prUrl` and `prRepository`, which name client
    repositories. Only the deduplicated COUNT is stored (§4).

    `prs_created` dedups WITHIN a session, so summing it across rows is not the
    number of distinct PRs: one PR worked across two sessions contributes to
    both. On this machine that's 18 summed against 17 actually distinct. Render
    and audit should say "PRs worked on" rather than claim a unique total, which
    the store can no longer reconstruct once the repo and number are dropped.
    """
    tool_names = {}
    for record in records:
        for block in _walk_blocks(record):
            if block.get("type") == "tool_use" and block.get("id"):
                tool_names[block["id"]] = block.get("name") or "<unknown>"

    tool_calls = {}
    compactions = interrupts = api_errors = 0
    pull_requests = set()
    cc_version = None
    latest_ts = None

    for record in records:
        version = record.get("version")
        if isinstance(version, str):
            cc_version = version
        ts = record_timestamp(record)
        if ts is not None and (latest_ts is None or ts > latest_ts):
            latest_ts = ts

        record_type = record.get("type")
        if record_type == "system":
            subtype = record.get("subtype")
            # Matched on the structured field, never on a substring. Real
            # transcripts quote "compact_boundary" inside tool_result content
            # whenever a session reads a doc about it, and a text search counts
            # those as compactions that never happened.
            if subtype == "compact_boundary":
                compactions += 1
            elif subtype == "api_error":
                api_errors += 1
        elif record_type == "pr-link":
            # The harness re-emits a PR on every subsequent turn, so the raw
            # count runs ~22x high. The pair is the identity; neither half of it
            # is ever stored.
            pull_requests.add((record.get("prRepository"), record.get("prNumber")))

        for block in _walk_blocks(record):
            block_type = block.get("type")
            if block_type == "tool_use":
                name = block.get("name") or "<unknown>"
                tool_calls.setdefault(name, {"calls": 0, "errors": 0})["calls"] += 1
            elif block_type == "tool_result" and block.get("is_error"):
                # An orphan result (its tool_use aged out of a truncated file)
                # has no name to charge the error to, so it's dropped rather
                # than invented.
                name = tool_names.get(block.get("tool_use_id"))
                if name is not None:
                    tool_calls.setdefault(name, {"calls": 0, "errors": 0})["errors"] += 1
            elif block_type == "text" and INTERRUPT_MARKER in (block.get("text") or ""):
                interrupts += 1

    return [{
        "session": session,
        "host": host,
        "thread": thread,
        "timestamp": latest_ts,
        "tool_calls": dict(sorted(tool_calls.items())),
        "compactions": compactions,
        "interrupts": interrupts,
        "api_errors": api_errors,
        "prs_created": len(pull_requests),
        "cc_version": cc_version,
    }]


def _result_tool_use_id(record: dict) -> str | None:
    """The tool_use a tool_result record is answering, or None."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("tool_use_id"):
                return block["tool_use_id"]
    return None


def parse_spawns(records, session: str, host: str, since: datetime | None = None):
    """
    Reduce one PARENT session's records into one row per subagent it spawned.

    The parent records each spawn twice: an `Agent` tool_use carrying what was
    requested, and a result carrying `toolUseResult` with what happened. Two
    passes, because the result can be read before its tool_use in a partial or
    reordered file, and an unjoined row would silently lose `requested_model`.

    Coverage here is partial by nature (`agentType` and `totalTokens` are absent
    on roughly a seventh of real results, all async spawns), so this stream is an
    enrichment-and-fallback layer rather than ground truth. The agent's own file
    holds the authoritative tokens and model. What lives ONLY here is `toolStats`
    (lines added and removed, per-tool counts), `status`, and the `agentId` link
    that ties a run to its spawning session even after the agent file is pruned.

    PRIVACY: `toolUseResult` also carries `prompt`, `description`, and `content`,
    and in real transcripts those routinely hold client context. None of them may
    ever reach a row. Everything copied out is named explicitly below for exactly
    that reason: a field is stored because it was chosen, never because it
    happened to be in the source dict (§4).
    """
    requested = {}
    for record in records:
        for tool_use in find_tool_uses(record, AGENT_TOOL_NAMES):
            tool_use_id = tool_use.get("id")
            if tool_use_id is None:
                continue
            spawn_input = tool_use.get("input")
            if not isinstance(spawn_input, dict):
                spawn_input = {}
            requested[tool_use_id] = {
                "subagent_type": spawn_input.get("subagent_type"),
                "model": spawn_input.get("model"),
            }

    rows = []
    cc_version = None
    for record in records:
        version = record.get("version")
        if isinstance(version, str):
            cc_version = version
        result = record.get("toolUseResult")
        if not isinstance(result, dict) or "agentId" not in result:
            continue
        ts = record_timestamp(record)
        if since is not None and _before_cutoff(ts, since):
            continue

        spawn = requested.get(_result_tool_use_id(record), {})
        rows.append({
            "session": session,
            "host": host,
            "timestamp": ts,
            "agent_id": result.get("agentId"),
            # The fallback is what restores full coverage: agentType is missing
            # from async results, but the spawning tool_use always asked for one.
            "agent_type": result.get("agentType") or spawn.get("subagent_type"),
            "requested_model": spawn.get("model"),
            "resolved_model": result.get("resolvedModel"),
            "status": result.get("status"),
            "total_tokens": result.get("totalTokens"),
            "duration_ms": result.get("totalDurationMs"),
            "tool_use_count": result.get("totalToolUseCount"),
            # Passed through verbatim: it's a small counter map the harness owns,
            # and freezing its keys here would drop new ones on a version bump.
            "tool_stats": result.get("toolStats"),
            "cc_version": cc_version,
        })
    return rows


def run_collect(transcripts_root: Path, data_dir: Path, host: str,
                since: datetime | None = None) -> list:
    """
    The data flow of `collect`, with no git involved.

    Read this host's existing stores, parse the transcripts currently on disk,
    overlay the re-read sessions onto each store, and write them back. Returns
    the merged events. cmd_collect wraps this with best-effort git
    pull/commit/push; keeping the git out here is what makes the whole pipeline
    unit-testable.

    Every stream overlays identically because they share a merge key: the
    session. One `found_sessions` therefore governs all of them, which is what
    keeps a pruned session's tokens alive alongside its skill events.
    """
    (fresh_events, fresh_usage, fresh_spawns, fresh_stats,
     found_sessions) = collect_from_disk(transcripts_root, host, since)
    merged = overlay_store(store_dir(data_dir, "events") / f"{host}.jsonl",
                           fresh_events, found_sessions)
    overlay_store(store_dir(data_dir, "usage") / f"{host}.jsonl",
                  fresh_usage, found_sessions)
    overlay_store(store_dir(data_dir, "agents") / f"{host}.jsonl",
                  fresh_spawns, found_sessions)
    overlay_store(store_dir(data_dir, "stats") / f"{host}.jsonl",
                  fresh_stats, found_sessions)
    return merged


def overlay_store(path: Path, fresh, found_sessions) -> list:
    """Read one store, overlay this run's rows onto it, write it back."""
    merged = merge_events(read_store(path), fresh, found_sessions)
    write_store(path, merged)
    return merged


def collect_from_disk(transcripts_root: Path, host: str, since: datetime | None = None):
    """
    Walk a transcripts tree and reduce it to one list per stream, plus the
    set of sessions seen.

    Every *.jsonl under the root is one session, named by its stem (the session
    UUID). We record the session in `found_sessions` whether or not it yielded
    events — an eventless transcript is still "seen this run", and the overlay
    must treat it as authoritative rather than mistaking it for a pruned session
    whose history should be preserved.

    The rglob also picks up subagent transcripts at
    <session>/subagents/agent-<id>.jsonl, which it always has. They're tagged
    rather than filtered: a skill call inside an Explore agent is real usage, it
    just belongs to a different thread (§5).
    """
    fresh_events = []
    fresh_usage = []
    fresh_spawns = []
    fresh_stats = []
    found_sessions = set()
    for jsonl in sorted(transcripts_root.rglob("*.jsonl")):
        session = jsonl.stem
        found_sessions.add(session)
        # Two independent tells, either one sufficient. The directory is the
        # real signal; the stem prefix keeps the classification working if a
        # future layout moves these files but keeps naming them agent-<id>.
        # Main-thread files are named by UUID, so the prefix can't false-positive.
        is_agent = jsonl.parent.name == "subagents" or jsonl.stem.startswith("agent-")
        thread = "agent" if is_agent else "main"
        # Materialized once and handed to both parsers. Two small pure functions
        # over one list read far better than one fused loop doing both jobs, and
        # the file is read from disk only once either way.
        records = list(iter_jsonl(jsonl))
        fresh_events.extend(parse_events(records, session=session, host=host,
                                         since=since, thread=thread))
        fresh_usage.extend(parse_usage(records, session=session, host=host,
                                       thread=thread, since=since))
        fresh_stats.extend(parse_stats(records, session=session, host=host,
                                       thread=thread))
        # Spawns are recorded by the PARENT, so an agent file has none to give.
        if not is_agent:
            fresh_spawns.extend(parse_spawns(records, session=session, host=host,
                                             since=since))
    return fresh_events, fresh_usage, fresh_spawns, fresh_stats, found_sessions


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


def store_dir(data_dir: Path, name: str = "events") -> Path:
    """One stream's subdir inside the private data repo. One file per host."""
    return data_dir / name


def union_stores(data_dir: Path, name: str = "events") -> list:
    """
    Concatenate every host's file for one stream into the cross-machine set.

    render builds the MOC from this union, never from a single machine's file,
    which is why every machine's regenerated MOC reflects usage from all of them.
    Sorted by filename for deterministic output (stable diffs when rendered).
    A stream a host has never written is simply absent, and reads as empty.
    """
    rows = []
    for store in sorted(store_dir(data_dir, name).glob("*.jsonl")):
        rows.extend(read_store(store))
    return rows


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


BLOCK_SCALAR_MARKERS = {"|", ">", "|-", ">-", "|+", ">+"}


def parse_frontmatter(text: str) -> dict:
    """
    Minimal YAML-ish frontmatter parse (we only care about name + description).

    Handles block scalars, because skill descriptions are routinely written as
    `description: |` or `description: >` with the text on the following indented
    lines. A line-at-a-time key:value split kept only the `|`/`>` marker and
    dropped the body, so those skills showed blank descriptions in the MOC. When
    we hit a marker we gather the indented continuation lines instead. Both block
    styles fold to one spaced string — the MOC collapses whitespace anyway, so
    there's no value in preserving literal newlines. A top-level key is one with
    no leading indentation; indented lines are continuations, never new keys,
    which also stops a colon inside the body from being misread as a key.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out = {}
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or ":" not in line or line[:1] in (" ", "\t"):
            i += 1
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val in BLOCK_SCALAR_MARKERS:
            block = []
            i += 1
            while i < len(lines) and (not lines[i].strip() or lines[i][:1] in (" ", "\t")):
                block.append(lines[i].strip())
                i += 1
            out[key] = " ".join(part for part in block if part)
        else:
            out[key] = val.strip('"').strip("'")
            i += 1
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


def fmt_tokens(n) -> str:
    """
    Render a token count for a table cell: 1234 -> 1.2k, 52553 -> 53k, 4.1e6 -> 4.1M.

    Token counts span six orders of magnitude across skills, so raw integers make
    the column unreadable and unsortable by eye. One decimal below 10 of a unit
    keeps small numbers honest without giving huge ones false precision. Zero and
    absent both render as a dash: a skill with no usage rows hasn't been measured
    at zero, it simply has nothing recorded.
    """
    if not n:
        return "—"
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= limit:
            scaled = n / limit
            return f"{scaled:.1f}{suffix}" if scaled < 10 else f"{scaled:.0f}{suffix}"
    return str(int(n))


def event_thread(event) -> str:
    """
    Which thread an event came from, inferred when the field predates module 2.

    Events collected before thread-tagging carry no `thread`, and once their
    transcript ages off disk that can never be re-derived. The `agent-` stem is
    the same tell collect_from_disk classifies on, so an old row still lands on
    the right side of the split rather than silently counting as main.
    """
    thread = event.get("thread")
    if thread:
        return thread
    return "agent" if str(event.get("session", "")).startswith("agent-") else "main"


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
               since: datetime | None = None, inventory: dict | None = None,
               report_path: Path | None = None) -> str:
    """
    The data flow of `render`, with no git: union every host's store, optionally
    drop events before --since, join against the skills inventory, and write the
    MOC. cmd_render layers a best-effort pull + auto-collect in front so the
    machine you're on is current before it renders.

    `inventory` is injectable so tests don't need a real ~/.claude/skills tree;
    in production it defaults to scanning SKILL_DIRS.
    """
    events = union_stores(data_dir)
    usage_rows = union_stores(data_dir, "usage")
    if since is not None:
        events = [e for e in events if not _before_cutoff(e.get("timestamp"), since)]
        # Same cutoff, same helper. A usage row's timestamp is the latest message
        # in its group, so --since keeps a group that was still active on the
        # boundary rather than splitting an aggregate nobody can split.
        usage_rows = [r for r in usage_rows
                      if not _before_cutoff(r.get("timestamp"), since)]
    if inventory is None:
        inventory = collect_inventory()
    moc = render_moc(events, inventory, usage_rows=usage_rows, links=links,
                     out_dir=out_path.parent)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(moc, encoding="utf-8")

    # The report is a second local artifact, written only when asked for so the
    # existing render tests keep asserting about one file.
    if report_path is not None:
        spawn_rows = union_stores(data_dir, "agents")
        if since is not None:
            spawn_rows = [r for r in spawn_rows
                          if not _before_cutoff(r.get("timestamp"), since)]
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_report(usage_rows, spawn_rows, events),
                               encoding="utf-8")
    return moc


def canonical_skill(name: str, inventory: dict) -> str:
    """
    Map a used skill name to the inventory key it belongs to.

    Events keep the plugin:skill id the Skill tool emits (superpowers:brainstorm),
    but the inventory is keyed by the bare frontmatter name (brainstorming). So a
    namespaced id that doesn't match exactly is retried against its bare suffix;
    if that's owned, it canonicalizes to the bare key. A name with no owning
    SKILL.md is returned unchanged so it still shows up, just as unowned. Events
    stay namespaced on disk — only this render-time join normalizes.
    """
    if name in inventory:
        return name
    if ":" in name:
        bare = name.split(":", 1)[1]
        if bare in inventory:
            return bare
    return name


def render_moc(events, inventory, usage_rows=None, links: str = "relative",
               out_dir: Path = Path(".")) -> str:
    """
    Build the MOC: a skills map annotated with how much you actually use each one.

    `usage_rows` is optional so a store predating the usage stream still renders,
    with a column of dashes rather than an error. The MOC stays a skills map and
    gains exactly one number; everything richer lives in the separate report.
    """
    def key_of(event):
        return canonical_skill(event["skill"], inventory)

    counts = Counter(key_of(e) for e in events)

    # Tokens join to skills through the same canonicalization the counts use, so
    # a namespaced attributionSkill lands on the bare owned row. This is the
    # four-bucket total, matching the identity the design doc verified against
    # toolUseResult.totalTokens (§3), which means cache reads dominate it.
    tokens = Counter()
    for row in usage_rows or []:
        skill = row.get("skill")
        if not skill:
            continue
        tokens[canonical_skill(skill, inventory)] += sum(
            row.get(field) or 0 for field in USAGE_TOKEN_KEYS)
    # Per-channel tallies power the Tool/Slash split: how much of a skill's use is
    # Claude reaching for it (tool) vs you invoking it by hand (slash). .get()
    # tolerates an older event without a channel rather than KeyError-ing.
    tool_counts = Counter(key_of(e) for e in events if e.get("channel") == "tool")
    slash_counts = Counter(key_of(e) for e in events if e.get("channel") == "slash")

    last_used = {}
    for e in events:
        skill = key_of(e)
        ts = e["timestamp"]
        if ts and (skill not in last_used or ts > last_used[skill]):
            last_used[skill] = ts

    used = set(counts)
    owned = set(inventory)
    owned_and_used = sorted(owned & used, key=lambda s: -counts[s])
    owned_unused = sorted(owned - used)
    used_not_owned = sorted(used - owned, key=lambda s: -counts[s])

    def link(name):
        return format_skill_link(name, inventory, links, out_dir)

    # An agent transcript is a session file, so the old count folded subagent
    # runs in with the sessions you actually started and reported a number
    # bigger than the conversations you had. Splitting them says what happened.
    sessions = {e["session"] for e in events if event_thread(e) == "main"}
    agent_runs = {e["session"] for e in events if event_thread(e) == "agent"}
    summary = f"{len(sessions)} sessions"
    if agent_runs:
        summary += f" + {len(agent_runs)} agent runs"

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
        f"{summary}.*",
        "",
        "## Used & Owned",
        "",
        "| Skill | Uses | Tool | Slash | Tokens | Last used | Description |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for s in owned_and_used:
        lu = (last_used.get(s, "") or "")[:10]
        desc = short(inventory[s]["description"])
        lines.append(
            f"| {link(s)} | {counts[s]} | {tool_counts[s]} | {slash_counts[s]} | "
            f"{fmt_tokens(tokens.get(s))} | {lu} | {desc} |"
        )

    lines += ["", "## Owned but Never Used", ""]
    if owned_unused:
        for s in owned_unused:
            lines.append(f"- {link(s)} — {short(inventory[s]['description'])}")
    else:
        lines.append("*(none — you've exercised every skill in your dir)*")

    lines += ["", "## Used but Not in Your Skills Dir",
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
# Report rendering
# --------------------------------------------------------------------------- #

def short_model(model) -> str:
    """
    claude-haiku-4-5-20251001 -> haiku-4-5. Render time only.

    The store keeps the raw model string, because that's what the transcript
    said and a dated id is the only way to tell two releases of one model apart
    later. Shortening is presentation, so it happens here and never on disk.
    """
    if not model:
        return "<unknown>"
    name = model[len("claude-"):] if model.startswith("claude-") else model
    return re.sub(r"-\d{8}$", "", name)


def _median(values):
    """Median of a list, or None when empty. Even counts average the middle pair."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _row_tokens(row) -> int:
    """The four-bucket total for one usage row."""
    return sum(row.get(field) or 0 for field in USAGE_TOKEN_KEYS)


def render_models_by_thread(usage_rows) -> list:
    """
    Per model, messages and tokens split across the spawn boundary.

    The section that pays for the module. The main thread and its subagents don't
    run the same mix, and until both sides are counted separately that difference
    averages away into a single per-model number that describes neither.
    """
    lines = ["## Models × Thread", ""]
    if not usage_rows:
        return lines + ["*(no usage rows yet)*", ""]

    totals = {}
    for row in usage_rows:
        model = short_model(row.get("model"))
        thread = row.get("thread") or "main"
        bucket = totals.setdefault(model, {"main": [0, 0], "agent": [0, 0]})
        side = bucket.get(thread)
        if side is None:
            continue
        side[0] += row.get("messages") or 0
        side[1] += _row_tokens(row)

    lines += ["| Model | Main msgs | Main tokens | Agent msgs | Agent tokens | Total |",
              "|---|---:|---:|---:|---:|---:|"]
    for model in sorted(totals, key=lambda m: -sum(s[1] for s in totals[m].values())):
        main, agent = totals[model]["main"], totals[model]["agent"]
        lines.append(
            f"| {model} | {main[0]} | {fmt_tokens(main[1])} | {agent[0]} | "
            f"{fmt_tokens(agent[1])} | {fmt_tokens(main[1] + agent[1])} |")
    return lines + [""]


def render_agent_types(spawn_rows, usage_rows=None) -> list:
    """
    Per subagent type: how often it runs, how often it finishes, what it costs.

    Tokens come from the agent's OWN usage rows, summed across the run, and fall
    back to the spawn row's `total_tokens` only when no usage rows survive. That
    ordering is the opposite of what the module 2 plan called for, and it's
    deliberate: `toolUseResult.totalTokens` is the LAST message's usage, so it
    measures the context the run ended at rather than what it consumed. On one
    57-message run that's 109,592 against 4,431,358. Summing the former across
    spawns adds up ending context sizes, which answers no question anyone has.

    Completion is measured over RESOLVED runs only. An `async_launched` spawn is
    one whose parent recorded the launch and never saw the outcome, so counting
    it as not-completed turns "we didn't look" into "it failed". On this machine
    that's the difference between general-purpose reading as 5% and as 100%:
    16 of its 17 spawns were fire-and-forget. The async count gets its own
    column instead, where it's information rather than a false negative.
    """
    lines = ["## Agent Types", ""]
    if not spawn_rows:
        return lines + ["*(no spawn rows yet)*", ""]

    # agent-<id> is the agent file's stem, which is how its usage rows are keyed.
    by_agent_session = {}
    for row in usage_rows or []:
        session = row.get("session")
        if session:
            by_agent_session[session] = by_agent_session.get(session, 0) + _row_tokens(row)

    types = {}
    for spawn in spawn_rows:
        entry = types.setdefault(spawn.get("agent_type") or "<unknown>",
                                 {"spawns": 0, "completed": 0, "async": 0,
                                  "tokens": [], "added": 0, "removed": 0})
        entry["spawns"] += 1
        status = spawn.get("status")
        if status == "async_launched":
            entry["async"] += 1
        elif status == "completed":
            entry["completed"] += 1
        tokens = by_agent_session.get(f"agent-{spawn.get('agent_id')}")
        if not tokens:
            tokens = spawn.get("total_tokens")
        if tokens:
            entry["tokens"].append(tokens)
        stats = spawn.get("tool_stats") or {}
        entry["added"] += stats.get("linesAdded") or 0
        entry["removed"] += stats.get("linesRemoved") or 0

    lines += ["| Agent type | Spawns | Async | Completed | Median tokens | "
              "Total tokens | Lines +/- |",
              "|---|---:|---:|---:|---:|---:|---|"]
    for name in sorted(types, key=lambda n: -types[n]["spawns"]):
        e = types[name]
        resolved = e["spawns"] - e["async"]
        # A type whose every spawn was fire-and-forget has no completion rate to
        # report. A dash says that; 0% would say they all failed.
        rate = f"{100 * e['completed'] // resolved}%" if resolved else "—"
        lines.append(
            f"| {name} | {e['spawns']} | {e['async']} | {rate} | "
            f"{fmt_tokens(_median(e['tokens']))} | "
            f"{fmt_tokens(sum(e['tokens']))} | +{e['added']} / -{e['removed']} |")
    return lines + [""]


def render_tokens_by_skill(usage_rows, top_n: int = 15) -> list:
    """
    The most expensive skills, with the model mix each one ran on.

    Skills and models are many-to-many: one skill runs on whatever model the
    thread it fired in was using, so the mix column is where that shows up. Skill
    ids stay exactly as `attributionSkill` wrote them, namespace included. The
    MOC canonicalizes because it joins against an inventory; this section has no
    inventory to join to, so normalizing here would invent a claim.
    """
    lines = ["## Tokens by Skill", ""]
    if not usage_rows:
        return lines + ["*(no usage rows yet)*", ""]

    by_skill = {}
    for row in usage_rows:
        skill = row.get("skill")
        if not skill:
            continue
        entry = by_skill.setdefault(skill, {"tokens": 0, "messages": 0,
                                            "models": Counter()})
        tokens = _row_tokens(row)
        entry["tokens"] += tokens
        entry["messages"] += row.get("messages") or 0
        entry["models"][short_model(row.get("model"))] += tokens

    if not by_skill:
        return lines + ["*(no usage rows carry a skill attribution)*", ""]

    lines += ["| Skill | Tokens | Messages | Model mix |", "|---|---:|---:|---|"]
    ranked = sorted(by_skill, key=lambda s: -by_skill[s]["tokens"])[:top_n]
    for skill in ranked:
        entry = by_skill[skill]
        total = entry["tokens"] or 1
        mix = ", ".join(f"{model} {round(100 * tokens / total)}%"
                        for model, tokens in entry["models"].most_common(3))
        lines.append(f"| `{skill}` | {fmt_tokens(entry['tokens'])} | "
                     f"{entry['messages']} | {mix} |")
    if len(by_skill) > top_n:
        lines.append(f"")
        lines.append(f"*({len(by_skill) - top_n} more skills not shown.)*")
    return lines + [""]


def render_cache_economics(usage_rows) -> list:
    """
    Per model: what you paid fresh, what you paid to cache, what you re-read.

    The question this answers is how much of the spend is re-reading context you
    already paid to store. A high read-to-creation ratio means the cache is doing
    its job; a low one means you're re-paying to build it. Cache reads dominate
    the raw totals everywhere else in this report, so this is the section that
    says whether that's good news.
    """
    lines = ["## Cache Economics", ""]
    if not usage_rows:
        return lines + ["*(no usage rows yet)*", ""]

    totals = {}
    for row in usage_rows:
        entry = totals.setdefault(short_model(row.get("model")),
                                  dict.fromkeys(USAGE_TOKEN_KEYS, 0))
        for field in USAGE_TOKEN_KEYS:
            entry[field] += row.get(field) or 0

    lines += ["| Model | Fresh input | Cache creation | Cache read | Read : creation |",
              "|---|---:|---:|---:|---:|"]
    for model in sorted(totals, key=lambda m: -totals[m]["cache_read_input_tokens"]):
        entry = totals[model]
        created = entry["cache_creation_input_tokens"]
        read = entry["cache_read_input_tokens"]
        # No creation means nothing was cached, so there's no ratio to state.
        ratio = f"{read / created:.1f}x" if created else "—"
        lines.append(
            f"| {model} | {fmt_tokens(entry['input_tokens'])} | "
            f"{fmt_tokens(created)} | {fmt_tokens(read)} | {ratio} |")
    return lines + [""]


def render_skills_by_agent(skill_events, top_n: int = 20) -> list:
    """
    Which skills fire on the main thread, and which fire inside which agents.

    The table the module has been chasing. Skills, agents, and models have no
    clean 1:1 mapping, and this is where that stops being an assertion: a skill
    can be something you invoke by hand, something an Explore agent reaches for,
    or both, and the counts differ.

    Agent types are listed with counts rather than spread across columns. There
    are fifteen of them on this machine, and a fifteen-column matrix of mostly
    zeroes reads worse than the list it's hiding.
    """
    lines = ["## Skills × Agents", ""]
    if not skill_events:
        return lines + ["*(no skill events yet)*", ""]

    by_skill = {}
    for event in skill_events:
        entry = by_skill.setdefault(event.get("skill") or "<unknown>",
                                    {"main": 0, "agent": 0, "types": Counter()})
        if event_thread(event) == "agent":
            entry["agent"] += 1
            entry["types"][event.get("agent_type") or "<unattributed>"] += 1
        else:
            entry["main"] += 1

    lines += ["| Skill | Main | Agent | Fired inside |", "|---|---:|---:|---|"]
    ranked = sorted(by_skill, key=lambda s: -(by_skill[s]["main"] + by_skill[s]["agent"]))
    for skill in ranked[:top_n]:
        entry = by_skill[skill]
        inside = ", ".join(f"{name} ×{n}" for name, n in entry["types"].most_common())
        lines.append(f"| `{skill}` | {entry['main']} | {entry['agent']} | "
                     f"{inside or '—'} |")
    if len(by_skill) > top_n:
        lines.append("")
        lines.append(f"*({len(by_skill) - top_n} more skills not shown.)*")
    return lines + [""]


def render_report(usage_rows, spawn_rows, skill_events) -> str:
    """
    Build the exploration artifact that sits beside the MOC.

    Regenerated locally like the MOC and never synced, on the same three-artifact
    logic: it's derived, disposable, and specific to the machine that wrote it.
    Every section degrades to a note rather than an error when its stream is
    empty, because a fresh host has all four stores missing and still has to
    render something.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sessions = {e["session"] for e in skill_events if event_thread(e) == "main"}
    lines = [
        "---",
        "title: Claude Code Usage Report",
        f"generated: {now}",
        "tags: [report, claude-code, telemetry]",
        "---",
        "",
        "# Claude Code Usage Report",
        "",
        f"*Generated {now} from {len(usage_rows or [])} usage rows and "
        f"{len(spawn_rows or [])} agent runs across {len(sessions)} sessions.*",
        "",
    ]
    lines += render_models_by_thread(usage_rows)
    lines += render_agent_types(spawn_rows, usage_rows)
    lines += render_tokens_by_skill(usage_rows)
    lines += render_cache_economics(usage_rows)
    lines += render_skills_by_agent(skill_events)
    lines += ["---", "*Built by skill_usage.py*", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #

def _model_diverged(requested, resolved) -> bool:
    """
    True when a requested model didn't expand to the family it named.

    `requested` is the shorthand you pass to the Agent tool ("opus", "haiku");
    `resolved` is the full id it became ("claude-opus-5[1m]"). Those differ as
    strings on every spawn, so comparing them raw makes the tripwire useless.
    What matters is whether the shorthand still names the family that ran.
    """
    if not requested or not resolved:
        return False
    return not short_model(resolved).startswith(requested)


def audit_report(events, usage_rows, spawn_rows) -> str:
    """
    Cross-check the streams against each other and print what disagrees.

    Module 1 kept a loud `<unknown>` bucket so a parser regression after a
    Claude Code bump showed up immediately instead of quietly corrupting counts.
    This extends that idea across all four streams, and makes it rerunnable: the
    one-off battery from the module 2 rollout becomes something you run after
    every version bump.

    Read-only and always exit 0. It reports; it doesn't judge. A number moving
    here is a prompt to go look, not proof of a bug, because most of these
    counts move for legitimate reasons too (a pruned transcript, a new agent
    type, a model that genuinely got rerouted).

    The token check is NOT an equality test, though the module 2 plan expected
    one. `totalTokens == input + output + cache_creation + cache_read` holds for
    a single message's usage block, and `toolUseResult.totalTokens` is the LAST
    message's block: the context the run ended at, not what it consumed getting
    there. Measured on one 57-message run, totalTokens was 109,592 against
    4,431,358 summed across the file. Aggregate rows can't reconstruct "the last
    message", so equality is unavailable here by construction.

    What IS checkable is the ordering: a run's final context can never exceed
    what it processed in total. A violation means one of the two parsers moved.
    """
    lines = ["# Audit", ""]

    # Agent-file usage rows key on the agent file's stem, which is agent-<id>.
    agent_tokens = {}
    for row in usage_rows:
        session = row.get("session")
        if session:
            agent_tokens[session] = agent_tokens.get(session, 0) + _row_tokens(row)

    compared = 0
    mismatches = []
    orphans = 0
    for spawn in spawn_rows:
        session = f"agent-{spawn.get('agent_id')}"
        if session not in agent_tokens:
            orphans += 1
            continue
        reported = spawn.get("total_tokens")
        if not reported:
            continue
        compared += 1
        summed = agent_tokens[session]
        if reported > summed:
            mismatches.append((session, reported, summed))

    lines += ["## Token cross-check", ""]
    if compared:
        lines.append(f"{compared} spawn/agent-file pairs compared, "
                     f"{len(mismatches)} where the reported final context "
                     f"exceeds the tokens the run processed.")
    else:
        lines.append("No comparable pairs. This host has no spawn rows whose "
                     "agent file is also in the store.")
    for session, reported, summed in mismatches[:20]:
        lines.append(f"  {session}: spawn reports {reported}, usage rows only "
                     f"sum to {summed}")
    if len(mismatches) > 20:
        lines.append(f"  ...and {len(mismatches) - 20} more.")
    lines.append("")

    # Requested vs resolved model. The requested side is a shorthand you type
    # ("opus"), the resolved side the full dated id it expanded to
    # ("claude-opus-5[1m]"), so a raw string compare flags every single spawn.
    # Divergence means the shorthand didn't expand to the family it names, which
    # is real rerouting and wants eyes.
    divergent = [s for s in spawn_rows if _model_diverged(s.get("requested_model"),
                                                          s.get("resolved_model"))]

    agent_events = [e for e in events if event_thread(e) == "agent"]
    lines += ["## Tripwires", ""]
    for label, count in (
        ("skill events in the <unknown> bucket",
         sum(1 for e in events if e.get("skill") == "<unknown>")),
        ("usage rows with no model", sum(1 for r in usage_rows if not r.get("model"))),
        ("agent-thread events missing agent_type",
         sum(1 for e in agent_events if not e.get("agent_type"))),
        ("spawn rows whose agent file was never seen", orphans),
        ("spawns where the resolved model differs from the requested one",
         len(divergent)),
    ):
        lines.append(f"- {label}: {count}")
    for spawn in divergent[:10]:
        lines.append(f"  {spawn.get('agent_id')}: requested "
                     f"{spawn['requested_model']}, resolved {spawn['resolved_model']}")

    lines += ["", "## Coverage", ""]
    lines += [f"- events: {len(events)}",
              f"- usage rows: {len(usage_rows)}",
              f"- spawn rows: {len(spawn_rows)}"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CLI glue: data-dir resolution, best-effort git, subcommands
# --------------------------------------------------------------------------- #

DEFAULT_OUT = Path("~/.claude/skills/skill-usage-moc.md")
# The report sits beside the MOC and follows the same three-artifact rule:
# generated locally, never synced, safe to delete.
DEFAULT_REPORT_OUT = Path("~/.claude/skills/claude-usage-report.md")


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


def resolve_host(arg: str | None) -> str:
    """
    The store's partition key: --host, then $SKILL_MOC_HOST, then the hostname.

    gethostname() is a convenient default, not a stable identity. On a Mac it
    picks up whatever suffix DHCP hands out, so one laptop reports "name.lan" on
    the home network and "name.local" on another. Because the host names the
    store file and the overlay merges per file, a hostname that drifts silently
    splits one machine's history across two files that render then counts as two
    machines. Pin it to something that can't move (a hardware serial) on any
    machine whose hostname isn't already fixed.
    """
    return arg or os.environ.get("SKILL_MOC_HOST") or socket.gethostname()


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
    _git(data_dir, "add", "events", "usage", "agents", "stats")
    # An empty commit (nothing changed) exits non-zero; best-effort swallows it.
    _git(data_dir, "commit", "-m", f"collect: update {host}")
    _git(data_dir, "push")


def cmd_collect(args) -> None:
    data_dir = resolve_data_dir(args.data_dir)
    host = resolve_host(args.host)
    git_pull(data_dir)
    merged = run_collect(TRANSCRIPTS_ROOT, data_dir, host)
    git_commit_push(data_dir, host)
    print(f"collect: {len(merged)} events in store for {host}", file=sys.stderr)


def cmd_render(args) -> None:
    data_dir = resolve_data_dir(args.data_dir)
    host = resolve_host(args.host)
    # render auto-collects first so the machine you're on is current the moment
    # you generate the MOC — no stale-by-one-run gap.
    git_pull(data_dir)
    run_collect(TRANSCRIPTS_ROOT, data_dir, host)
    git_commit_push(data_dir, host)

    if args.json:
        # Keyed by stream rather than a bare events array. There are four stores
        # now, and a bare list can only ever be one of them. This is a breaking
        # change to the debug surface; see CHANGELOG.
        json.dump({name: union_stores(data_dir, name)
                   for name in ("events", "usage", "agents", "stats")},
                  sys.stdout, indent=2)
        print()
        return

    out = args.out.expanduser()
    report_out = args.report_out.expanduser()
    run_render(data_dir, out, links=args.links, since=parse_since(args.since),
               report_path=report_out)
    print(f"render: wrote MOC -> {out}", file=sys.stderr)
    print(f"render: wrote report -> {report_out}", file=sys.stderr)


def cmd_audit(args) -> None:
    data_dir = resolve_data_dir(args.data_dir)
    # Read-only on purpose: no collect, no git, nothing written. You should be
    # able to run this against a store you're suspicious of without changing it.
    print(audit_report(union_stores(data_dir),
                       union_stores(data_dir, "usage"),
                       union_stores(data_dir, "agents")), end="")


def main():
    ap = argparse.ArgumentParser(
        description="Quantify your Claude Code skill usage from session transcripts.")
    sub = ap.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("collect",
                        help="parse transcripts on disk, merge into the events store, sync")
    pc.add_argument("--data-dir", default=None, help="private events repo (or $SKILL_MOC_DATA_DIR)")
    pc.add_argument("--host", default=None,
                    help="host key for the store (or $SKILL_MOC_HOST; default: hostname)")
    pc.set_defaults(func=cmd_collect)

    pr = sub.add_parser("render",
                        help="union the events stores and write the MOC (auto-collects first)")
    pr.add_argument("--data-dir", default=None, help="private events repo (or $SKILL_MOC_DATA_DIR)")
    pr.add_argument("--host", default=None,
                    help="host key for the store (or $SKILL_MOC_HOST; default: hostname)")
    pr.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"MOC output path (default: {DEFAULT_OUT})")
    pr.add_argument("--report-out", type=Path, default=DEFAULT_REPORT_OUT,
                    help=f"usage report output path (default: {DEFAULT_REPORT_OUT})")
    pr.add_argument("--since", default=None, help="only count usage on/after YYYY-MM-DD")
    pr.add_argument("--links", choices=["relative", "wikilink"], default="relative",
                    help="how skill names link to SKILL.md (default: relative)")
    pr.add_argument("--json", action="store_true",
                    help="dump the unioned events as JSON instead of writing a MOC")
    pr.set_defaults(func=cmd_render)

    pa = sub.add_parser("audit",
                        help="cross-check the streams and print what disagrees")
    pa.add_argument("--data-dir", default=None, help="private events repo (or $SKILL_MOC_DATA_DIR)")
    pa.set_defaults(func=cmd_audit)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()