"""Polymarket CLOB momentum trading bot (educational).

This package is intentionally small and dependency-light so beginners can read
it top to bottom. The important modules:

- ``config``   — load/validate settings from environment variables.
- ``market_data`` — public Gamma + CLOB HTTP helpers (no wallet needed).
- ``strategy`` — 20-period moving-average momentum signal.
- ``risk``     — risk limits, kill switch, position bookkeeping.
- ``trader``   — thin wrapper around py-clob-client-v2 with dry-run support.
- ``main``     — the run loop that ties everything together.
"""

__version__ = "0.1.0"
