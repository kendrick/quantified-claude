# Open Questions

<!-- Things that are unresolved and should not be guessed at. -->
<!-- Agents encountering these should ask rather than assume. -->

## Are the tool and slash channels really disjoint?

The 2026-06-17 design (§9) flagged this and asked for it to be settled before the two channels get summed: "confirm tool vs slash are disjoint events, not the same action recorded twice." The evidence leaned that way, since `speckit-plan` measured 34 slash against 0 tool, but the check was never written down as done. Meanwhile `render_moc` does sum them, so every Uses number in every MOC rendered so far depends on the answer. If they turn out to overlap, the fix is at render time and the store needs no migration. Someone should measure it properly and either record the result or fix the sum.

## Is the tool distributed as a template or as a clone?

The design (§2) calls the tool repo a public GitHub *template* repo, and its build order still lists "mark it a template" as outstanding. The README tells you to `git clone` it and `git pull` to update. Those are different models, and they imply different things about how someone else's fork tracks upstream. The repo is public today but isn't marked a template, so the README currently describes reality. Pick one on purpose rather than letting the design doc and the README keep disagreeing.

## Will per-project breakdowns ever be wanted?

`project` was dropped from events because it encoded client names and the MOC never used it (see `decisionLog.md`). The answer at the time was an explicit "maybe later," with an age-encrypted event stream documented as the fallback if that ever turns into a yes. Nothing has changed since. Adding the field back is a privacy decision, not an implementation detail, so don't reintroduce it without asking.

## Is per-record timing worth reconstructing concurrent runs from?

The module-2 probe found the timestamps dense enough to reconstruct overlapping agent runs, with 155 overlapping windows in the sample. It was deferred rather than declined: interesting, render-only, and not retention-critical, so the note says revisit after Phase 2 rendering ships. Still parked (`docs/plans/2026-07-14-agents-models-tokens-design.md` §7).
