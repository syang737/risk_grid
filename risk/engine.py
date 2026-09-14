"""Builds the position x scenario P&L matrix.

This is the load-bearing idea of the whole system. Shock P&L is independent per
position, so it can be computed once into an (N, S) matrix. Every pivot the
user then drags is a groupby-sum over that matrix -- no repricing, no
recomputation. Drilling from firm level into a single account is the same
operation at a different grain.

The matrix is built in chunks so peak memory stays bounded regardless of book
size: a full-book temporary at (N, S) in float64 would be 8x the stored matrix.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import os

import numpy as np
import polars as pl

from .pricing import american_price, black_scholes, black_scholes_greeks
from .scenarios import ScenarioGrid

# Tuned by spikes/perf_spike.py. The win is cache residency, not parallelism:
# at 50 scenarios an 8k-row chunk keeps the float64 temporaries near 3MB, which
# fits L3. Chunks of 250k measured 3-4x slower at identical thread counts.
DEFAULT_CHUNK = 8_000
DEFAULT_THREADS = min(8, os.cpu_count() or 1)

REQUIRED_COLUMNS = (
    "underlying_price", "strike", "dte", "rate", "div_yield",
    "iv", "is_call", "qty", "multiplier", "is_option",
)


def _columns(positions: pl.DataFrame) -> dict[str, np.ndarray]:
    missing = [c for c in REQUIRED_COLUMNS if c not in positions.columns]
    if missing:
        raise ValueError(f"positions is missing required columns: {missing}")
    return {c: positions[c].to_numpy() for c in REQUIRED_COLUMNS}


def base_valuation(positions: pl.DataFrame) -> pl.DataFrame:
    """Price and greeks at the current snapshot, in position (not per-unit) terms.

    Equity rows carry delta 1.0 per share and no other greeks.
    """
    c = _columns(positions)
    opt = c["is_option"]
    notional = c["qty"] * c["multiplier"]

    price = np.where(opt, 0.0, c["underlying_price"])
    greeks = {k: np.zeros(len(positions)) for k in ("delta", "gamma", "vega", "theta", "rho")}
    greeks["delta"] = np.where(opt, 0.0, 1.0)

    if opt.any():
        T = np.maximum(c["dte"][opt], 0.0) / 365.0
        g = black_scholes_greeks(
            c["underlying_price"][opt], c["strike"][opt], T,
            c["rate"][opt], c["div_yield"][opt], c["iv"][opt], c["is_call"][opt],
        )
        # American value for the mark; BS greeks are the industry convention for display.
        price[opt] = american_price(
            c["underlying_price"][opt], c["strike"][opt], T,
            c["rate"][opt], c["div_yield"][opt], c["iv"][opt], c["is_call"][opt],
        )
        for k in greeks:
            greeks[k][opt] = g[k]

    return positions.with_columns(
        pl.Series("mark", price),
        pl.Series("market_value", price * notional),
        *[pl.Series(k, v * notional) for k, v in greeks.items()],
    )


def early_exercise_premium(positions: pl.DataFrame) -> np.ndarray:
    """American value minus European value at the base snapshot.

    Computed once. See `scenario_pnl(model="eep")` for why.
    """
    c = _columns(positions)
    T = np.maximum(c["dte"], 0.0) / 365.0
    # Placeholder strike on equity rows keeps log(S/K) finite; zeroed below.
    K = np.where(c["is_option"], c["strike"], 1.0)
    args = (c["underlying_price"], K, T, c["rate"], c["div_yield"], c["iv"], c["is_call"])
    prem = american_price(*args) - black_scholes(*args)
    return np.where(c["is_option"], np.maximum(prem, 0.0), 0.0)


def scenario_pnl(
    positions: pl.DataFrame,
    grid: ScenarioGrid,
    sigma_daily: np.ndarray | None = None,
    horizon_days: float = 1.0,
    chunk_size: int = DEFAULT_CHUNK,
    dtype: type = np.float32,
    model: str = "american",
    threads: int = 0,
) -> np.ndarray:
    """Return the (N, S) P&L matrix in `dtype`.

    `model` selects the repricing scheme:

      "american"  Full Bjerksund-Stensland reprice in every scenario. Default.

      "eep"       Reprice European in each scenario and carry the base early
                  exercise premium through unchanged. Roughly 2x faster, and
                  MEASURABLY TOO INACCURATE FOR RISK REPORTING -- keep it for
                  previews, not for numbers anyone acts on.

                  `spikes/eep_error.py` on a 200k book: total worst-case P&L
                  lands within 0.00%, which looks reassuring and is misleading.
                  Per scenario the error reaches 44% even on scenarios carrying
                  large P&L, with a systematic ~3MM bias in the near-the-money
                  rows. 96% of the error comes from puts: as the underlying
                  falls they go deep in the money and their early exercise
                  premium grows, which is exactly what this model holds fixed.

    `threads` > 1 spreads chunks across a thread pool. The work is
    embarrassingly parallel per position and numpy/scipy release the GIL, so
    this scales with cores without any pickling.
    """
    if model not in ("american", "eep"):
        raise ValueError(f"unknown model: {model}")

    c = _columns(positions)
    n, s = len(positions), len(grid)

    moves = grid.move_pct(sigma_daily, horizon_days)  # (S,) or (N, S)
    per_position_moves = moves.ndim == 2

    vol_shock = grid.vol_shock[None, :]
    days_fwd = grid.days_forward[None, :]

    # Base marks come from base_valuation when it has already run; otherwise
    # price them once here rather than once per chunk.
    if "mark" in positions.columns:
        base_mark = positions["mark"].to_numpy()
    else:
        base_mark = base_valuation(positions)["mark"].to_numpy()

    eep = early_exercise_premium(positions) if model == "eep" else None

    out = np.empty((n, s), dtype=dtype)
    notional = c["qty"] * c["multiplier"]

    def do_chunk(bounds: tuple[int, int]) -> None:
        start, stop = bounds
        sl = slice(start, stop)

        S0 = c["underlying_price"][sl][:, None]
        mv = moves[sl] if per_position_moves else moves[None, :]
        S_new = S0 * (1.0 + mv)

        opt_rows = c["is_option"][sl]
        pnl = S_new - S0  # equity default

        if opt_rows.any():
            # Equity rows ride along through the option maths (masking out 15%
            # of rows costs more than it saves); their results are discarded
            # by the np.where below. A placeholder strike keeps log(S/K) finite.
            K = np.where(opt_rows, c["strike"][sl], 1.0)[:, None]
            T_new = np.maximum(np.maximum(c["dte"][sl][:, None], 0.0) - days_fwd, 0.0) / 365.0
            vol_new = np.maximum(c["iv"][sl][:, None] + vol_shock, 0.001)
            r = c["rate"][sl][:, None]
            q = c["div_yield"][sl][:, None]
            call = np.broadcast_to(c["is_call"][sl][:, None], S_new.shape)

            if model == "american":
                shocked = american_price(S_new, K, T_new, r, q, vol_new, call)
            else:
                shocked = black_scholes(S_new, K, T_new, r, q, vol_new, call) + eep[sl][:, None]

            pnl = np.where(opt_rows[:, None], shocked - base_mark[sl][:, None], pnl)

        out[sl] = (pnl * notional[sl][:, None]).astype(dtype, copy=False)

    bounds = [(i, min(i + chunk_size, n)) for i in range(0, n, chunk_size)]
    threads = threads or DEFAULT_THREADS
    if threads > 1 and len(bounds) > 1:
        with ThreadPoolExecutor(max_workers=threads) as pool:
            list(pool.map(do_chunk, bounds))
    else:
        for b in bounds:
            do_chunk(b)

    return out
