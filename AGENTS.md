# AGENTS.md

Read `_working-memory/` before you change anything. It holds what this file won't repeat: [`decisionLog.md`](_working-memory/decisionLog.md) (why things are the way they are), [`conventions.md`](_working-memory/conventions.md) (naming, layout, error handling), [`antipatterns.md`](_working-memory/antipatterns.md) (what's been tried and failed), [`dataContracts.md`](_working-memory/dataContracts.md) (the shapes in the four streams), [`openQuestions.md`](_working-memory/openQuestions.md) (check before guessing).

The design docs in `docs/plans/` are the reasoning of record. Code comments, commits, and issues cite them by section number (`§4`). **If you're changing behavior a design doc describes, read the doc first.**

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install pytest   # no manifest; pytest comes from a venv you make
.venv/bin/python -m pytest                               # or `python3 -m pytest` if pytest is already on PATH
```

## Hard Rules

- **IMPORTANT: no usage data ever lands in this repo.** It's public and holds code only. Events live in a separate private repo, and `.gitignore` blocks `/events/` and the generated artifacts against an accidental `--data-dir=.`.
- **YOU MUST copy stored fields out by name.** Never spread a source dict into a stored row. `parse_spawns` sits next to `prompt`, `description`, and `content`, all of which carry client context in real transcripts. See `skill_usage.py:535`.
- **Stdlib only.** Adding a dependency is a decision to raise with the maintainer, not a convenience to reach for.
- **Python 3.9 is the floor**, worked around by hand in places (`datetime.fromisoformat` rejects a bare `Z` before 3.11; `rglob` has no symlink knob in 3.9).
- **One module.** `skill_usage.py` is the whole tool, divided by full-width `# ---...--- #` banner sections. Roughly 1,300 lines is the guard-rail, past which rendering splits out (`docs/plans/2026-07-14-agents-models-tokens-design.md` §10). New code goes inside a banner section, not at the bottom of the file.
- **Commits are conventional**, lowercase and imperative, scoped where there is one. `cliff.toml` sets `filter_unconventional = true`, so a subject that doesn't parse silently vanishes from the changelog.

## Where Things Are

- `skill_usage.py` — the tool. Sections: Locations, Transcript parsing, Skill inventory, MOC rendering, Report rendering, Audit, CLI glue.
- `tests/` — pytest, one module per unit of behavior. Fixtures are local `_helper()` factories inside each module; `tmp_path` for anything touching disk, never a real `~/.claude`.
- `scheduling/` — the launchd plist template and its installer.

## Patterns to Preserve

- Pure functions, thin commands. `cmd_*` resolves arguments, runs git, and prints; everything else takes data and returns data. That's what makes the pipeline testable without staging a fake `~/.claude`.
- Read every field with `.get()`. The transcript format shifts with each Claude Code release and the store has to stay readable across versions of this tool.
- Lenient input, strict output. Skip a bad JSON line, keep an unparseable timestamp, treat a missing file as empty. Write sorted and deterministic so diffs stay small.
- Git is best-effort. `_git` warns and returns `False`; the local write is the correctness boundary. Don't promote a git failure to a fatal error.
