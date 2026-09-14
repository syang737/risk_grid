"""Synthetic book generator.

Shaped like a real carrying broker-dealer book rather than uniform random rows,
because the pivot cost depends on cardinality and skew, not just row count:
a few hundred underlyings, option chains that fan out across expiries and
strikes, and a long tail of small accounts under a few large desks.

Labels are built once per distinct value and then indexed, never formatted per
row -- at 5M positions the difference is tens of seconds.
"""

from __future__ import annotations

import numpy as np
import polars as pl

EXPIRY_DTES = np.array([1, 2, 7, 14, 30, 45, 60, 90, 180, 365, 730], dtype=float)

# Real sector names, matching the granularity the incumbent shows in its
# Product column -- a mix of broad sectors and narrower industry groups.
SECTORS = (
    "Industrials", "Financials", "Real Estate", "Metals & Mining",
    "Financial Services", "Healthcare", "Diversified Services", "Transportation",
    "Insurance", "Electronics", "Materials & Construction", "Retail",
    "Consumer Durables", "Consumer Cyclicals", "Basic Materials", "Drugs",
    "Banking", "Health Services", "Energy", "Technology", "Utilities",
    "Telecommunications", "Media", "Aerospace & Defense", "Chemicals",
)


def generate_book(
    n_positions: int,
    seed: int = 0,
    n_underlyings: int = 400,
    n_accounts: int = 5_000,
    n_master_accounts: int = 400,
    n_desks: int = 25,
) -> tuple[pl.DataFrame, np.ndarray]:
    """Return (positions, per-position daily sigma of the underlying)."""
    rng = np.random.default_rng(seed)
    n = n_positions

    # --- underlying universe -------------------------------------------------
    u_idx = rng.integers(0, n_underlyings, n)
    u_price = np.exp(rng.normal(3.6, 0.9, n_underlyings))
    u_vol = np.clip(rng.gamma(4.0, 0.075, n_underlyings), 0.08, 1.5)
    u_sigma_daily = u_vol / np.sqrt(252.0)
    u_label = np.array([f"U{i:04d}" for i in range(n_underlyings)])
    u_sector = np.array(SECTORS)[rng.integers(0, len(SECTORS), n_underlyings)]

    # --- instruments ---------------------------------------------------------
    is_option = rng.random(n) < 0.85
    is_call = rng.random(n) < 0.5

    spot = u_price[u_idx]
    # Strikes cluster around the money and thin out in the wings.
    moneyness = np.clip(rng.normal(1.0, 0.18, n), 0.3, 2.5)
    strike = np.maximum(np.round(spot * moneyness, 0), 0.5)

    dte = EXPIRY_DTES[rng.integers(0, len(EXPIRY_DTES), n)]
    # Per-position IV: underlying vol plus a smile-ish idiosyncratic spread.
    iv = np.clip(u_vol[u_idx] + rng.normal(0.0, 0.05, n), 0.03, 3.0)

    # --- account hierarchy: firm > desk > master account > account -----------
    # Pareto weights so a handful of accounts hold most of the book.
    ma_desk = rng.integers(0, n_desks, n_master_accounts)
    acct_ma = rng.integers(0, n_master_accounts, n_accounts)

    acct_weights = rng.pareto(1.2, n_accounts) + 1.0
    acct_weights /= acct_weights.sum()
    acct_idx = rng.choice(n_accounts, n, p=acct_weights)

    desk_label = np.array([f"DESK{i:02d}" for i in range(n_desks)])
    ma_label = np.array([f"MA{i:04d}" for i in range(n_master_accounts)])
    acct_label = np.array([f"ACCT{i:05d}" for i in range(n_accounts)])

    ma_of_row = acct_ma[acct_idx]

    qty = rng.integers(-500, 500, n).astype(float)
    qty[qty == 0] = 1.0

    df = pl.DataFrame({
        "position_id": np.arange(n, dtype=np.int64),
        "firm": np.full(n, "FIRM"),
        "desk": desk_label[ma_desk[ma_of_row]],
        "master_account": ma_label[ma_of_row],
        "account": acct_label[acct_idx],
        "sector": u_sector[u_idx],
        "underlying": u_label[u_idx],
        "instrument_type": np.where(is_option, "OPTION", "EQUITY"),
        "expiry": np.where(is_option, dte.astype(int).astype(str), "-"),
        "strike": np.where(is_option, strike, 0.0),
        "right": np.where(is_option, np.where(is_call, "C", "P"), "-"),
        "underlying_price": spot,
        "dte": np.where(is_option, dte, 0.0),
        "iv": np.where(is_option, iv, 0.0),
        "is_call": is_call,
        "is_option": is_option,
        "rate": np.full(n, 0.042),
        "div_yield": np.where(rng.random(n) < 0.4, rng.uniform(0.005, 0.04, n), 0.0),
        "qty": qty,
        "multiplier": np.where(is_option, 100.0, 1.0),
    })

    # Contract is the leaf drill level: the specific tradeable line.
    df = df.with_columns(
        pl.when(pl.col("is_option"))
        .then(
            pl.col("underlying") + " " + pl.col("expiry") + "D "
            + pl.col("strike").cast(pl.Int64).cast(pl.Utf8) + pl.col("right")
        )
        .otherwise(pl.col("underlying"))
        .alias("contract")
    )

    return df, u_sigma_daily[u_idx]
