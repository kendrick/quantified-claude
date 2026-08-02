# Project Overview

## What This Is

A personal telemetry tool that counts which Claude Code skills you actually reach for, read retroactively out of the session transcripts already sitting on your disk, and rendered as an Obsidian-friendly Map of Content.

The name is broader than the tool on purpose. Skill usage is module 1, and it's what exists. Module 2 (agents, models, tokens) is designed and filed as issues, but no code has been written for it. Source: `README.md`, `docs/plans/2026-06-17-skill-usage-moc-design.md` §1.

## Stack

- Language: Python 3.9+, standard library only. There is no manifest and no lockfile, so there's nothing to install; `skill_usage.py` runs directly under `python3`.
- Tests: pytest, wired by `pytest.ini` (`testpaths = tests`). `tests/conftest.py` puts the repo root on `sys.path` so test modules can `from skill_usage import ...` without a package install.
- Data layer: JSON Lines, one file per host, living in a *separate private git repo*. Git is the sync mechanism. No database, no server, no API.
- Deployment: a macOS `launchd` agent, installed by `scheduling/install.sh`. Linux and WSL are documented in the README rather than shipped.
- Changelog: git-cliff over conventional commits, configured in `cliff.toml`.

## Repository Structure

- `skill_usage.py` — the whole tool. One module, two subcommands (`collect`, `render`), divided into banner-comment sections.
- `tests/` — the pytest suite, roughly one module per unit of `skill_usage.py`.
- `docs/plans/` — dated design docs. These are the reasoning of record and the code is downstream of them, so read the doc before changing behavior it describes.
- `scheduling/` — the launchd plist template plus its installer.
- `_working-memory/` — this directory.

## Key Constraints

- This repo is public and holds code only. Usage data lives in a separate private repo and must never land here. `.gitignore` blocks `/events/` and the generated MOC specifically to survive an accidental `--data-dir=.` pointed at the repo root.
- Pure stdlib is a readability constraint, not an accident of scope. Adding a dependency is a decision to raise, not a convenience to reach for.
- Python 3.9 is the floor, and the code works around it by hand. `datetime.fromisoformat` didn't accept a bare `Z` until 3.11, and `rglob`'s symlink-following knob doesn't exist in 3.9.
- One file until roughly 1,300 lines. The module-2 design sets that guard-rail, past which rendering splits into its own module (`docs/plans/2026-07-14-agents-models-tokens-design.md` §10).
- The transcripts are a wasting asset. Claude Code prunes them at `cleanupPeriodDays: 90`, so anything derivable only from raw transcripts has to be collected before the window closes. That's why module 2's build order front-loads collection over rendering.
