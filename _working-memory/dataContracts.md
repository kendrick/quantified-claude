# Data Contracts

<!-- Canonical shapes for data flowing through the application. -->
<!-- Agents must consume data through these contracts. -->
<!-- When mocking data, conform to these shapes exactly. -->

Two boundaries here the code can't enforce: the transcript records Claude Code writes (an input format nobody in this repo controls) and the events store (an on-disk format that has to stay readable by older and newer versions of this tool at once). Both are described in prose below. Everything between them is plain dicts.

## The Events Store

Four directories, `events/`, `usage/`, `agents/`, and `stats/`, each holding one JSONL file per host at `<stream>/<hostname>.jsonl`. They live in the *private* data repo, never this one. One JSON object per line, which keeps the store diff-friendly and lets `read_store` skip a single torn line instead of losing the whole file.

A skill-usage event, as `parse_events` writes it today:

```json
{"skill": "humanizer", "timestamp": "2026-06-17T00:00:00.000Z", "session": "<file-stem>", "host": "mbp", "channel": "tool", "thread": "main", "cc_version": "2.1.209"}
```

- `skill` — the identifier verbatim as the transcript gave it, including any `plugin:skill` namespace. Normalization to the bare inventory key happens at render time in `canonical_skill`, never on disk.
- `session` — the transcript file's stem, always. This is the merge key; see the invariant below.
- `channel` — `tool` when Claude invoked the Skill tool, `slash` when you typed `/skillname`. `render_moc` adds the two together to get a skill's total Uses, which is safe: they were measured disjoint on 2026-08-02 (zero of 224 slash events co-occur with a same-skill tool event inside five minutes). See `decisionLog.md`. Storing the channel rather than a pre-summed total is what left that decision open until it could be checked, and what would let render stop summing if a future release changes how slash commands are recorded.
- `thread`—`main`, or `agent` for a subagent transcript (`agent-<id>.jsonl`). Agent-thread rows also carry `agent_type` and `parent_session`, and main-thread rows leave both keys out.
- `cc_version`—the last `version` string in the source transcript. Every row in every stream carries it, so a parser regression traces back to the Claude Code release that caused it.
- `timestamp` — may be `null`. A record with no parseable timestamp is never dropped by the `--since` filter, on the grounds that losing a real invocation to a format we failed to parse is worse than letting one slip past the cutoff.

Readers must tolerate missing keys. Every field is read through `.get()` so rows written by an older version stay valid forever, and fields are only ever added, never renamed or repurposed. `project` was removed on purpose and must not come back; see `decisionLog.md`.

### The Merge Invariant

`merge_events` overlays on `event["session"]`, which must equal the transcript file's stem. For a subagent transcript that stem is `agent-<id>`, so that's what `session` holds, and the parent session's UUID belongs in a separate `parent_session` field. Putting the parent UUID in `session` would make two agent files that share a parent collide on the merge key and clobber each other. The design doc singles this out as the easiest thing here to get wrong.

Source: `merge_events` and `parse_events` in `skill_usage.py`, plus `docs/plans/2026-07-14-agents-models-tokens-design.md` §5.

### The Other Three Streams

`usage/`, `agents/`, and `stats/` overlay-merge exactly like `events/`. Re-parsing a transcript replaces that session's rows in all four streams at once. Design doc §5 has the reasoning. The shapes below are what the parsers write.

`usage/`, one row per transcript × model × skill × UTC hour, from `parse_usage`:

```json
{"session": "<file-stem>", "host": "mbp", "thread": "main", "model": "claude-opus-4-8", "skill": "humanizer", "messages": 42, "input_tokens": 1234, "output_tokens": 5678, "cache_creation_input_tokens": 90000, "cache_read_input_tokens": 400000, "hour": "2026-06-17T14", "timestamp": "<latest seen in that hour>", "cc_version": "2.1.209"}
```

- `skill`—the record's `attributionSkill`, or `null` when no skill claimed the message.
- `hour`—the UTC hour as `YYYY-MM-DDTHH`, or `null` when the timestamp didn't parse. It's what puts tokens on the day they were spent (#12). Rows from sessions pruned before this change have no `hour` key and keep their single stamp, so readers use `.get("hour")`.
- `messages`—the count of unique `message.id` values. Streaming can spread one message across as many as 24 records, each repeating the same usage block, so `parse_usage` counts each ID once.
- The four token keys are `USAGE_TOKEN_KEYS`, named exactly as `message.usage` names them.
- `model`—the raw ID. `short_model` shortens it at render time only.
- Agent-thread rows carry `agent_type` and `parent_session`, as in `events/`.

`agents/`, one row per subagent spawn, from `parse_spawns`:

```json
{"session": "<parent-stem>", "host": "mbp", "timestamp": "...", "agent_id": "...", "agent_type": "Explore", "requested_model": null, "resolved_model": null, "status": "completed", "total_tokens": 52553, "duration_ms": 123456, "tool_use_count": 45, "tool_stats": {"readCount": 25, "bashCount": 20, "linesAdded": 0, "linesRemoved": 0}, "cc_version": "2.1.209"}
```

- `session`—the *spawning* transcript's stem, not the agent's own. Re-parsing the parent replaces its spawn rows.
- About a seventh of real results, all async spawns, lack `agentType` and `totalTokens`. Treat this stream as enrichment, and take tokens and model from the agent's own transcript in `usage/`.
- `agent_type`—`toolUseResult.agentType`, falling back to the spawn input's `subagent_type`. `requested_model` comes from the spawn input and `resolved_model` from the result. Each is `null` when its source is absent.
- `status`—seen in real data as `completed`, `async_launched`, and `forked`.
- `tool_stats`—`toolUseResult.toolStats`, keeping only keys whose value is an int or float. A new counter survives a Claude Code release, while strings, nested maps, and bools are dropped. A missing or non-map `toolStats` is stored as `null`.

`stats/`, one row per transcript file, from `parse_stats`:

```json
{"session": "<file-stem>", "host": "mbp", "thread": "main", "timestamp": "<latest seen>", "tool_calls": {"Bash": {"calls": 44, "errors": 2}}, "compactions": 1, "interrupts": 2, "api_errors": 0, "prs_created": 1, "cc_version": "2.1.209"}
```

- `tool_calls`—per tool name, including MCP tools. When a truncated file has lost an error's `tool_use`, `parse_stats` drops the error because it has no tool name to charge.
- `compactions` and `api_errors`—counted from the system record's `subtype` field. A text search would also count every tool result that quotes `compact_boundary`.
- `prs_created`—PRs deduplicated within this session only. Summing it across rows overcounts a PR worked on in two sessions, and the store can't correct that because it never keeps the repo or number.
- `parse_stats` ignores `--since`, because a cutoff would turn a whole-file count into a partial one.

## Claude Code Transcripts (Input, Not Ours)

Read from `~/.claude/projects/**/*.jsonl`. This is an undocumented format that shifts across Claude Code versions, which is why the parser is defensive rather than schema-driven:

- Skill invocations arrive as a `tool_use` block named `Skill`. Its depth in the record varies by version, so `find_tool_uses` walks the structure recursively instead of following a fixed path. The naming key has moved too, so `SKILL_NAME_KEYS` tries several and falls back to scanning the input blob, landing on `<unknown>` only when nothing works.
- Slash invocations arrive as `<command-name>/skillname</command-name>` inside a user message. Only user records with *string* content are trusted. That one rule rejects every false positive seen in real data, because assistant prose quoting the tag arrives as a content list instead.
- A malformed JSON line is skipped rather than fatal (`iter_jsonl`).

The `<unknown>` bucket is a tripwire, not a fallback to be quietly improved away. When a Claude Code version bump breaks field extraction, counts crater visibly instead of drifting.

## The Skills Inventory

`collect_inventory` maps skill name to `{description, path}` by finding every `SKILL.md` under `~/.claude/skills` and `~/.claude/plugins`. The name comes from the YAML frontmatter's `name`, falling back to the containing directory's name. `parse_frontmatter` is a hand-rolled reader that understands only what the MOC needs: top-level `key: value` pairs plus block scalars (`|` and `>`), which is how skill descriptions are usually written.

## The MOC (Output)

Markdown with YAML frontmatter, written to `~/.claude/skills/skill-usage-moc.md` by default. Three sections: Used & Owned (a table), Owned but Never Used, and Used but Not in Your Skills Dir. It's derived, disposable, and regenerated in full on every render, so never edit it by hand and never merge it across machines.

## The Usage Report (Output)

Markdown with YAML frontmatter, written beside the MOC to `~/.claude/skills/claude-usage-report.md` by default (`--report-out` moves it). `render_report` builds five sections from `usage/`, `agents/`, and `events/`: Models × Thread, Agent Types, Tokens by Skill, Cache Economics, and Skills × Agents. A section whose stream is empty prints a note instead of raising, so a fresh host still renders. The report follows the MOC's rules: derived, regenerated in full on every render, never edited by hand, never synced.
