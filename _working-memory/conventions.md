# Conventions

<!-- Project-specific patterns agents must follow. -->
<!-- This is the "how we do things here" file. -->

## Naming

- Module and file names use underscores (`skill_usage.py`). The original `skill-usage-moc.py` was renamed in commit 818578e both because the name undersold half the tool and because the underscore form is importable, which the test suite needs.
- Functions are `snake_case` and named for what they produce rather than how: `collect_from_disk`, `union_stores`, `render_moc`, `canonical_skill`.
- A leading underscore marks a helper nobody outside the module should call, like `_git` and `_before_cutoff`. Test helpers follow the same rule (`_ev`, `_skill`, `_write`).
- CLI entry points are `cmd_<subcommand>`; the pure data flow underneath is `run_<subcommand>`. That pairing is the seam described under Code Patterns.
- Module-level constants are `SCREAMING_SNAKE`, and most sit inside the banner section that uses them (`FRONTMATTER_RE` under Skill inventory, `DEFAULT_OUT` under CLI glue) rather than piling up at the top. The parsing constants under Locations are the exception, not the pattern to copy.

## File Organization

- One module. The whole tool is `skill_usage.py`, and the guard-rail is roughly 1,300 lines, past which rendering splits out. Until then, resist the urge to split for tidiness.
- The module is divided by banner comments, each a full-width `# ---...--- #` rule with a title. The current sections are Locations, Transcript parsing, Skill inventory, MOC rendering, and CLI glue. New code goes inside an existing section or gets a new banner; don't just append to the bottom.
- Tests live in `tests/`, one module per unit of behavior, named `test_<thing>.py` after the function or layer under test.
- Design docs go in `docs/plans/` as `YYYY-MM-DD-<slug>-design.md`. They're the reasoning of record, and code comments, commit messages, and issues all cite them by section number (`§4`).

## Code Patterns

- Pure functions, thin commands. Every piece of real logic is a function that takes data and returns data: `parse_events`, `merge_events`, `render_moc`, `canonical_skill`. The `cmd_*` functions do nothing but resolve arguments, run git, and print. That's what makes the pipeline testable without staging a fake `~/.claude` tree, and it's the pattern to preserve when adding a subcommand.
- Dependencies are injectable at the seam. `parse_events` takes `builtins`, `run_render` takes `inventory`, and everything filesystem-facing takes an explicit root path. Tests pass fixtures; production passes the defaults.
- Read every field with `.get()`. The store has to stay readable across versions of this tool, and the transcript format shifts across versions of Claude Code, so assume any key can be absent.
- Be lenient about input and strict about output. Bad JSON lines are skipped, unparseable timestamps are kept rather than dropped, missing files read as empty. What gets written is sorted and deterministic so diffs stay small.
- Stdlib only. Adding a dependency is a decision to raise, not a convenience to reach for.

## Comments

The comments here carry unusual weight and are worth reading before editing anything. They explain why: the version skew that forced a defensive walk, the real-data false positive a filter guards against, the Python 3.9 floor behind a workaround. Several cite a design-doc section by number. Match that standard. A comment restating what the code does is noise; a comment explaining what the code can't say for itself is the whole point.

## Tests

- pytest, run with `python3 -m pytest` from the repo root. There's no manifest, so pytest comes from a virtualenv you create yourself (`.venv/` is gitignored) or from your environment.
- Every test module opens with a docstring explaining the stakes: what breaks in the real world if this behavior regresses, usually with a design-doc citation. All eight current modules do this, so it's a convention rather than a coincidence.
- Test names are full sentences describing the guarantee, like `test_session_in_store_but_pruned_from_disk_survives_a_collect` and `test_command_name_mention_in_assistant_prose_is_not_counted`.
- Fixtures are built by small local `_helper()` factories at the top of the module, using the real record shapes observed in live transcripts, trimmed to the fields the parser reads.
- Inline comments in tests explain the invariant being pinned, not the mechanics of the assertion.
- Use `tmp_path` for anything that touches the filesystem. Never point a test at a real `~/.claude` or a real data repo.

## Error Handling

- Sync is best-effort; the local write is the correctness boundary. Every git call goes through `_git`, which warns and returns `False` on failure rather than raising. By the time git runs the store is already written, and the merge is idempotent, so the next run picks up where this one left off. Don't promote a git failure to a fatal error.
- Missing input is normal rather than exceptional. An absent store file is an empty store; an absent skills directory is skipped.
- User error exits loudly and early. `resolve_data_dir` and `parse_since` call `sys.exit` with a message telling you what to pass. There is no default data directory on purpose, because a wrong guess would write telemetry into the wrong repo.
- Diagnostics go to stderr so `--json` output stays clean on stdout.

## Commits

Conventional commits, enforced in spirit by `cliff.toml` (`filter_unconventional = true`, so anything that doesn't parse silently vanishes from the changelog). Use a scope where there is one: `feat(skill-usage):`, `fix(skill-usage):`. `chore`, `ci`, and `style` are dropped from the changelog by design. Subject lines are lowercase and imperative, and bodies explain why.
