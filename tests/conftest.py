"""Hermetic isolation fixtures for the WorkForce test suite.

Mirror worklane wl-282 pattern: tests must never touch the live Desk,
roster employment records, or fire real For You drops from daemon.tick.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hermetic_no_desk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable live desk writes for every test (capacity For You hook).

    Daemon.tick runs the wf-122 capacity hook with dry_run=False. Fixture
    vendor_limit ledgers (pool basename from command, e.g. ``sh``) can trip
    detect_capacity_alerts and would POST a real inbox card without this
    kill-switch — see false positive wf-128.

    Production daemon does not set WORKFORCE_NO_DESK; CLI --live path is
    unaffected. Rare integration tests may set WORKFORCE_ALLOW_DESK=1 to
    opt back into live desk (still needs a reachable desk mock).
    """
    monkeypatch.setenv("WORKFORCE_NO_DESK", "1")
    # Opt-in must not leak from the host environment into the suite.
    monkeypatch.delenv("WORKFORCE_ALLOW_DESK", raising=False)
