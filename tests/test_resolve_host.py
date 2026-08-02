"""
Unit tests for the store's host key.

`host` names the file a machine writes (events/<host>.jsonl) and the overlay
merges per file, so the key has to mean "this laptop" for as long as the store
lives. socket.gethostname() doesn't promise that: a Mac takes its suffix from
whatever network handed out the lease, so the same machine can answer
"name.lan" today and "name.local" tomorrow. That drift wouldn't lose events, but
it would start a second file and make one laptop render as two.
"""

import socket

from skill_usage import resolve_host


def test_explicit_argument_wins(monkeypatch):
    # --host is the last word: an operator pinning the key by hand shouldn't have
    # to also clear the environment.
    monkeypatch.setenv("SKILL_MOC_HOST", "from-env")
    assert resolve_host("C02J9H8WQ6L4") == "C02J9H8WQ6L4"


def test_environment_is_used_when_no_argument_is_given(monkeypatch):
    # The everyday path. Set it once in a shell profile (and in the launchd
    # plist, which can't read that profile) and every run agrees.
    monkeypatch.setenv("SKILL_MOC_HOST", "C02J9H8WQ6L4")
    assert resolve_host(None) == "C02J9H8WQ6L4"


def test_hostname_is_the_fallback(monkeypatch):
    # Unset environment falls back to the hostname, which is right on a machine
    # whose hostname is already stable. Nothing changes for existing hosts.
    monkeypatch.delenv("SKILL_MOC_HOST", raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: "F61GDXFM2W")
    assert resolve_host(None) == "F61GDXFM2W"


def test_an_empty_environment_value_does_not_win(monkeypatch):
    # `export SKILL_MOC_HOST=` in a profile is a plausible slip. An empty key
    # would write to "events/.jsonl", so treat it as unset rather than honoring
    # it literally.
    monkeypatch.setenv("SKILL_MOC_HOST", "")
    monkeypatch.setattr(socket, "gethostname", lambda: "F61GDXFM2W")
    assert resolve_host(None) == "F61GDXFM2W"
