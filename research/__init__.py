"""
Research and measurement code. NOTHING HERE TRADES.

Everything in this package is investigation: strategies that were
tested and not adopted, the tooling that tested them, and one-off
re-run harnesses. No live process imports any of it, and
`automation/start_trading.ps1` does not launch any of it -- that is
the point of the package existing, so a glance at the import list of a
live process is enough to know what actually runs.

Kept in the repo rather than deleted because the code IS the evidence:
"ORB was tested across 16 variants and 1,506 sessions and none beat a
coin flip" is only a durable finding while the thing that produced it
can still be re-run and checked. See README.md's "Not adopted" entries
for the write-ups.

Modules import each other absolutely (`from research import orb`) and
import project-root modules normally (`import config`), so run them
from the project root as modules:

    python -m research.orb_study
    python -m research.orb_candle_cache --describe
    python -m research.gamma_exposure_study --horizon 30
    python -m research.rerun_with_fixed_candles --study component
"""
