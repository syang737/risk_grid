"""Synthetic book generator.

Shaped like a real carrying broker-dealer book rather than uniform random rows,
because the pivot cost depends on cardinality and skew, not just row count:
a few hundred underlyings, option chains that fan out across expiries and
strikes, and a long tail of small accounts under a few large desks.

Symbols, sectors and industries come from `risk.universe` -- a real ticker with
its real classification, not a label drawn at random -- and contracts carry a
real expiry date and a strike off a real ladder. Prices are the universe's
indicative levels with a per-run jitter; they are demo data, not market data.

Labels are built once per distinct value and then indexed, never formatted per
row -- at 5M positions the difference is tens of seconds.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from . import universe

# Tenors the chain is built around. Each snaps to a real listed expiry below,
# so the day counts that come out are the calendar's, not these.
EXPIRY_TENORS = (1, 2, 7, 14, 30, 45, 60, 90, 180, 365, 730)

# Desks of a carrying broker-dealer, since they sit next to the instruments in
# the grid and `DESK07` gave the whole screen away.
DESKS = (
    "Equity Derivatives", "Delta One", "Index Options", "Single Stock Options",
    "Volatility Arbitrage", "Market Making", "Prime Brokerage", "Securities Lending",
    "Convertible Arbitrage", "Statistical Arbitrage", "Program Trading", "Cash Equities",
    "ETF Trading", "Risk Arbitrage", "Portfolio Trading", "Retail Options",
    "Institutional Sales", "Proprietary Trading", "Structured Products", "Dispersion",
    "Event Driven", "Quantitative Strategies", "Long/Short Equity", "Special Situations",
    "Treasury",
)

# Strike increments by underlying price, roughly the listed ladder.
STRIKE_BANDS = (10.0, 50.0, 100.0, 250.0, 1000.0)
STRIKE_STEPS = (0.5, 1.0, 2.5, 5.0, 10.0)
STRIKE_STEP_ABOVE = 50.0


def third_friday(year: int, month: int) -> date:
    """The standard monthly expiry."""
    first = date(year, month, 1)
    # weekday(): Monday is 0, Friday is 4.
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def listed_expiries(today: date, tenors=EXPIRY_TENORS) -> list[date]:
    """Snap each tenor to a date something is actually listed on.

    Inside a month, weeklies expire on Friday; from a month out, the liquid
    line is the third Friday. Tenors that land on the same date collapse, which
    is what a real chain looks like -- a handful of near weeklies and then the
    monthly ladder.
    """
    dates: set[date] = set()
    for tenor in tenors:
        target = today + timedelta(days=int(tenor))
        if tenor < 30:
            # The next Friday on or after the target.
            snapped = target + timedelta(days=(4 - target.weekday()) % 7)
            if snapped <= today:
                snapped += timedelta(days=7)
        else:
            snapped = third_friday(target.year, target.month)
            if snapped <= today:
                year, month = divmod(target.year * 12 + target.month, 12)
                snapped = third_friday(year, month + 1)
        dates.add(snapped)
    return sorted(dates)


def strike_step(price: np.ndarray) -> np.ndarray:
    """The listed strike increment for each underlying price."""
    return np.select(
        [price < band for band in STRIKE_BANDS], STRIKE_STEPS, STRIKE_STEP_ABOVE
    )


def _desk_labels(n: int) -> np.ndarray:
    """Real desk names, numbered only once the list runs out."""
    names = []
    for i in range(n):
        name = DESKS[i % len(DESKS)]
        round_ = i // len(DESKS)
        names.append(name if round_ == 0 else f"{name} {round_ + 1}")
    return np.array(names)


def _strike_labels(strike: np.ndarray) -> np.ndarray:
    """`150` for a whole strike, `147.5` for a fractional one.

    A strike truncated to an integer would collapse 147 and 147.5 onto one
    contract identifier, merging two different instruments in the grid.
    """
    whole = strike == np.round(strike)
    out = np.where(
        whole,
        np.char.mod("%d", strike.astype(np.int64)),
        np.char.mod("%s", np.round(strike, 2)),
    )
    return out


def generate_book(
    n_positions: int,
    seed: int = 0,
    n_underlyings: int = 400,
    n_accounts: int = 5_000,
    n_master_accounts: int = 400,
    n_desks: int = 25,
    today: date | None = None,
) -> tuple[pl.DataFrame, np.ndarray]:
    """Return (positions, per-position daily sigma of the underlying)."""
    rng = np.random.default_rng(seed)
    n = n_positions
    today = today or date.today()

    # --- underlying universe -------------------------------------------------
    symbols = universe.sample(n_underlyings, rng)
    n_underlyings = len(symbols)
    u_idx = rng.integers(0, n_underlyings, n)

    u_label = np.array([s.ticker for s in symbols])
    u_sector = np.array([s.sector for s in symbols])
    u_industry = np.array([s.industry for s in symbols])
    # Indicative level, jittered so two batches differ without a name ever
    # printing somewhere absurd.
    u_price = np.array([s.price for s in symbols]) * np.exp(
        rng.normal(0.0, 0.05, n_underlyings)
    )
    u_vol = np.clip(
        np.array([universe.annual_vol(s) for s in symbols])
        * np.exp(rng.normal(0.0, 0.2, n_underlyings)),
        0.05, 1.5,
    )
    u_sigma_daily = u_vol / np.sqrt(252.0)

    # --- instruments ---------------------------------------------------------
    is_option = rng.random(n) < 0.85
    is_call = rng.random(n) < 0.5

    spot = u_price[u_idx]
    # Strikes cluster around the money, thin out in the wings, and then snap to
    # the increment the chain is actually listed on.
    moneyness = np.clip(rng.normal(1.0, 0.18, n), 0.3, 2.5)
    step = strike_step(spot)
    strike = np.maximum(np.round(spot * moneyness / step) * step, step)

    # --- expiries: real listed dates, and the day count they imply -----------
    expiry_dates = listed_expiries(today)
    expiry_labels = np.array([d.isoformat() for d in expiry_dates])
    expiry_dtes = np.array([(d - today).days for d in expiry_dates], dtype=float)
    e_idx = rng.integers(0, len(expiry_dates), n)
    dte = expiry_dtes[e_idx]

    # Per-position IV: underlying vol plus a smile-ish idiosyncratic spread.
    iv = np.clip(u_vol[u_idx] + rng.normal(0.0, 0.05, n), 0.03, 3.0)

    # --- account hierarchy: firm > desk > master account > account -----------
    # Pareto weights so a handful of accounts hold most of the book.
    ma_desk = rng.integers(0, n_desks, n_master_accounts)
    acct_ma = rng.integers(0, n_master_accounts, n_accounts)

    acct_weights = rng.pareto(1.2, n_accounts) + 1.0
    acct_weights /= acct_weights.sum()
    acct_idx = rng.choice(n_accounts, n, p=acct_weights)

    desk_label = _desk_labels(n_desks)
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
        "industry": u_industry[u_idx],
        "underlying": u_label[u_idx],
        "instrument_type": np.where(is_option, "OPTION", "EQUITY"),
        "expiry": np.where(is_option, expiry_labels[e_idx], "-"),
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
        "_strike_label": np.where(is_option, _strike_labels(strike), ""),
    })

    # Contract is the leaf drill level: the specific tradeable line. Same shape
    # `ingest.mapping.finalise` derives for a firm's file with no contract
    # column, so a synthetic book and an ingested one agree.
    df = df.with_columns(
        pl.when(pl.col("is_option"))
        .then(
            pl.col("underlying") + " " + pl.col("expiry") + " "
            + pl.col("_strike_label") + pl.col("right")
        )
        .otherwise(pl.col("underlying"))
        .alias("contract")
    ).drop("_strike_label")

    return df, u_sigma_daily[u_idx]
