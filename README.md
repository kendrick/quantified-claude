# quantified-claude

Which Claude Code skills do you actually use? Not which ones you installed — which ones you *reach for*. This answers that, counted from the session transcripts already sitting on your disk.

It reads your transcripts, tallies every skill invocation, and writes a Map of Content (MOC): a Markdown table of your skills sorted by how often you've used them, split by how you invoked each one, with the ones you've never touched called out separately. Drop it in an Obsidian vault or just read it in any Markdown viewer.

Skill usage is the first thing it measures. The name is deliberately broad, so other facets can slot in later.

## What You Get

A file like this (counts are real, from one machine):

```
## Used & Owned

| Skill | Uses | Tool | Slash | Last used | Description |
|---|---:|---:|---:|---|---|
| humanizer | 96 | 96 | 0 | 2026-06-17 | Remove signs of AI-generated writing |
| systematic-debugging | 15 | 15 | 0 | 2026-06-17 | Find root cause before proposing fixes |
| brainstorming | 9 | 9 | 0 | 2026-06-17 | Use before any creative work |

## Owned but Never Used

- canvas-design — Design on an infinite canvas
- frontend-slides — Build slide decks in the browser

## Used but Not in Your Skills Dir
*(built-in skills, plugin skills, or renamed/removed ones)*

- speckit-specify — 30 uses (last 2026-06-12)
```

The **Tool** and **Slash** columns are the interesting part. Tool counts the times Claude decided to invoke a skill on its own; Slash counts the times you typed `/skillname` yourself. A skill that's all Tool is one Claude leans on for you; a skill that's all Slash is one you drive by hand.

## How It Works

Three pieces, each with its own lifecycle, each kept where that lifecycle fits:

- **The tool** — this repo. Code, public, shareable. You `git pull` to update it.
- **The events** — your usage data, in a *separate, private* repo. One small JSON-lines file per machine.
- **The MOC** — the rendered Markdown, regenerated locally wherever your skills live. Never synced; it's derived, so each machine just rebuilds its own.

Keeping the code and the data in different repos makes the public/private boundary structural instead of something you have to remember. The code has nothing sensitive in it. The data never has to be public.

Two subcommands do the work:

- `collect` parses this machine's transcripts into usage events and merges them into `events/<hostname>.jsonl` in your private data repo, keyed by session ID so re-runs never double-count. Then it commits and pushes. **Running collect is how machines sync** — each one only ever writes its own file, so there's nothing to conflict.
- `render` pulls every machine's events, unions them, joins against the skills installed on *this* machine, and writes the MOC. It runs a `collect` for the current machine first, so the output is current the moment you generate it.

If you work across several machines, each one collects its own usage, and any machine's MOC reflects all of them — they all render from the same unioned events.

## Setup

You need Python 3.9+ (standard library only, no dependencies) and a private repo to hold your events.

**1. Get the tool.**

```bash
git clone https://github.com/<you>/quantified-claude.git ~/repos/quantified-claude
```

**2. Make a private events repo.** Anywhere private — a private GitHub repo you've cloned locally is fine.

```bash
git init ~/repos/quantified-claude-events
```

**3. Point the tool at it.** Set the path once in your shell profile so you don't have to pass `--data-dir` every time:

```bash
export SKILL_MOC_DATA_DIR=~/repos/quantified-claude-events
```

**4. Back-fill your history, then render.**

```bash
python3 ~/repos/quantified-claude/skill_usage.py collect
python3 ~/repos/quantified-claude/skill_usage.py render
```

The MOC lands at `~/.claude/skills/skill-usage-moc.md` by default. That's it.

## Everyday Use

```bash
# Re-render whenever you want a fresh picture (auto-collects first):
python3 skill_usage.py render

# Only count usage since a date:
python3 skill_usage.py render --since 2026-01-01

# Write the MOC somewhere else:
python3 skill_usage.py render --out ~/vault/40-MOCs/claude-skills.md

# Use [[wikilinks]] instead of relative links (for an Obsidian vault):
python3 skill_usage.py render --links wikilink

# Dump the raw events as JSON instead of a MOC:
python3 skill_usage.py render --json
```

`collect` and `render` both take `--data-dir` (if you'd rather not use the env var) and `--host` (to override the hostname stamped on events).

## Keeping It Current

You don't want to run this by hand forever. On a Mac, a `launchd` job that runs `render` once a day keeps the MOC fresh and your events synced without you thinking about it. (A plist to do that is on the to-do list.)

On Linux or WSL, scheduling is fussier, but you don't strictly need a scheduler: because `render` auto-collects and the whole thing is idempotent, running `render` whenever you happen to think of it counts and syncs that machine. A WSL box that participates only occasionally still shows up correctly.

## A Note on Transcript Retention

Claude Code prunes old session transcripts on a schedule you control (`cleanupPeriodDays`). Once you've back-filled, your usage history lives in the events store — small, versioned, committed, synced — not in the raw transcripts. So you don't have to keep transcripts forever to keep your counts.

What retention *does* govern is the re-derive window: how far back you can re-parse if you ever fix a bug in how skills are counted. 90 days is a good default — long enough to notice a problem and re-run, short enough that disk stays small.

**Sequencing matters.** Don't lower `cleanupPeriodDays` until after your first `collect` has back-filled the existing history into the events store. Until the store holds your back-catalog, the transcripts are the only record. The order is: collect once, confirm the events landed, then lower the setting. And don't set it to `0` — that disables transcript writing entirely, which is the opposite of what you want.

## Privacy

The events carry session IDs, timestamps, skill names, and hostnames. No file paths, no project names, no prompt content — nothing that identifies what you were working on or for whom. Keep the events repo private anyway, as defense in depth. The tool repo is code only, so there's nothing to scrub before sharing it.

## Design

The full design and the reasoning behind the three-artifact split live in [docs/plans/2026-06-17-skill-usage-moc-design.md](docs/plans/2026-06-17-skill-usage-moc-design.md).
