"""
Unit tests for the events-store merge.

`collect` doesn't append blindly — it overlays. The store is a durable
accumulator keyed by session UUID, and a finished transcript is immutable, so a
session's events never change once written. That lets the merge replace each
re-read session's slice wholesale (idempotent, no double-count) while leaving
sessions that have since been pruned from disk exactly where they are. This is
what lets transcript retention drop to a short re-derive window without losing
history (design §3, §6).
"""

from skill_usage import merge_events


def _ev(skill, session, channel="tool", ts="2026-06-17T00:00:00.000Z"):
    return {
        "skill": skill,
        "timestamp": ts,
        "session": session,
        "host": "h",
        "channel": channel,
    }


def test_reingesting_the_same_session_does_not_double_count():
    # The session UUID is a natural idempotency key: re-reading a transcript that
    # is already in the store replaces its slice rather than appending a second
    # copy. Running collect twice must equal running it once.
    a1 = _ev("humanizer", "S")
    a2 = _ev("impeccable", "S", channel="slash")
    stored = [a1, a2]
    fresh = [a1, a2]  # same session, parsed again on a later run

    merged = merge_events(stored, fresh, found_sessions={"S"})

    assert merged == [a1, a2]


def test_session_in_store_but_pruned_from_disk_survives_a_collect():
    # The whole point of the store: once a session's events are recorded, they
    # outlive the transcript. Here OLD has aged off disk and only NEW is present
    # this run, so OLD isn't in found_sessions and must be carried through.
    old = _ev("humanizer", "OLD")
    new = _ev("impeccable", "NEW")
    stored = [old]
    fresh = [new]

    merged = merge_events(stored, fresh, found_sessions={"NEW"})

    assert old in merged and new in merged and len(merged) == 2


def test_a_found_session_with_zero_fresh_events_clears_its_old_slice():
    # Edge of the overlay: if a session is on disk but no longer yields events,
    # the fresh parse is still authoritative, so its stale stored slice is
    # dropped rather than resurrected. (Immutable transcripts make this rare, but
    # the overlay must honor disk, not the store.)
    stale = _ev("humanizer", "S")
    stored = [stale]
    fresh = []  # session S parsed this run but produced nothing

    merged = merge_events(stored, fresh, found_sessions={"S"})

    assert merged == []
