# Data Contracts

<!-- Canonical shapes for data flowing through the application. -->
<!-- Agents must consume data through these contracts. -->
<!-- When mocking data, conform to these shapes exactly. -->

Two boundaries here the code can't enforce: the transcript records Claude Code writes (an input format nobody in this repo controls) and the events store (an on-disk format that has to stay readable by older and newer versions of this tool at once). Both are described in prose below. Everything between them is plain dicts.

## The Events Store

One JSONL file per host at `events/<hostname>.jsonl`, inside the *private* data repo, never this one. One JSON object per line, which keeps the store diff-friendly and lets `read_store` skip a single torn line instead of losing the whole file.

A skill-usage event, as `parse_events` writes it today:

```json
{"skill": "humanizer", "timestamp": "2026-06-17T00:00:00.000Z", "session": "<file-stem>", "host": "mbp", "channel": "tool"}
```

- `skill` — the identifier verbatim as the transcript gave it, including any `plugin:skill` namespace. Normalization to the bare inventory key happens at render time in `canonical_skill`, never on disk.
- `session` — the transcript file's stem, always. This is the merge key; see the invariant below.
- `channel` — `tool` when Claude invoked the Skill tool, `slash` when you typed `/skillname`. `render_moc` adds the two together to get a skill's total Uses, which is safe: they were measured disjoint on 2026-08-02 (zero of 224 slash events co-occur with a same-skill tool event inside five minutes). See `decisionLog.md`. Storing the channel rather than a pre-summed total is what left that decision open until it could be checked, and what would let render stop summing if a future release changes how slash commands are recorded.
- `timestamp` — may be `null`. A record with no parseable timestamp is never dropped by the `--since` filter, on the grounds that losing a real invocation to a format we failed to parse is worse than letting one slip past the cutoff.

Readers must tolerate missing keys. Every field is read through `.get()` so rows written by an older version stay valid forever, and fields are only ever added, never renamed or repurposed. `project` was removed on purpose and must not come back; see `decisionLog.md`.

### The Merge Invariant

`merge_events` overlays on `event["session"]`, which must equal the transcript file's stem. For a subagent transcript that stem is `agent-<id>`, so that's what `session` holds, and the parent session's UUID belongs in a separate `parent_session` field. Putting the parent UUID in `session` would make two agent files that share a parent collide on the merge key and clobber each other. The design doc singles this out as the easiest thing here to get wrong.

Source: `merge_events` and `parse_events` in `skill_usage.py`, plus `docs/plans/2026-07-14-agents-models-tokens-design.md` §5.

### Planned Streams (Module 2, Not Yet Built)

The store grows from one directory to four, all host-partitioned and overlay-merged identically: `events/` for skill invocations, `usage/` for token aggregates per transcript and model and skill, `agents/` for one row per subagent run, and `stats/` for one row per transcript. Every row in every stream will also carry `cc_version`, so a parser regression is traceable to the Claude Code release that caused it. Full schemas live in `docs/plans/2026-07-14-agents-models-tokens-design.md` §5. Don't infer any of this from the current code, because none of it is implemented.

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
