"""
Tests for main_condor.should_settle_on_expiry() and
main_directional_spread.should_settle_on_expiry().

Pinned 2026-08-04: both the condor and the directional spread had their
own contract expiry that day, and BOTH sat unsettled past market close --
the live process's last recorded cycle was 15:25/15:26, and the old
settlement condition (`not market_is_open()`, i.e. wait for 15:30) never
got a chance to fire before the process stopped.

First fix attempt used 15:15, based on NIFTY SPOT freezing solid at that
exact time in the recorded data. WRONG: checking actual OPTION premiums
in that same window showed they kept moving, sometimes violently (one
strike's premium fell 85% between 15:15 and 15:26), so 15:15 would have
settled on an arbitrary, still-volatile mid-window price. The real
structure, per NSE's published schedule: continuous cash trading ends
15:15 and the underlying goes into a closing auction (why spot froze --
not a dead feed), options get their own reactive window to ~15:40, and
15:40 is the true official F&O close. EXPIRY_SETTLEMENT_CUTOFF (15:40)
reflects that -- settlement fires at the real close, not the point
spot happened to stop updating.

Still an approximation: whether Dhan exposes a genuine settlement price
distinct from last-traded-price is unverified (the live endpoint only
responds during market hours) -- see BACKLOG.md.

Run: python -m pytest tests/ -q
"""

from datetime import date, datetime

import pytest

import main_condor as mc
import main_directional_spread as mds

MODULES = (mc, mds)


@pytest.mark.parametrize("mod", MODULES, ids=["condor", "directional_spread"])
def test_does_not_settle_before_cutoff_on_expiry_day(mod):
    expiry = date(2026, 8, 4)
    assert not mod.should_settle_on_expiry(expiry, now=datetime(2026, 8, 4, 15, 39, 59))


@pytest.mark.parametrize("mod", MODULES, ids=["condor", "directional_spread"])
def test_settles_at_cutoff_on_expiry_day(mod):
    expiry = date(2026, 8, 4)
    assert mod.should_settle_on_expiry(expiry, now=datetime(2026, 8, 4, 15, 40, 0))
    assert mod.should_settle_on_expiry(expiry, now=datetime(2026, 8, 4, 20, 0, 0))


@pytest.mark.parametrize("mod", MODULES, ids=["condor", "directional_spread"])
def test_does_not_settle_while_options_are_still_in_their_own_window(mod):
    """15:15-15:40 is exactly the window where spot is frozen (closing
    auction) but options are still genuinely trading -- must not settle
    here, that was the whole point of moving off 15:15."""
    expiry = date(2026, 8, 4)
    assert not mod.should_settle_on_expiry(expiry, now=datetime(2026, 8, 4, 15, 20, 0))
    assert not mod.should_settle_on_expiry(expiry, now=datetime(2026, 8, 4, 15, 35, 0))


@pytest.mark.parametrize("mod", MODULES, ids=["condor", "directional_spread"])
def test_does_not_settle_before_expiry_day_at_all(mod):
    expiry = date(2026, 8, 7)
    assert not mod.should_settle_on_expiry(expiry, now=datetime(2026, 8, 4, 23, 0, 0))


@pytest.mark.parametrize("mod", MODULES, ids=["condor", "directional_spread"])
def test_settles_on_any_later_day_regardless_of_time(mod):
    """Pins the 2026-08-04 incident directly: a process that comes back
    up on a LATER calendar day (after being down over the actual expiry)
    must settle immediately, not wait for that day's own cutoff time."""
    expiry = date(2026, 8, 4)
    assert mod.should_settle_on_expiry(expiry, now=datetime(2026, 8, 5, 9, 20, 0))
