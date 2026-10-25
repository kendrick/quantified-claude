# Decision Log

Append-only; newest entry on top. Don't edit past entries; supersede them with a new one.

Each entry follows this shape:

```markdown
## 2026-04-19: Short title

**Source:** the commit, PR, or discussion it came from (optional for hand-written entries)

**Context:** Why this came up.
**Decision:** What was decided.
**Alternatives considered:** What was rejected, and why.
```

## 2026-10-25: Key usage rows per UTC hour as well

**Source:** issue #12, PR #15; the §4 addendum in `docs/plans/2026-07-14-agents-models-tokens-design.md`

**Context:** The 2026-07-14 entry held that aggregating per transcript lost only message ordering. It also lost when inside a session the tokens were spent. Every row was stamped at the session's last message, which put about a third of measured tokens on the wrong day.
**Decision:** Rows are keyed per `(transcript, model, attributionSkill, UTC hour)` and carry an `hour` field. Totals don't change, because every reader sums rows. This supersedes the 2026-07-14 entry's claim about what aggregation loses, and the rest of that entry stands.
**Alternatives considered:** Per-day buckets, at 1.07x the rows against 1.50x for hours, but coarser than the hour-level timestamps `events/` already carries. Per-message rows stay rejected (`antipatterns.md`).

## 2026-08-02: The tool and slash channels are disjoint, so summing them is correct

**Source:** measured against the union events store (879 events, both hosts)

**Context:** The 2026-06-17 design (§9) asked for this before the channels got summed, and it was never recorded as done. `render_moc` sums them anyway, so every Uses number ever printed depended on the answer.

**Decision:** They're disjoint. Of 224 slash events, zero have a same-skill tool event in the same session within 60 seconds, and still zero at five minutes. Pairings only start appearing at the one-hour window (11) and the one-day window (32), which is the spacing of separate invocations in a long session rather than one action recorded twice. A slash that also emitted a `tool_use` would land milliseconds apart. Summing stands, and no MOC needs revisiting.

**Alternatives considered:** Deduplicating at render was the fallback if they overlapped. Unnecessary. Worth re-running after a Claude Code release changes how slash commands are recorded, since this is a property of the transcript format rather than of the parser.

## 2026-07-14: Store token usage as per-session aggregates, not per-message rows

**Source:** `docs/plans/2026-07-14-agents-models-tokens-design.md` §4

**Context:** Aggregating early normally forfeits the ability to re-derive if the aggregation turns out to be wrong. The measured corpus is about 1,300 aggregate rows against 25,000 to 35,000 per-message rows: tens of KB versus 6 to 10 MB, growing a megabyte or two a month.
**Decision:** One row per `(transcript, model, attributionSkill)`. The usual objection doesn't apply here, because the session-keyed overlay merge re-parses each on-disk transcript and replaces its slice wholesale. A fixed parser therefore re-derives every aggregate inside the retention window on the next run. Aggregation is a projection recomputed from source, not a lossy one-way commit.
**Alternatives considered:** Per-message rows, rejected on size and on git diff noise. The only thing genuinely lost is intra-session message ordering, which no planned query needs.

## 2026-07-14: Four store directories, not one file with a `kind` field

**Source:** `docs/plans/2026-07-14-agents-models-tokens-design.md` §3

**Context:** Module 2 adds three row shapes (tokens, agent spawns, session stats) alongside module 1's skill events.
**Decision:** Separate directories, `events/` and `usage/` and `agents/` and `stats/`, each host-partitioned the same way. One directory maps to exactly one parse function, rows stay homogeneous, and an old reader never trips over a foreign row. `read_store`, `write_store`, and `merge_events` are already generic over the path, so all four streams share that plumbing unchanged. Only `git_commit_push` has to learn the new directories.
**Alternatives considered:** A single stream discriminated by a `kind` field, rejected because it makes every reader defensive against every other stream's shape.

## 2026-07-14: `event["session"]` stays the file stem; the parent UUID lives in `parent_session`

**Source:** `docs/plans/2026-07-14-agents-models-tokens-design.md` §5

**Context:** Subagent transcripts are separate files at `<project>/<session>/subagents/agent-<id>.jsonl`, and `collect_from_disk` has been rglobbing them all along. Labeling them means carrying both the agent's own id and its parent session.
**Decision:** `merge_events` overlays on `event["session"]`, and that field stays the transcript file's stem, which for an agent file is `agent-<id>`. The parent session's UUID goes in a separate `parent_session` field, which render joins on.
**Alternatives considered:** Repurposing `session` to hold the parent UUID. It reads naturally and it's wrong: two agent files sharing a parent would collide on the merge key and silently clobber each other.

## 2026-07-14: Never store prompts, `cwd`, `gitBranch`, or PR URLs

**Source:** `docs/plans/2026-07-14-agents-models-tokens-design.md` §6

**Context:** Module 2 mines much richer records than module 1, and several of the new fields carry client and engagement names.
**Decision:** Apply the rule that already worked for `project`: don't store the string, store the count. Agent prompts, descriptions, and result content never reach the store, so the agent stream holds only numbers and enums. `cwd`, `gitBranch`, `prUrl`, and `prRepository` are dropped, though the deduped PR *count* is kept. A sentinel test pins this: a fixture whose prompt contains a marker string asserts the marker never appears in the serialized rows.
**Alternatives considered:** An age-encrypted per-project stream via the existing chezmoi setup, documented as the fallback if per-project views are ever wanted. Not collecting the strings beats guarding them.

## 2026-06-17: Ship macOS launchd only; document Linux and WSL

**Source:** `docs/plans/2026-06-17-skill-usage-moc-design.md` §7 and §10; commit 4bce1bc

**Context:** The original handoff called for launchd, systemd, and cron templates.
**Decision:** Ship the mac plist and its installer, and document the WSL and Linux options in the README without shipping code for them. Because `render` auto-collects and everything is idempotent, a machine doesn't need a scheduler to participate. Running `render` by hand counts and syncs it.
**Alternatives considered:** Hand-rolled systemd or Windows Task Scheduler entries, rejected as fragile code written for machines the user doesn't primarily work on.

## 2026-06-17: Transcript retention is a re-derive window, not a durability requirement

**Source:** `docs/plans/2026-06-17-skill-usage-moc-design.md` §6

**Context:** The handoff wanted `cleanupPeriodDays` raised high enough to keep every transcript forever, because the transcripts were the archive.
**Decision:** Durability moved into the events store, which is small, versioned, committed, and synced. Retention now governs only how far back you can re-parse after fixing a counting bug, and the target is 90 days. The sequencing is load-bearing: don't lower the setting until a `collect` has back-filled existing history, because until then the transcripts are the only record.
**Alternatives considered:** `36500`, kept only until the first back-fill landed, and `0`, which is actively harmful because it disables transcript writing entirely.

## 2026-06-17: A durable events store, merged by session UUID

**Source:** `docs/plans/2026-06-17-skill-usage-moc-design.md` §3; commit 1123944

**Context:** The handoff proposed re-parsing full history on every run and keeping all transcripts forever to make that possible.
**Decision:** Keep a durable store and overlay it. `collect` loads the store, parses whatever transcripts are on disk, replaces each found session's slice wholesale, and leaves pruned-but-stored sessions untouched. This works because a finished transcript is immutable, which makes the session UUID a natural idempotency key.
**Alternatives considered:** Blind append with dedup at the seam. A keyed merge has no seam, so the dedup problem never arises.

## 2026-06-17: Drop `project` from events entirely

**Source:** `docs/plans/2026-06-17-skill-usage-moc-design.md` §4; the `parse_events` docstring in `skill_usage.py`

**Context:** The reference script recorded `project` on every event. The MOC counts by skill and never by project, and project names encode client and engagement names.
**Decision:** Don't record it. Deleting the liability beats guarding it, and the field was dead weight regardless. Readers use `.get()`, so `project` could come back later with no migration if per-project views are ever wanted.
**Alternatives considered:** Stripping or hashing the field, both rejected as guarding something that shouldn't exist in the first place.

## 2026-06-17: Harvest both the tool and slash invocation channels

**Source:** `docs/plans/2026-06-17-skill-usage-moc-design.md` §9; commits d5cc5b0 and b17a79a

**Context:** A probe against real transcripts found the reference script undercounting badly. Skills invoked as `/skillname` are recorded as `<command-name>` tags in user messages, a shape the parser never read. `speckit-plan` measured 34 slash invocations against 0 tool invocations.
**Decision:** Parse both record shapes and tag each event with `channel`, either `tool` or `slash`. Keeping the channel is cheap and makes the counts auditable, and the split turns out to be the interesting number: it separates what Claude reaches for on your behalf from what you drive by hand. Built-in CLI commands ride the same rail, so they're filtered by a conservative denylist.
**Alternatives considered:** Counting only the tool channel, which was the status quo and was wrong by an order of magnitude on some skills.

## 2026-06-17: Three artifacts, three homes

**Source:** `docs/plans/2026-06-17-skill-usage-moc-design.md` §2; `README.md`

**Context:** The system produces the tool, the usage data, and the rendered MOC. Three things with three different lifecycles.
**Decision:** Give each its own home. Code in this public repo, events in a separate private repo, the MOC regenerated locally into the skills dir. Two repos make the public/private boundary structural instead of something you have to remember. A MOC has to live with the content it maps or its links go dead. And the MOC never needs to sync at all, because it's derived and `render` is idempotent, so every machine rebuilds its own from the unioned events.
**Alternatives considered:** Syncing through the dotfiles and chezmoi tree, rejected because dotfiles are config and this is data, with a different lifecycle and a different blast radius. Also a committed always-current MOC via CI, cut as unnecessary once render is local and idempotent.

## 2026-06-17: Python, stdlib only

**Source:** `docs/plans/2026-06-17-skill-usage-moc-design.md` §7

**Context:** The obvious alternative for a "read some JSON files and count things" tool is shell plus jq.
**Decision:** Python with no dependencies. This is the readability choice rather than a concession against it, because Python reads like pseudocode and JSON spelunking is exactly where shell and jq are at their worst. The real threat to "I can still edit this myself" is over-engineering, not the language.
**Alternatives considered:** shell plus jq, rejected on readability at the core of the tool.
