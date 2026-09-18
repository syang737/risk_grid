"""The synthetic book, and how real its instruments are.

The generator used to name underlyings `U0042` and then draw a sector for each
one at random, so the same ticker could be a bank on one seed and a utility on
the next. These tests pin the properties that fix makes available: a symbol's
classification is a lookup, a contract identifies exactly one tradeable line,
and a strike and an expiry are things that are actually listed.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from risk import universe
from risk.synthetic import (
    DESKS,
    generate_book,
    listed_expiries,
    strike_step,
    third_friday,
)

BOOK_SIZE = 20_000


@pytest.fixture(scope="module")
def book() -> pl.DataFrame:
    df, _ = generate_book(BOOK_SIZE, seed=5)
    return df


@pytest.fixture(scope="module")
def options(book) -> pl.DataFrame:
    return book.filter(pl.col("is_option"))


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_every_symbol_carries_its_own_sector_and_industry(book):
    """The assertion the old random assignment could not have passed."""
    distinct = book.select("underlying", "sector", "industry").unique()
    assert len(distinct) == book["underlying"].n_unique(), "a ticker classified two ways"

    for ticker, sector, industry in distinct.iter_rows():
        symbol = universe.BY_TICKER[ticker]
        assert (sector, industry) == (symbol.sector, symbol.industry)


def test_the_book_spans_the_whole_universe_of_sectors(book):
    assert set(book["sector"]) == set(universe.SECTORS)
    # Industries are finer than sectors but still far inside the rollup
    # threshold, so `industry` is a materialised dimension like `sector`.
    assert book["industry"].n_unique() < 100


def test_industry_sits_strictly_below_sector(book):
    """One industry never spans two sectors, or the drill would be a lie."""
    pairs = book.select("sector", "industry").unique()
    assert pairs["industry"].n_unique() == len(pairs)


def test_prices_stay_near_their_indicative_level(book):
    """Jittered per run, but never somewhere that gives the demo away."""
    for ticker, price in book.select("underlying", "underlying_price").unique().iter_rows():
        indicative = universe.BY_TICKER[ticker].price
        assert 0.5 * indicative < price < 2.0 * indicative


def test_desks_are_named_not_numbered(book):
    assert set(book["desk"]) <= set(DESKS)


# --------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------


def test_a_contract_names_exactly_one_line(options):
    """Contract is the leaf drill level, so it must round-trip to its parts."""
    for row in options.select(
        "contract", "underlying", "expiry", "strike", "right"
    ).unique().iter_rows(named=True):
        symbol, expiry, tail = row["contract"].split(" ")
        assert symbol == row["underlying"]
        assert expiry == row["expiry"]
        assert tail[-1] == row["right"]
        assert float(tail[:-1]) == row["strike"]


def test_equity_contracts_are_just_the_ticker(book):
    equity = book.filter(~pl.col("is_option"))
    assert equity.filter(pl.col("contract") != pl.col("underlying")).height == 0


def test_two_strikes_a_half_apart_are_two_contracts():
    """147 and 147.5 used to collapse onto one identifier."""
    from datetime import date as _date

    from ingest.mapping import finalise

    frame = pl.DataFrame({
        "account": ["A", "A"], "underlying": ["AAPL", "AAPL"],
        "expiry": ["2027-01-15", "2027-01-15"], "strike": [147.0, 147.5],
        "right": ["C", "C"], "instrument_type": ["OPTION", "OPTION"],
        "qty": [1.0, 1.0], "underlying_price": [150.0, 150.0],
    })
    contracts = finalise(frame, _date(2026, 9, 18))["contract"].to_list()
    assert contracts == ["AAPL 2027-01-15 147C", "AAPL 2027-01-15 147.5C"]


# --------------------------------------------------------------------------
# Strikes
# --------------------------------------------------------------------------


def test_every_strike_sits_on_the_listed_ladder(options):
    step = strike_step(options["underlying_price"].to_numpy())
    remainder = options["strike"].to_numpy() % step
    # Modulo of a float ladder lands on 0 or on the step itself.
    assert ((remainder < 1e-9) | (step - remainder < 1e-9)).all()


def test_the_ladder_widens_with_price():
    import numpy as np

    prices = np.array([3.0, 30.0, 80.0, 180.0, 600.0, 5000.0])
    assert list(strike_step(prices)) == [0.5, 1.0, 2.5, 5.0, 10.0, 50.0]


def test_strikes_stay_around_the_money(options):
    ratio = (options["strike"] / options["underlying_price"]).to_numpy()
    assert 0.9 < float(ratio.mean()) < 1.1


# --------------------------------------------------------------------------
# Expiries
# --------------------------------------------------------------------------


def test_third_friday_is_a_friday_in_the_third_week():
    for year, month in ((2026, 1), (2026, 5), (2026, 8), (2027, 2), (2027, 11)):
        day = third_friday(year, month)
        assert day.weekday() == 4
        assert 15 <= day.day <= 21


@pytest.mark.parametrize("offset", range(7))
def test_listed_expiries_are_fridays_in_the_future(offset):
    """Whatever weekday the batch is built on."""
    today = date(2026, 6, 1) + timedelta(days=offset)
    expiries = listed_expiries(today)

    assert expiries == sorted(set(expiries))
    for day in expiries:
        assert day.weekday() == 4
        assert day > today


def test_expiry_and_dte_agree(book):
    today = date.today()
    options = book.filter(pl.col("is_option"))
    parsed = options.with_columns(
        pl.col("expiry").str.to_date("%Y-%m-%d").alias("as_date")
    )
    implied = (parsed["as_date"] - today).dt.total_days().cast(pl.Float64)
    assert (implied == parsed["dte"]).all()


def test_equities_have_no_expiry(book):
    equity = book.filter(~pl.col("is_option"))
    assert set(equity["expiry"]) == {"-"}
    assert equity["dte"].sum() == 0.0
    assert equity["strike"].sum() == 0.0


# --------------------------------------------------------------------------
# The universe itself
# --------------------------------------------------------------------------


def test_sampling_is_without_replacement():
    import numpy as np

    picked = universe.sample(50, np.random.default_rng(0))
    assert len({s.ticker for s in picked}) == 50


def test_asking_for_more_symbols_than_exist_says_so():
    """A silently smaller universe would change the cardinality under test."""
    import numpy as np

    with pytest.warns(UserWarning, match="universe holds"):
        picked = universe.sample(len(universe.UNIVERSE) + 1, np.random.default_rng(0))
    assert picked == universe.UNIVERSE


def test_every_sector_has_a_volatility():
    for symbol in universe.UNIVERSE:
        assert 0.05 < universe.annual_vol(symbol) < 1.5


# --------------------------------------------------------------------------
# Round trip through ingestion
# --------------------------------------------------------------------------


def test_a_dated_expiry_survives_the_mapping_pipeline():
    """Real dates take the date branch of the dte derivation, not the day count."""
    from ingest.mapping import FieldMapping, IngestionProfile, apply_profile

    today = date.today()
    source, _ = generate_book(2_000, seed=9, today=today)
    export = source.select(
        pl.col("account").alias("Acct No"),
        pl.col("underlying").alias("Ticker"),
        pl.col("qty").cast(pl.Utf8).alias("Quantity"),
        pl.col("strike").cast(pl.Utf8).alias("Strike Price"),
        pl.col("expiry").alias("Expiration"),
        pl.col("right").alias("C/P"),
        pl.col("underlying_price").cast(pl.Utf8).alias("Und Last"),
        pl.col("sector").alias("Product Group"),
        pl.col("industry").alias("Industry Group"),
    )

    profile = IngestionProfile(firm_id="acme", mappings=[
        FieldMapping("account", "Acct No"),
        FieldMapping("underlying", "Ticker"),
        FieldMapping("qty", "Quantity", transform="number"),
        FieldMapping("strike", "Strike Price", transform="number"),
        FieldMapping("expiry", "Expiration"),
        FieldMapping("right", "C/P", transform="call_put"),
        FieldMapping("underlying_price", "Und Last", transform="number"),
        FieldMapping("sector", "Product Group"),
        FieldMapping("industry", "Industry Group"),
    ])

    mapped = apply_profile(export, profile, batch_date=today)
    assert mapped["dte"].to_list() == source["dte"].to_list()
    assert mapped["industry"].to_list() == source["industry"].to_list()
    # And the contract the pipeline derives is the one the generator wrote.
    assert mapped["contract"].to_list() == source["contract"].to_list()
