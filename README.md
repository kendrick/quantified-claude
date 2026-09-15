# quantified-claude

Which Claude Code skills do you actually use? Not which ones you installed—which ones you _reach for_.

`quantified-claude` answers that, along with three more questions like it: which subagents you spawn, which models run where, and what all of it costs in tokens. Every number comes from the session transcripts already sitting on your disk, so there's nothing to instrument and nothing to wait for.

It reads those transcripts and writes two Markdown files. One is a Map of Content (MOC) of your skills, sorted by how often you've used them. The other is a usage report covering models, agents, and token spend. Drop them in an Obsidian vault or read them in any Markdown viewer.

Skills were the first thing it measured; agents, models, and tokens came next. The name is deliberately broad, so other facets can slot in later.

- [What You Get](#what-you-get)
- [How It Works](#how-it-works)
- [Setup](#setup)
- [Everyday Use](#everyday-use)
- [Keeping It Current](#keeping-it-current)
- [A Note on Transcript Retention](#a-note-on-transcript-retention)
- [Privacy](#privacy)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)
- [Design](#design)

## What You Get

Two files, regenerated side by side every time you render. Counts below are real, from one machine.

### The Skills MOC

`~/.claude/skills/skill-usage-moc.md`, a map of every skill you own crossed with how much you use it.

```
## Used & Owned

| Skill              | Uses | Tool | Slash | Tokens | Last used  | Description                      |
|--------------------|-----:|-----:|------:|-------:|------------|----------------------------------|
| humanizer          |  887 |  886 |     1 |   2.1B | 2026-09-15 | Rewrite AI-sounding text…        |
| technical-writing  |  326 |  326 |     0 |   198M | 2026-09-15 | Layered prose standard…          |
| handoff            |  230 |    8 |   222 |   224M | 2026-09-15 | Save a session handoff…          |

## Owned but Never Used

- canvas-design — Design on an infinite canvas
- frontend-slides — Build slide decks in the browser

## Used but Not in Your Skills Dir
*(built-in skills, plugin skills, or renamed/removed ones)*

- speckit-specify — 30 uses (last 2026-06-12)
```

The **Tool** and **Slash** columns are the interesting part. Tool counts the times Claude decided to invoke a skill on its own; Slash counts the times you typed `/skillname` yourself. A skill that's all Tool is one Claude leans on for you; a skill that's all Slash is one you drive by hand. Compare `humanizer` against `handoff` above.

**Tokens** is every token spent on messages Claude Code attributed to that skill, all four buckets summed. Cache reads dominate it, so read the column as relative weight rather than as a bill.

### The Usage Report

`~/.claude/skills/claude-usage-report.md`, the same events sliced by model, agent, and cost. Five sections. This is the first.

```
## Models × Thread

| Model     | Main msgs | Main tokens | Agent msgs | Agent tokens | Total |
|-----------|----------:|------------:|-----------:|-------------:|------:|
| opus-5    |     33981 |        8.5B |      21864 |         2.0B |   11B |
| opus-4-8  |     18923 |        6.7B |       4538 |         277M |  7.0B |
| sonnet-5  |       683 |        164M |      11655 |         1.1B |  1.3B |
| haiku-4-5 |         1 |         30k |       3345 |         130M |  130M |
```

The main thread and the subagents it spawns don't run the same model, and that split is invisible anywhere else. The other four sections:

- Agent Types — spawns, completion rate, median tokens, and lines added and removed, per agent type
- Tokens by Skill — with the model mix behind each one
- Cache Economics — cache read against cache creation, per model
- Skills × Agents — which skills fire inside which subagents rather than on the main thread

Sections whose stream is empty print a note instead of failing, so a fresh host renders something on day one.

## How It Works

Three pieces, each with its own lifecycle, each kept where that lifecycle fits:

- **The tool**—this repo. Code, public, shareable. You `git pull` to update it.
- **The events**—your usage data, in a _separate, private_ repo. Four small directories of JSON Lines, one file per machine in each.
- **The artifacts**—the rendered MOC and report, regenerated locally wherever your skills live. Never synced; they're derived, so each machine just rebuilds its own.

Keeping the code and the data in different repos makes the public/private boundary structural instead of something you have to remember. The code has nothing sensitive in it. The data never has to be public.

### The Four Streams

Every stream partitions by host and keys on the same session UUID, so re-parsing a transcript replaces that session's slice in all four at once.

| Stream        | File                  | One row per                | Feeds                 |
| ------------- | --------------------- | -------------------------- | --------------------- |
| Skills        | `events/<host>.jsonl` | skill invocation           | the MOC               |
| Tokens        | `usage/<host>.jsonl`  | transcript × model × skill | both files            |
| Agents        | `agents/<host>.jsonl` | subagent run               | the report            |
| Session stats | `stats/<host>.jsonl`  | transcript file            | `--json` only, so far |

### The Subcommands

- `collect` parses this machine's transcripts into all four streams and merges them into the private data repo, keyed by session ID so re-runs never double-count. Then it commits and pushes. **Running collect is how machines sync**—each one only ever writes its own files, so there's nothing to conflict.
- `render` pulls every machine's events, unions them, joins against the skills installed on _this_ machine, and writes the MOC and the report. It runs a `collect` for the current machine first, so the output is current the moment you generate it.
- `audit` cross-checks the streams against each other and prints what disagrees: skill events that landed in the `<unknown>` bucket, usage rows with no model, spawns whose agent file was never seen, models that resolved to something other than what was requested. Read-only, always exits 0. Run it after a Claude Code version bump, when a parser is most likely to have stopped matching the transcript format without saying so.

If you work across several machines, each one collects its own usage, and any machine's MOC reflects all of them—they all render from the same unioned events.

## Setup

You need Python 3.9+ (standard library only, no dependencies) and a private repo to hold your events.

**1. Get the tool.**

```bash
git clone https://github.com/kendrick/quantified-claude.git ~/repos/quantified-claude
```

**2. Make a private events repo.** A private GitHub repo you've cloned locally is the intended shape.

```bash
git init ~/repos/quantified-claude-events
```

> [!IMPORTANT]
> Make it a real git repo with a remote you can push to, even if you only ever run one machine. The git steps are best-effort: if the directory isn't a repo, or has no remote, `collect` still writes the JSONL files locally and prints `! git push skipped: …` to stderr, then exits 0. It looks like it worked, because locally it did. Nothing syncs, and a second machine unions against events it can never see.

**3. Point the tool at it.** Set the path once in your shell profile so you don't have to pass `--data-dir` every time:

```bash
export SKILL_MOC_DATA_DIR=~/repos/quantified-claude-events
```

On a machine whose hostname moves with the network—a laptop taking a DHCP suffix—pin the store's partition key too, or that one laptop ends up with two events files:

```bash
export SKILL_MOC_HOST=work-laptop
```

**4. Back-fill your history, then render.**

```bash
python3 ~/repos/quantified-claude/skill_usage.py collect
python3 ~/repos/quantified-claude/skill_usage.py render
```

```
collect: 2598 events in store for work-laptop
render: wrote MOC -> /Users/you/.claude/skills/skill-usage-moc.md
render: wrote report -> /Users/you/.claude/skills/claude-usage-report.md
```

That's it.

## Everyday Use

```bash
# Re-render whenever you want a fresh picture (auto-collects first):
python3 skill_usage.py render

# Only count usage since a date:
python3 skill_usage.py render --since 2026-01-01

# Write the two files somewhere else:
python3 skill_usage.py render --out ~/vault/40-MOCs/claude-skills.md \
                              --report-out ~/vault/40-MOCs/claude-usage.md

# Use [[wikilinks]] instead of relative links (for an Obsidian vault):
python3 skill_usage.py render --links wikilink

# Dump all four raw streams as JSON instead of the Markdown:
python3 skill_usage.py render --json

# Check the streams against each other after a Claude Code upgrade:
python3 skill_usage.py audit
```

`collect` and `render` both take `--data-dir` (if you'd rather not use the env var) and `--host` (to override the hostname stamped on events). `audit` takes `--data-dir` and nothing else, because it reads the store without touching it or git.

## Keeping It Current

You don't want to run this by hand forever. On a Mac, point the installer at your events repo and it sets up a `launchd` job that renders once a day, which keeps both files fresh and this machine's events synced:

```bash
scheduling/install.sh ~/repos/quantified-claude-events
```

That fills in the plist template, writes the agent to `~/Library/LaunchAgents`, and loads it; logs land in `~/Library/Logs/quantified-claude/`. Pass a second argument to pin the host key, since launchd never reads your shell profile and won't see `SKILL_MOC_HOST`. Edit `scheduling/com.quantified-claude.skill-usage.plist` if you want a time other than 10:05.

On Linux or WSL, scheduling is fussier, but you don't strictly need a scheduler: because `render` auto-collects and the whole thing is idempotent, running `render` whenever you happen to think of it counts and syncs that machine. A WSL box that participates only occasionally still shows up correctly.

## A Note on Transcript Retention

Claude Code prunes old session transcripts on a schedule you control (`cleanupPeriodDays`). Once you've back-filled, your usage history lives in the events store—small, versioned, committed, synced—not in the raw transcripts. So you don't have to keep transcripts forever to keep your counts.

What retention _does_ govern is the re-derive window: how far back you can re-parse if you ever fix a bug in how skills are counted. 90 days is a good default—long enough to notice a problem and re-run, short enough that disk stays small.

**Sequencing matters.** Don't lower `cleanupPeriodDays` until after your first `collect` has back-filled the existing history into the events store. Until the store holds your back-catalog, the transcripts are the only record. The order is: collect once, confirm the events landed, then lower the setting. And don't set it to `0`—that disables transcript writing entirely, which is the opposite of what you want.

The same sequencing applies every time a new stream ships. The agent, token, and session-stats streams were added after the skills stream, and they could only ever back-fill as far as the transcripts still on disk reached. A stream added today starts from your current retention window, not from your first session.

## Privacy

The parsers copy every stored field out by name. Nothing lands in a row because it happened to sit in the source record.

What lands in the store: session IDs, timestamps, hostnames, skill names, model IDs, token counts, subagent types, tool names and their error counts, and a deduplicated count of PRs opened. What never does: file paths, project names, prompt text, agent prompts and descriptions, PR URLs and repository names.

The parsers enforce that, so it doesn't rest on your vigilance. Keep the events repo private anyway, as defense in depth. The tool repo is code only, so there's nothing to scrub before sharing it.

## Development

There's nothing to install and nothing to build. Run the suite from the repo root:

```bash
python3 -m pytest
```

`pytest.ini` points at `tests/`, and `tests/conftest.py` puts the repo root on `sys.path`, so the tests import `skill_usage` without an install step. Every fixture is a synthetic transcript record built inside the test module that uses it. Nothing in the suite reads your real `~/.claude`.

## Contributing

Issues and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers the commit format, the test conventions, the constraints worth keeping, and what to read before changing behavior a design doc describes.

The short version: `_working-memory/` is the onboarding material. [`decisionLog.md`](_working-memory/decisionLog.md) says why things are the way they are, [`conventions.md`](_working-memory/conventions.md) says how we do things here, and [`antipatterns.md`](_working-memory/antipatterns.md) says what's already been tried and didn't work.

## License

MIT. See [LICENSE](LICENSE).

## Design

The design docs are the reasoning of record, and the code is downstream of them:

- [The three-artifact split and the skills stream](docs/plans/2026-06-17-skill-usage-moc-design.md)
- [Agents, models, and tokens](docs/plans/2026-07-14-agents-models-tokens-design.md), including what the transcripts actually carry and why tokens have to be deduped by message
