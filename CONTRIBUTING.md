# Contributing

This is a personal tool that happens to be public. Issues and pull requests are welcome. The constraints below are the ones the code already holds itself to, and they're the difference between a change that lands and one that stalls in review.

## Before You Change Behavior

The design docs in `docs/plans/` are the reasoning of record, and the code is downstream of them. If you're changing something a doc describes, read the doc first; code comments, commit messages, and issues all cite it by section number (`§4`).

`_working-memory/` is the rest of the context, and it's written for humans as much as for agents:

| File | What it holds |
|---|---|
| [`decisionLog.md`](_working-memory/decisionLog.md) | Why things are the way they are. Append-only, newest first. |
| [`conventions.md`](_working-memory/conventions.md) | Naming, file layout, code patterns, error handling. |
| [`antipatterns.md`](_working-memory/antipatterns.md) | What's already been tried and didn't work. Check here before proposing a refactor or a library swap. |
| [`dataContracts.md`](_working-memory/dataContracts.md) | The shapes flowing through the four streams. |
| [`openQuestions.md`](_working-memory/openQuestions.md) | Unresolved ambiguity. Check here before guessing. |

## Setting Up

There's no manifest and nothing to install. Make a virtualenv, add pytest, run the suite from the repo root:

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest
```

`.venv/` is gitignored. Python 3.9 is the floor, and the code works around it by hand in a few places, so don't reach for syntax newer than that.

## The Constraints Worth Knowing

- Stdlib only — adding a dependency is a decision to raise in an issue, not a convenience to reach for. It's there for readability; the tool is small enough that a dependency would cost more than it saves.
- One module — the whole tool is `skill_usage.py`, divided by full-width banner comments. The guard-rail is roughly 1,300 lines, past which rendering splits out. Until then, resist splitting for tidiness, and put new code inside an existing banner section or under a new one.
- Pure functions, thin commands — real logic goes in a function that takes data and returns data. The `cmd_*` functions resolve arguments, run git, and print. That's what keeps the pipeline testable without staging a fake `~/.claude`.
- Nothing private lands in this repo — usage data lives in a separate private repo, and `.gitignore` blocks `/events/` and the generated artifacts so an accidental `--data-dir=.` can't publish your telemetry.
- Store fields are copied out by name — never spread a source dict into a stored row. A field is stored because someone chose it, and the privacy stance depends on that staying true.

## Tests

Every change to parsing or rendering needs a test. Conventions the suite already follows:

- Module docstrings explain the stakes: what breaks in the real world if this behavior regresses, usually with a design-doc citation.
- Test names are full sentences describing the guarantee, like `test_session_in_store_but_pruned_from_disk_survives_a_collect`.
- Fixtures are small local `_helper()` factories in the test module itself, using real record shapes observed in live transcripts, trimmed to the fields the parser reads.
- Anything touching the filesystem uses `tmp_path`. Never point a test at a real `~/.claude` or a real data repo.

## Commits

Conventional commits, with a scope where there is one: `feat(skill-usage):`, `fix(collect):`. Subject lines are lowercase and imperative. Bodies explain why.

`cliff.toml` sets `filter_unconventional = true`, so a subject that doesn't parse vanishes from the changelog without warning. `chore`, `ci`, and `style` are dropped from the changelog by design.

## Comments

Comment proactively, but only where the comment carries its weight. The comments in this codebase explain the version skew that forced a defensive walk, the real-data false positive a filter guards against, the Python 3.9 floor behind a workaround. Match that standard. A comment restating what the code does is noise; a comment explaining what the code can't say for itself is the whole point.
