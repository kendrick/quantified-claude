# quantified-claude Module 2: Agents, Models & Tokens — Design

**Date:** 2026-07-14
**Status:** Design agreed; issues filed; ready to implement.
**Project:** `quantified-claude`. Module 1 is [skill-usage](2026-06-17-skill-usage-moc-design.md); this is module 2, built on the same collector.
**Relationship to module 1:** additive. Every constraint in the module-1 design still holds: the three-artifact split, the merge-by-session-UUID overlay, the privacy stance, pure-stdlib readability. This doc extends the event schema and adds rendered artifacts; it changes none of the load-bearing decisions.

---

## 1. What this builds

Module 1 answered one question: which skills do I use, and how often. Module 2 widens the lens to three more, all mined from the same transcripts already on disk:

- **Agents** — which subagent types I spawn, how often, and how much work they do.
- **Models** — which model runs where. The main thread and the subagents it spawns don't run the same model, and skills, agents, and models turn out to have no clean 1:1 mapping. This makes that visible.
- **Tokens** — input, output, and the two cache buckets, attributed down to the skill and the model that spent them.

Same shape as module 1: single-user, retroactive, no instrumentation. The collector reads what Claude Code already writes.

**Why now, and why collection comes first.** Transcripts age out at `cleanupPeriodDays: 90`. Everything here is derivable from whatever is on disk the day the collector ships, and only from that. Every week the schema isn't collecting is a week of subagent history that ages out unrecorded. So the build order front-loads collection: the parsers and the durable store land first (issues #1-3 and #10), rendering follows (issues #5-7). Rendering can always be added later against a store that's been accruing the whole time; the store can't be back-filled past the retention window.

---

## 2. What the transcripts actually carry

This design is grounded in a probe of the 417 transcript files on the primary machine, not in assumptions about the record format. The numbers below are measured; they're what makes the schema trustworthy.

**Subagent transcripts are separate files.** They live at `<project>/<session>/subagents/agent-<agentId>.jsonl`, confirmed 311/311. Session files sit directly under the project dir. Crucially, session files contain **zero** inline sidechain records (0 of 55,872 in the 60 newest), so there's no double-count risk between the two file kinds. This also exposes a latent defect in module 1: `collect_from_disk` already rglobs `*.jsonl`, so it's been parsing agent files all along, and their skill events land with `session="agent-<id>"` and get silently folded into the MOC's counts. 39 of 363 historical Skill-tool calls came from inside subagents. Module 2 doesn't remove those; it labels them.

**Agent files self-identify.** Every record carries `agentId`, `sessionId` (the parent session's UUID), and `isSidechain: true`. Most also carry `attributionAgent`, the subagent type: `general-purpose`, `Explore`, or a custom name. Coverage is 290/297; the seven misses are all old `2.1.92` files.

**Assistant records carry the model and the token usage.** `message.model` (e.g. `claude-opus-4-8`, `claude-haiku-4-5-20251001`; `<synthetic>` marks error placeholders and gets excluded) and `message.usage` with `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, and finer sub-fields.

**Tokens must be deduped by message.** The same `message.id` appears on up to 24 consecutive records (streaming and content-block splits), each repeating an identical usage block (3,508 identical against 1 differing). Counting naïvely inflates by that factor. The dedup unit is `(transcript-file, message.id)`, counted once.

**Skills are natively attributed.** `attributionSkill` rides on exactly the assistant records that carry usage: `humanizer` on 3,224 of them, `speckit-implement`, `superpowers:brainstorming`, and so on. That's what makes tokens-per-skill directly derivable rather than something we'd have to reconstruct by hand.

**The parent session summarizes each spawn.** The Agent tool_use records `{subagent_type, description, prompt, model?}`, and its result carries `toolUseResult: {status, agentId, agentType, resolvedModel, totalTokens, totalDurationMs, totalToolUseCount, usage, toolStats}`. Coverage is partial (279/366 `totalTokens`, 185/366 `resolvedModel`), so this is the enrichment-and-fallback layer, not ground truth; the agent file's own records are ground truth and have full coverage. But `toolStats` (lines added/removed, per-tool counts) and `status` exist nowhere else, and the `agentId` link ties a run back to its spawning session even after the agent file is pruned. Verified identity: `totalTokens == input + output + cache_creation + cache_read`.

**A second probe round surfaced more signal** (see §7 for what became issues and what didn't): tool errors are recorded (`is_error: true` on tool_result, 990 of 27,115; `ExitPlanMode` errors at 57/119 read as plan rejections), compactions land as `system.subtype == "compact_boundary"` (39), interrupts as `[Request interrupted` markers (69), API errors as `system.subtype == "api_error"` (83). `version` is on 81% of records. `pr-link` records exist but the harness re-emits each one up to 14×, so a PR count needs dedup by `(prRepository, prNumber)`: 727 raw collapse to 19 unique across 9/110 sessions.

---

## 3. The store: four streams, one merge

Module 1's private events repo grows from one directory to four, all under the same host-partitioned, overlay-merged model:

| Stream | Directory | One row per | New in |
|---|---|---|---|
| Skills | `events/<host>.jsonl` | skill invocation | module 1 (enriched here) |
| Tokens | `usage/<host>.jsonl` | (transcript, model, skill) | module 2 |
| Agents | `agents/<host>.jsonl` | subagent run | module 2 |
| Session stats | `stats/<host>.jsonl` | transcript file | module 2 |

Separate directories rather than a `kind` field in one file. Each directory maps to exactly one parse function, its rows are homogeneous, and old readers never trip over a foreign row. `read_store`, `write_store`, and `merge_events` are already generic over the path, so the four streams share all of that plumbing unchanged. The only glue that moves is `git_commit_push`, which now stages all four directories.

**Everything keys on the same session UUID (the file stem), so the overlay merge is identical across streams.** Re-parsing a transcript replaces that session's slice wholesale in every stream at once. A session pruned from disk survives in every stream. The idempotence and self-healing that module 1 proved carry over for free.

---

## 4. Aggregate, not per-message: why it's safe

The token stream stores **aggregates**: one row per `(transcript, model, attributionSkill)`, not one row per API message. Today's corpus is ~1,300 aggregate rows against 25,000-35,000 per-message rows; the aggregate store is tens of KB where the per-message store would be 6-10 MB and grow 1-2 MB a month.

The usual objection to aggregating early is that you lose the ability to re-derive if you got the aggregation wrong. That objection doesn't apply here, and the reason is the overlay merge. Because every collect re-parses each on-disk transcript and **replaces** its slice wholesale, a fixed parser re-derives every aggregate inside the retention window on the next run—exactly as it re-derives skill counts today. Aggregation isn't a lossy one-way commit; it's a projection recomputed from source on every collect. The only thing genuinely lost is intra-session message ordering, which no planned query needs.

Aggregates also keep the git store diff-friendly: a new session lands as one to four added lines, not a few hundred, so the history stays readable and the repo stays small.

**When rows are written.** On every collect: the daily launchd run and render's auto-collect both trigger it. A session that's still live gets its rows rewritten on the next collect (the overlay replaces the slice), converging the moment the transcript goes immutable. There's no double-count window, because there's no append seam, the same property module 1 relies on.

---

## 5. Schemas

Old rows stay valid forever; new fields are only ever added, and every reader uses `.get()`. All rows in all four streams carry `cc_version` (the last `version` string seen in the source file), so a parser regression after a Claude Code bump is traceable to the release that caused it.

```json
// events/<host>.jsonl: skill events, enriched
{"skill": "humanizer", "timestamp": "...", "session": "<file-stem>", "host": "mbp", "channel": "tool", "thread": "main", "cc_version": "2.1.209"}
{"skill": "systematic-debugging", "timestamp": "...", "session": "agent-<id>", "host": "mbp", "channel": "tool", "thread": "agent", "agent_type": "Explore", "parent_session": "<uuid>", "cc_version": "2.1.209"}

// usage/<host>.jsonl: token key names identical to message.usage; skill = attributionSkill or null;
// messages = count of unique message.id; sorted (session, model, skill)
{"session": "<file-stem>", "host": "mbp", "thread": "main", "model": "claude-opus-4-8", "skill": "humanizer",
 "messages": 42, "input_tokens": 1234, "output_tokens": 5678,
 "cache_creation_input_tokens": 90000, "cache_read_input_tokens": 400000,
 "timestamp": "<max seen>", "cc_version": "2.1.209"}

// agents/<host>.jsonl: session = the SPAWNING file's stem; agent_type = toolUseResult.agentType || input.subagent_type;
// optional fields null when absent; NO prompt/description/content, ever
{"session": "<parent-stem>", "host": "mbp", "timestamp": "...", "agent_id": "...", "agent_type": "Explore",
 "requested_model": null, "resolved_model": null, "status": "completed",
 "total_tokens": 52553, "duration_ms": 123456, "tool_use_count": 45,
 "tool_stats": {"readCount": 25, "bashCount": 20, "linesAdded": 0, "linesRemoved": 0}, "cc_version": "2.1.209"}

// stats/<host>.jsonl: one row per session file; counts only, never URL/path/branch strings
{"session": "<file-stem>", "host": "mbp", "thread": "main", "timestamp": "<max seen>", "cc_version": "2.1.209",
 "compactions": 1, "interrupts": 2, "api_errors": 0, "prs_created": 1,
 "tool_calls": {"Bash": {"calls": 44, "errors": 2}, "ExitPlanMode": {"calls": 1, "errors": 1},
                "mcp__context7__query-docs": {"calls": 3, "errors": 0}}}
```

**The merge invariant that's easy to get wrong.** `merge_events` overlays on `event["session"]`, which is the file stem. Agent events belong to an `agent-<id>.jsonl` file, so their `session` is `agent-<id>`, which is what the overlay needs. The parent session's UUID goes in a **separate** `parent_session` field. Repurposing `session` to hold the parent UUID would break the overlay: two different agent files sharing a parent would collide on the merge key. Keep `session` as the stem; `parent_session` is the join field for render.

Raw model strings are stored (`claude-haiku-4-5-20251001`); normalization to a short label happens only at render time.

---

## 6. Privacy

Module 1 dropped `project` because it encoded client and engagement names. Module 2 mines richer records, so several fields now need the same discipline, and the rule is the same one that worked before: don't store the string, store the count.

- **Agent prompts, descriptions, and result content are never stored.** They routinely carry client context. The agent stream keeps `status`, models, token totals, and tool-stat counters: numbers and enums, nothing free-text. A sentinel test pins this: a fixture whose prompt contains a marker string asserts the marker never appears in the serialized rows.
- **`cwd`, `gitBranch`, `prUrl`, and `prRepository` are never stored.** All four identify clients. The PR *count* is kept (deduped); the URLs and repo names are not. `cwd` and `gitBranch` are dropped entirely; the per-project slicing they'd enable was considered and declined rather than encrypted, which keeps the private repo free of client-identifying strings by construction.
- **The data repo stays private** as defense in depth, exactly as in module 1.

An age-encrypted project stream (via the existing chezmoi `age` setup) is the documented fallback if per-project views are ever wanted, but the default is not to collect those strings at all.

---

## 7. The second probe: what became issues, what didn't

The first design covered agents, models, and tokens. A second probe round checked for signal being discarded elsewhere in the records. The verdict, by category:

| Signal | Coverage | Verdict |
|---|---|---|
| Tool errors (`is_error`), plan rejections (`ExitPlanMode` errors), MCP-tool census, compactions, interrupts, api_errors | high; per-tool | **Issue #10.** New session-stats stream; retention-critical, so Phase 1. |
| `cc_version` per record | 81% | **Folded into #1-3 + #10.** Stamped on every stream's rows. |
| Requested vs `resolvedModel` divergence | 0/34 divergent today | **Folded into audit #8.** A tripwire: any future divergence means routing changed and wants eyes. |
| PR count | `pr-link`, 727 raw → 19 unique | **In #10**, deduped by `(repo, number)`; strings not stored (see §6). |
| `cwd` / `gitBranch` | 81% | **Declined.** Client-identifying; encrypted stream documented but not built. |
| Per-record timestamps for concurrent-run reconstruction | dense enough (155 overlapping agent windows) | **Deferred.** Interesting but render-only, not retention-critical; revisit after Phase 2. |

---

## 8. Rendering

Two surfaces, deliberately asymmetric.

**The MOC stays a skills map.** It gains one column (Tokens, joined to each skill through the existing `canonical_skill` normalization) and an honest summary line that stops conflating agent transcripts with sessions ("N sessions + M agent runs"). Nothing else. The MOC's job is to map skills to their SKILL.md files; token-per-skill is a natural annotation on that, everything else would be bloat.

**A second artifact, `~/.claude/skills/claude-usage-report.md`, holds the exploration.** Regenerated locally like the MOC, never synced, same three-artifact logic. Its sections: Models × Thread (the table where the main thread's opus-heavy mix visibly diverges from the subagents' haiku/sonnet mix), Agent Types (spawns, completion rate, median tokens per run, lines touched), Tokens by Skill (top-N with model mix, the skills-to-models many-to-many made visible), Cache Economics (fresh input vs cache creation vs cache read, per model), and Skills × Agents (which skills fire inside which agent types). Tokens only, no dollar estimates: a baked-in pricing table goes stale as models and prices change, and staleness in a trust tool is worse than a missing feature. A cost section can be a later issue if it's ever wanted.

---

## 9. Verification: proving the numbers

The store is only worth building if the numbers are trustworthy. The battery below runs against real data at back-fill (issue #4) and becomes rerunnable via the `audit` subcommand (issue #8) after every Claude Code version bump.

- **Token cross-check (the load-bearing one).** For each agent run with both a `total_tokens` spawn row and matching agent-file usage rows, the deduped four-bucket sum must equal `total_tokens` exactly. The identity is verified on real data (0 + 822 + 110 + 51621 = 52553). Measure the match rate across all ~279 eligible pairs; any outlier gets investigated before rendering trusts the number.
- **The 39 known sidechain skill calls.** After back-fill, events with `channel == "tool"` and `thread == "agent"` on this machine must total 39, and all tool events must still total 363. This pins that the thread label is correct and that labeling changed no counts.
- **Dedup tripwire.** Unique `message.id` counts against raw assistant-record counts on the large agent files; the ~24:1 streaming collapse must show up.
- **Idempotence, per stream.** Collect twice on an unchanged tree; all four store files byte-identical.
- **Skill-count invariance.** MOC Uses / Tool / Slash totals identical before and after the schema change, because enrichment adds fields, never events. Only the summary's session count may move (it was inflated by counting agent files as sessions).
- **Stats spot-checks.** Summed `prs_created` == 19; `ExitPlanMode` errors ≈ 57; `compactions` == 39; tool-error totals near 990/27,115.
- **Privacy grep.** The private repo greps clean for `"prompt"`, `"prUrl"`, `"cwd"`, `"gitBranch"`, and a known client name.

---

## 10. Build order

Ten issues, dependency-ordered. Phase 1 is retention-critical and ships first; the GitHub numbers run 1→10 so cross-references line up, but #10 is Phase 1 work despite its number (its body says so).

1. **Phase 1 — Collector.** Thread-aware skill events (#1), token aggregates (#2), agent spawn records (#3), session stats + `cc_version` on every stream (#10), then back-fill and run the trust battery (#4).
2. **Phase 2 — Render.** MOC tokens column and honest session count (#5), the usage-report skeleton with Models × Thread and Agent Types (#6), the insight sections (#7).
3. **Phase 3 — Trust and docs.** The `audit` subcommand (#8) and the doc updates (#9).

The collector changes all land in `skill_usage.py`: one walk, four row streams, shared merge and store and git plumbing. The single-file design holds; the guard-rail is ~1,300 lines, past which rendering splits into its own module.
