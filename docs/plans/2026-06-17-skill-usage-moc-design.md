# quantified-claude: Skill-Usage MOC — Design

**Date:** 2026-06-17
**Status:** Design agreed; ready to implement.
**Project:** `quantified-claude` — an umbrella for quantifying one's own Claude
Code usage. **Skill-usage is the first module**; other facets may layer on later
(not planned yet, and deliberately out of scope for this doc).
**Supersedes:** the original handoff prompt. Where this doc and the handoff
disagree, this doc wins — the deltas (and *why*) are called out in
[§7 Departures from the handoff](#7-departures-from-the-handoff).

---

## 1. What this builds

The first `quantified-claude` module: a personal tool that generates a **Map of
Content (MOC)** of the user's Claude Code skills, annotated with the user's own
usage frequency, counted retroactively from session transcripts already on disk.
Output is an Obsidian-friendly markdown file. Single-user telemetry, not team
observability.

The user works across several machines and travels, so usage must roll up across
all of them.

---

## 2. The three artifacts (the core insight)

The design hinges on recognizing that the system produces three kinds of thing
with three different lifecycles. Keeping them in separate homes is what keeps
the whole thing simple and the public/private boundary structural rather than
remembered.

| Artifact | What it is | Home | How it syncs |
|---|---|---|---|
| **Tool** | the code | public GitHub *template* repo (`~/repos/quantified-claude`) | normal `git pull` updates |
| **Events** | source telemetry, the shared truth | a separate **private** repo | `collect` self-syncs it |
| **MOC** | derived artifact, reproducible anytime | `~/.claude/skills/` (with the content it maps) | not synced — regenerated locally per machine |

Why this split matters:

- **Code is shareable; usage is data.** The same principle that says telemetry
  doesn't belong in dotfiles says it doesn't belong inside a clone of the tool
  either. Two repos make the boundary structural.
- **A MOC must live with the content it maps**, or its links are dead. Its home
  is the skills dir, full stop.
- **The MOC never needs to sync.** It's derived from events, and `render` is
  idempotent. Each machine pulls the synced *events*, then regenerates its own
  local MOC. Every machine's MOC reflects the full cross-machine union (because
  the events behind it are unioned), but the file itself is regenerated, never
  merged — so no sync conflicts and no chezmoi churn.

---

## 3. Architecture

**One script, two subcommands** — keeps it a single readable file with shared
parsing code:

- `skill-usage-moc.py collect` — parse the transcripts currently on disk →
  **merge their events into `events/<hostname>.jsonl` keyed by session UUID** →
  `git pull --rebase && commit && push`. **Running collect *is* syncing.**
  Because each machine only ever writes its own `events/<host>.jsonl`, the rebase
  cannot conflict.
- `skill-usage-moc.py render` — `git pull` the data repo → union every
  `events/*.jsonl` → union the skills inventory → write the MOC to
  `~/.claude/skills/`. **`render` runs a local `collect` first**, so the machine
  you're on is always current the moment you generate the MOC.

**The events store is a durable accumulator, not a throwaway** — and this is what
lets transcript retention stay *low* (see §6). The merge is keyed by session UUID,
which works because **a finished transcript is immutable**: a session's `.jsonl`
only grows while live, then never changes. So `collect`:

1. Loads the existing events store (a dict keyed by session).
2. Parses whatever transcripts are *currently on disk*.
3. **Overlays** — replaces each found session's slice (never appends).
4. Leaves sessions already in the store but pruned from disk untouched.
5. Writes the store back.

This keeps every property the handoff's "always recompute from full history"
design was protecting, **without** requiring transcripts to live forever:
- **Idempotent + self-healing** — running twice equals running once; a missed run
  just re-overlays every present transcript next time.
- **No double-counting** — the session UUID is a natural idempotency key. (The
  handoff's "dedup-at-the-seam" fear applied only to a *blind* append; a keyed
  merge has no seam.)
- **Pruned transcripts' counts survive** — they're already in the store.

**Scheduled, not hooks** (kept from the handoff): real-time isn't needed, and a
scheduled run is simpler — no two-event hook capture, no jq dependency.

**The one cost of pruning:** a parser bug discovered *late* can only be corrected
over transcripts still on disk; counts for already-pruned sessions keep whatever
they had at ingest. Mitigated by verifying the extractor upfront (§9), a loud
`<unknown>` bucket (a CC version bump that breaks field extraction shows counts
cratering immediately, not silently), and a modest retention buffer (§6).

The **collect/render split is inherent**, not over-engineering: no machine can
read another's transcripts directly, so something must reduce local transcripts
to a small file, and those files have to meet somewhere.

---

## 4. Data model

Each event record carries only:

```json
{ "skill": "...", "timestamp": "...", "session": "...", "host": "..." }
```

**`project` is deliberately dropped.** The reference script recorded it on every
event, but the MOC counts by skill, never by project — so `project` was dead
weight that *also* happened to encode client/engagement names (the consultant
privacy leak). Not recording it is a better fix than redaction or hashing: it
deletes the liability instead of guarding it.

**Forward-compatible:** `project` can be added back later with no migration —
`render` tolerates missing keys, so old records stay valid. (User chose "maybe
later" on per-project breakdowns.)

If per-project views are ever wanted, the fallback is to store `project` and
keep those event files **age-encrypted** via the user's existing chezmoi `age`
setup — never plaintext client names in a repo.

---

## 5. Privacy

- No client names in events (project dropped), so the **data repo carries no
  sensitive identifiers** — session UUIDs + timestamps + skill names + hostnames
  only. Still keep the data repo **private** as defense in depth.
- The public tool repo contains code only — nothing to audit for secrets before
  sharing.
- The MOC (skill names + counts) is low-sensitivity and lives locally; it is not
  pushed anywhere.

---

## 6. Transcript retention — a re-derive window, not a durability requirement

**Durability of usage history lives in the events store, not in the
transcripts** (see §3). The store is small, git-versioned, committed, and synced —
a far more robust archive than gigabytes of raw transcripts that get auto-pruned.
So retention no longer has to be "forever." It only governs the **re-derive
window**: how far back you can re-parse if you discover a parser bug after the
fact.

- **Target: `cleanupPeriodDays: 90`** — long enough to notice a parser regression
  after a CC version bump (the `<unknown>` bucket makes it loud) and re-derive
  before the transcripts vanish; short enough that disk stays tiny (~225 MB/month
  at the user's current heavy pace, so ~90 days ≈ sub-GB). `30` is fine if
  disk-conscious; `365` for extra margin. **Not `36500`.**
- **Do NOT set it to `0`** — that currently disables transcript writing entirely
  (known bug), the opposite of "keep them."

**Sequencing — important.** Until `collect` exists and has back-filled current
history into the events store, the transcripts are the *only* record. So:
1. Now (done): a high value (`36500`) is set, so nothing prunes while the tool
   doesn't yet exist.
2. Build `collect`; run it once to ingest the back-catalog into the events store.
3. *Then* lower `cleanupPeriodDays` to `90`.

The README must document this; don't change the user's settings silently.

---

## 7. Departures from the handoff

| Handoff said | This design | Why |
|---|---|---|
| Python, stdlib-first | **Kept** — and it's the *readability* choice, not despite it | Python reads like pseudocode; the JSON-spelunking core is where shell/jq is worst. The threat to "I can edit this" is over-engineering, not the language. |
| collect/render split | **Kept** | Inherent to multi-machine; not optional complexity. |
| Stateless re-parse of *full* history every run; raise retention to keep it all | **Revised** to a durable events store with merge-by-session-UUID; retention drops to a 90-day re-derive window | A finished transcript is immutable, so re-parsing it forever is waste. Keyed merge keeps idempotency + no-double-count while moving durability into a tiny versioned file instead of huge auto-pruned logs. |
| Sync via the dotfiles/chezmoi tree | **Rejected** | Dotfiles are config; this is data — different lifecycle and blast radius. Also: chezmoi isn't live sync (apply-on-demand, one-directional), and the user's autosync is mac-only and external. |
| Strip/hash the `project` field | **Replaced** by dropping `project` entirely | The MOC never uses it; deleting it beats guarding it. |
| Per-OS scheduling templates (launchd + systemd + cron) | **Trimmed** to mac `launchd` now; WSL/Linux documented, not shipped | Mac-primary user. `render` auto-collecting makes scheduling optional everywhere, so fragile WSL timers aren't worth hand-rolling. |
| Optional CI render | **Cut** | The MOC is regenerated locally; no need for a committed-always-current copy. |

---

## 8. CLI surface

```
skill-usage-moc.py collect [--data-dir DIR] [--host NAME]
skill-usage-moc.py render  [--data-dir DIR] [--out PATH] [--since YYYY-MM-DD]
                           [--links relative|wikilink] [--json]
```

- `--data-dir` — private data repo. Defaults to `$SKILL_MOC_DATA_DIR`, then a
  config line. The public code carries zero knowledge of where private data
  lives.
- `--out` — MOC output path. Defaults to `~/.claude/skills/skill-usage-moc.md`.
- `--links` — `relative` (default; `[name](./name/SKILL.md)`, works in any
  editor / GitHub) or `wikilink` (`[[skill-name]]`, for when the skills dir
  lives inside an Obsidian vault).
- `--since` — count usage on/after a date.
- `--json` — dump raw events (also the verification tool — see §9).

---

## 9. Verification items (confirm against the live CC version before trusting counts)

1. **Skill-name extraction.** The Skill tool's input schema is undocumented and
   drifts across CC versions. Run `skill-usage-moc.py render --json | head` (or a
   collect dry-run) against real data, inspect the actual `input` shape, and
   adjust `SKILL_NAME_KEYS` / `extract_skill_name` if needed.
2. **Direct `/skillname` invocations.** Two invocation paths exist: (a) Claude
   calls the `Skill` tool — what the parser catches; (b) the user types
   `/skillname`, which may bypass the Skill tool. Confirm how the direct path is
   recorded; if it's missing, either parse those records too or document the
   undercount as a known limitation.

---

## 10. Portability (incl. a future WSL box)

The core is portable with no code changes: `Path.home()` and
`socket.gethostname()` resolve correctly inside WSL (where `claude` runs as a
Linux process), per-host event files mean any new host slots in for free, and
git self-sync works anywhere.

The only WSL-specific friction is **scheduling** — WSL is bad at cron/systemd
(the distro isn't always running; systemd needs enabling in `/etc/wsl.conf`).
But because `render` auto-collects and everything is idempotent + self-healing, a
WSL box doesn't *need* a scheduler to participate — running `render` counts and
syncs it. Document the options (systemd-in-WSL2 user timer, or a Windows Task
Scheduler entry poking `wsl.exe … collect`) for future-you; ship no fragile code
now.

---

## 11. Build order

1. `git init` the public repo; drop in `skill-usage-moc.py` as the starting
   module; mark it a GitHub template; ensure it's the public repo.
2. Run `--json` on real data; confirm/fix Skill-name extraction (§9).
3. Refactor to `collect` + `render` subcommands sharing the parser.
4. Implement `--data-dir`, the **merge-by-session-UUID** writer for
   `events/<host>.jsonl` (load → overlay found sessions → save), and `collect`'s
   git self-sync.
5. Implement `render`: pull, union events, union inventory, emit MOC with
   `--links` support; have it auto-collect locally first.
6. Tests: unit-test the parser against fixture `.jsonl` lines — include a
   plugin-namespaced skill (`plugin-name:skill-name`), a `<unknown>` case, and a
   `--since` boundary. Also test the merge: re-ingesting the same session is
   idempotent (no double-count), and a session present in the store but absent
   from disk survives a `collect` run.
7. Set up the private data repo; wire `--data-dir` to it.
8. Run `collect` once to **back-fill the current transcript history** into the
   events store.
9. **Only now** lower `cleanupPeriodDays` to `90` (it's at `36500` until the
   store holds the back-catalog — see §6).
10. Ship the mac `launchd` plist; document WSL/Linux scheduling.
11. README: architecture rationale, the events-store-vs-retention model (§6), the
    privacy note, and the two-repo setup.
```
