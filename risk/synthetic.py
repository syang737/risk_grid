"""Synthetic book generator.

Shaped like a real carrying broker-dealer book rather than uniform random rows,
because the pivot cost depends on cardinality and skew, not just row count:
a few hundred underlyings, option chains that fan out across expiries and
strikes, and a long tail of small accounts under a few large desks.
"""

from __future__ import annotations

import numpy as np
import polars as pl

EXPIRY_DTES = np.array([1, 2, 7, 14, 30, 45, 60, 90, 180, 365, 730], dtype=float)


def generate_book(n_positions: int, seed: int = 0, n_underlyings: int = 400,
                  n_accounts: int = 5_000, n_desks: int = 25) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    n = n_positions

    # Underlying universe: log-normal prices, vols clustered around 30%.
    u_idx = rng.integers(0, n_underlyings, n)
    u_price = np.exp(rng.normal(3.6, 0.9, n_underlyings))
    u_vol = np.clip(rng.gamma(4.0, 0.075, n_underlyings), 0.08, 1.5)
    u_sigma_daily = u_vol / np.sqrt(252.0)

    # ~85% options, which is what drives the repricing cost.
    is_option = rng.random(n) < 0.85
    is_call = rng.random(n) < 0.5

    spot = u_price[u_idx]
    # Strikes cluster around the money and thin out in the wings.
    moneyness = np.clip(rng.normal(1.0, 0.18, n), 0.3, 2.5)
    strike = np.round(spot * moneyness, 0)
    strike = np.maximum(strike, 0.5)

    dte = EXPIRY_DTES[rng.integers(0, len(EXPIRY_DTES), n)]
    # Per-position IV: underlying vol plus a smile-ish idiosyncratic spread.
    iv = np.clip(u_vol[u_idx] + rng.normal(0.0, 0.05, n), 0.03, 3.0)

    # Account hierarchy: skewed so a few desks hold most of the book.
    desk = rng.integers(0, n_desks, n_accounts)
    acct_weights = rng.pareto(1.2, n_accounts) + 1.0
    acct_weights /= acct_weights.sum()
    acct_idx = rng.choice(n_accounts, n, p=acct_weights)

    qty = rng.integers(-500, 500, n).astype(float)
    qty[qty == 0] = 1.0

    return pl.DataFrame({
        "position_id": np.arange(n, dtype=np.int64),
        "firm": np.full(n, "FIRM", dtype=object),
        "desk": [f"DESK{d:02d}" for d in desk[acct_idx]],
        "account": [f"ACCT{a:05d}" for a in acct_idx],
        "underlying": [f"U{u:04d}" for u in u_idx],
        "instrument_type": np.where(is_option, "OPTION", "EQUITY"),
        "expiry": np.where(is_option, dte.astype(int).astype(str), "-"),
        "strike": np.where(is_option, strike, 0.0),
        "underlying_price": spot,
        "dte": np.where(is_option, dte, 0.0),
        "iv": np.where(is_option, iv, 0.0),
        "is_call": is_call,
        "is_option": is_option,
        "rate": np.full(n, 0.042),
        "div_yield": np.where(rng.random(n) < 0.4, rng.uniform(0.005, 0.04, n), 0.0),
        "qty": qty,
        "multiplier": np.where(is_option, 100.0, 1.0),
    }), u_sigma_daily[u_idx]
