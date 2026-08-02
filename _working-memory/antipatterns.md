# Antipatterns

<!-- Negative knowledge. Things the team tried that didn't work, captured so   -->
<!-- agents and humans don't re-litigate closed loops. Append-only, like        -->
<!-- decisionLog.md.                                                            -->
<!--                                                                            -->
<!-- Format: -->
<!-- ## YYYY-MM-DD — [Short title in imperative voice — what to avoid]         -->
<!-- **Tried:** What was attempted                                              -->
<!-- **What broke:** Observed failure mode                                      -->
<!-- **Why we backed out:** Root cause if known; otherwise the observed pain    -->
<!-- **Don't suggest:** Specific things agents should not re-propose            -->
<!--                                                                            -->
<!-- The last line is the agent-targeted lever. Be specific. "Don't suggest    -->
<!-- moving X to Y" beats "don't suggest big refactors."                       -->

Entries below come in two kinds. Some shipped and then broke against real data. Others were the plan of record and got rejected during design; those say so, and the reasoning lives in the design docs.

## 2026-07-14 — Don't put a pricing table in the report

**Tried:** Rendering dollar cost alongside token counts in the usage report, the obvious next step once tokens are being collected.
**What broke:** Nothing yet. This was declined before it was built.
**Why we backed out:** A baked-in pricing table goes stale as models and prices change, and this tool's whole value is that you trust its numbers. Stale numbers in a trust tool are worse than a missing feature.
**Don't suggest:** Hardcoding per-model prices anywhere in `skill_usage.py`, or deriving cost columns from the token aggregates. If cost is genuinely wanted, it's a separate issue with a real answer for where the prices come from.

## 2026-07-14 — Don't collect `cwd` or `gitBranch` to enable per-project slicing

**Tried:** Considered during the module-2 probe, which found both fields on 81% of records.
**What broke:** Not built.
**Why we backed out:** Both identify clients. Per-project slicing was declined rather than encrypted, which keeps the private repo free of client-identifying strings by construction rather than by discipline.
**Don't suggest:** Adding `cwd`, `gitBranch`, `prUrl`, or `prRepository` to any store row. The PR count is fine; the strings are not.

## 2026-07-14 — Don't store per-message token rows

**Tried:** The natural schema, one row per API message, aggregating later at render time.
**What broke:** Not built. It was sized first: 25,000 to 35,000 rows against roughly 1,300 aggregates, 6 to 10 MB growing 1 to 2 MB a month, and a few hundred added lines per session in the git diff.
**Why we backed out:** The usual argument for keeping raw rows is re-derivability, and the overlay merge already provides that. Every collect replaces each session's slice wholesale from source, so a fixed parser re-derives every aggregate inside the retention window.
**Don't suggest:** Expanding the `usage/` stream to per-message granularity to "keep options open." The options are already open.

## 2026-06-17 — Don't use `Path.rglob` to walk the skills directories

**Tried:** `Path.rglob("SKILL.md")` over `~/.claude/skills`, the obvious way to find skills.
**What broke:** It found zero personal skills, silently. Every skill in that directory is a symlink into a shared store at `~/.agents/skills`, and `rglob` won't descend through a symlinked directory. Python 3.13 made that the explicit default. The MOC just came out missing `humanizer` and friends, with no error anywhere.
**Why we backed out:** Fixed in commit b2ee3f6 by switching to `os.walk(followlinks=True)`, which behaves consistently across the 3.9+ range this targets. `rglob`'s `recurse_symlinks` knob doesn't exist in 3.9.
**Don't suggest:** "Simplifying" `collect_inventory` back to `rglob` or `glob`. The `os.walk` call is load-bearing and `tests/test_inventory.py` pins it.

## 2026-06-17 — Don't parse frontmatter as line-at-a-time `key: value`

**Tried:** Splitting each frontmatter line on the first colon.
**What broke:** Skill descriptions are routinely YAML block scalars (`description: |` or `description: >`) with the text on following indented lines. The naive split captured only the `|` marker and dropped the body, so those skills showed a blank or one-character description in the MOC.
**Why we backed out:** Fixed in commit 68f5472. `parse_frontmatter` now gathers indented continuation lines after a block marker and treats indented lines as continuations rather than new keys, which also stops a colon inside the body from being misread as a key.
**Don't suggest:** Reverting to a simple split, or swapping in PyYAML. The stdlib-only constraint holds, and `tests/test_frontmatter.py` covers both block styles.

## 2026-06-17 — Don't sync events through the dotfiles or chezmoi tree

**Tried:** Proposed in the original handoff as the sync mechanism, since chezmoi was already set up.
**What broke:** Rejected at design time.
**Why we backed out:** Dotfiles are config; this is data. Different lifecycle, different blast radius. Chezmoi also isn't live sync, being apply-on-demand and one-directional, and the existing autosync is mac-only and external.
**Don't suggest:** Moving the events store into the dotfiles repo, or into a clone of this one. The separate private repo is what makes the public/private boundary structural.

## 2026-06-17 — Don't strip or hash `project`; it isn't collected at all

**Tried:** The handoff proposed redacting or hashing the project field to fix the client-name leak.
**What broke:** Rejected at design time.
**Why we backed out:** The MOC counts by skill and never by project, so the field was dead weight that also happened to be a liability. Deleting it beats guarding it.
**Don't suggest:** Adding `project` back in hashed, truncated, or redacted form. If per-project views are ever genuinely wanted, that's the age-encrypted stream in `openQuestions.md`, not a hash.

## 2026-06-17 — Don't append to the events store and dedup at the seam

**Tried:** The handoff's model: re-parse the full transcript history every run, append, and keep transcripts forever so the history stays re-derivable.
**What broke:** Rejected at design time.
**Why we backed out:** A finished transcript is immutable, so re-parsing it forever is waste, and "keep everything forever" is a heavy retention requirement to carry. Keying the merge on session UUID removes the seam entirely, so the dedup problem the append design worried about never arises.
**Don't suggest:** Changing `merge_events` to append, or adding a dedup pass on top of it. Also don't suggest raising `cleanupPeriodDays` back up to preserve history, because the store is the archive now.

## 2026-06-17 — Don't set `cleanupPeriodDays` to 0

**Tried:** Reading `0` as "never clean up."
**What broke:** It disables transcript writing entirely, a known Claude Code bug and the exact opposite of the intent.
**Why we backed out:** Documented in the design (§6) and the README so nobody reaches for it.
**Don't suggest:** `0` for any retention setting here. 90 is the target, 30 if you're disk-conscious, 365 for margin. And never lower the setting before a `collect` has back-filled the store.

## 2026-06-17 — Don't capture usage with hooks

**Tried:** Proposed as a real-time alternative to a scheduled run.
**What broke:** Rejected at design time.
**Why we backed out:** Real-time isn't needed for a tool you read once a day, and hooks would mean a two-event capture plus a jq dependency. A scheduled run is simpler.
**Don't suggest:** Wiring `collect` into Claude Code hooks, `SessionEnd`, or any event-driven trigger.
