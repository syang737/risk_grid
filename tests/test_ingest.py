"""Mapping, validation, and the ingestion pipeline.

Most of these use a deliberately awkward export -- unsigned quantities with a
side column, accounting negatives, strikes in thousandths, vol as a percent --
because that is what clearing firms actually send, and a mapping layer that only
handles tidy files is not a mapping layer.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from ingest.mapping import (
    FieldMapping,
    IngestionProfile,
    MappingError,
    apply_profile,
    describe_transforms,
    read_source,
    sigma_from,
    suggest_mappings,
)
from ingest.schema import REQUIRED, describe
from ingest.validate import ValidationContext, validate

RAW = pl.DataFrame({
    "Acct No":      ["A-001", "A-001", "A-002", "A-002"],
    "Ticker":       ["aapl ", "AAPL", "MSFT", "SPY"],
    "Side":         ["LONG", "SHORT", "SHORT", "LONG"],
    "Quantity":     ["1,200", "(300)", "50", "2,000"],
    "Strike Price": ["150000", "160000", "400000", "0"],
    "Expiration":   ["03/20/2026", "03/20/2026", "06/19/2026", ""],
    "C/P":          ["CALL", "put", "Calls", ""],
    "Und Last":     ["$182.50", "182.50", "410.25", "560.10"],
    "Implied Vol":  ["28.5", "31.2", "24.0", "0"],
})

BATCH_DATE = date(2026, 1, 15)


def profile(**overrides) -> IngestionProfile:
    mappings = [
        FieldMapping("account", "Acct No", transform="upper"),
        FieldMapping("underlying", "Ticker", transform="upper"),
        FieldMapping("qty", "Quantity", transform="signed_by",
                     params={"side_column": "Side", "short_values": ["SHORT"]}),
        FieldMapping("strike", "Strike Price", transform="scale", params={"factor": 0.001}),
        FieldMapping("expiry", "Expiration", transform="date", params={"format": "%m/%d/%Y"}),
        FieldMapping("right", "C/P", transform="call_put"),
        FieldMapping("underlying_price", "Und Last", transform="number"),
        FieldMapping("iv", "Implied Vol", transform="scale", params={"factor": 0.01}),
    ]
    return IngestionProfile(firm_id="acme", mappings=mappings, **overrides)


@pytest.fixture
def mapped():
    return apply_profile(RAW, profile(), batch_date=BATCH_DATE)


# --------------------------------------------------------------------------
# Transforms
# --------------------------------------------------------------------------


def test_side_column_supplies_the_sign(mapped):
    """Loading unsigned quantities without the side column reports a flat book as long."""
    assert mapped["qty"].to_list() == [1200.0, -300.0, -50.0, 2000.0]


def test_accounting_negatives_are_understood(mapped):
    assert mapped["qty"][1] == -300.0


def test_currency_and_separators_are_stripped(mapped):
    assert mapped["underlying_price"].to_list() == [182.50, 182.50, 410.25, 560.10]


def test_scale_converts_units(mapped):
    assert mapped["strike"].to_list() == [150.0, 160.0, 400.0, 0.0]
    assert mapped["iv"][0] == pytest.approx(0.285)


def test_right_encodings_are_normalised(mapped):
    assert mapped["right"].to_list() == ["C", "P", "C", "-"]


def test_dates_parse_to_day_counts(mapped):
    # 2026-01-15 -> 2026-03-20 is 64 days; the equity row has none.
    assert mapped["dte"].to_list() == [64.0, 64.0, 155.0, 0.0]


def test_text_is_trimmed_and_uppercased(mapped):
    assert mapped["underlying"].to_list() == ["AAPL", "AAPL", "MSFT", "SPY"]


def test_map_values_handles_firm_specific_codes():
    raw = pl.DataFrame({"acct": ["1"], "sym": ["X"], "q": ["1"], "px": ["10"], "cls": ["EQ"]})
    coded = IngestionProfile(firm_id="f", mappings=[
        FieldMapping("account", "acct"),
        FieldMapping("underlying", "sym"),
        FieldMapping("qty", "q", transform="number"),
        FieldMapping("underlying_price", "px", transform="number"),
        FieldMapping("sector", "cls", transform="map_values",
                     params={"values": {"EQ": "Equities"}, "default": "Unclassified"}),
    ])
    assert apply_profile(raw, coded)["sector"][0] == "Equities"


def test_a_rule_that_cannot_run_is_an_error_not_a_warning():
    """An unvalidatable file must not pass as validated."""
    from ingest import validate as module

    def explodes(frame, ctx):
        raise RuntimeError("boom")

    module.RULES.append(explodes)
    try:
        report = validate(pl.DataFrame({"account": ["a"], "underlying": ["X"],
                                        "qty": [1.0], "underlying_price": [10.0]}))
        assert not report.ok
        assert any(f.code == "rule_failed" for f in report.errors)
    finally:
        module.RULES.remove(explodes)


# --------------------------------------------------------------------------
# Derivation
# --------------------------------------------------------------------------


def test_options_are_inferred_from_the_strike(mapped):
    """A firm need not map an instrument type column at all."""
    assert mapped["is_option"].to_list() == [True, True, True, False]
    assert mapped["instrument_type"].to_list() == ["OPTION", "OPTION", "OPTION", "EQUITY"]


def test_multiplier_defaults_by_instrument(mapped):
    assert mapped["multiplier"].to_list() == [100.0, 100.0, 100.0, 1.0]


def test_contract_is_built_when_absent(mapped):
    assert mapped["contract"][0] == "AAPL 2026-03-20 150C"
    assert mapped["contract"][3] == "SPY"


def test_master_account_falls_back_to_account(mapped):
    assert mapped["master_account"].to_list() == mapped["account"].to_list()


def test_unmapped_optional_fields_take_defaults(mapped):
    assert set(mapped["sector"].to_list()) == {"Unclassified"}
    assert mapped["rate"][0] == pytest.approx(0.042)


def test_engine_required_columns_are_all_present(mapped):
    from risk.engine import REQUIRED_COLUMNS

    assert set(REQUIRED_COLUMNS) <= set(mapped.columns)


def test_mapped_frame_actually_prices():
    """The real test of the schema: the engine accepts it."""
    from risk.batch import build_batch
    from risk.templates import default_shock_config

    frame = apply_profile(RAW, profile(), batch_date=BATCH_DATE)
    positions, sigma, supplied = sigma_from(frame)
    assert supplied is False

    batch = build_batch(positions, sigma, grid=default_shock_config().to_grid(), firm_id="acme")
    assert batch.positions == 4
    assert "worst" in batch.aggregate(
        __import__("risk.aggregate", fromlist=["PivotRequest"]).PivotRequest(
            dimensions=("account",))
    ).rows.columns


def test_sigma_is_used_when_the_firm_supplies_it():
    frame = apply_profile(RAW, profile(), batch_date=BATCH_DATE)
    frame = frame.with_columns(pl.lit(0.02).alias("sigma_daily"))
    _, sigma, supplied = sigma_from(frame)
    assert supplied is True and sigma[0] == pytest.approx(0.02)


# --------------------------------------------------------------------------
# Profile validation
# --------------------------------------------------------------------------


def test_every_problem_is_reported_at_once():
    """A form that reveals one error per submit is a bad form."""
    bad = IngestionProfile(firm_id="acme", mappings=[FieldMapping("account", "Nope")])
    with pytest.raises(MappingError) as excinfo:
        apply_profile(RAW, bad)

    problems = excinfo.value.problems
    assert any("Nope" in p for p in problems)
    assert any("required fields not mapped" in p for p in problems)


def test_unknown_transform_is_refused():
    bad = IngestionProfile(firm_id="acme", mappings=[
        FieldMapping("account", "Acct No", transform="teleport")
    ])
    assert any("unknown transform" in p for p in bad.validate(tuple(RAW.columns)))


def test_duplicate_field_mapping_is_refused():
    duplicated = profile()
    duplicated.mappings.append(FieldMapping("account", "Ticker"))
    assert any("mapped more than once" in p for p in duplicated.validate(tuple(RAW.columns)))


def test_signed_by_needs_its_side_column_to_exist():
    bad = IngestionProfile(firm_id="acme", mappings=[
        FieldMapping("account", "Acct No"),
        FieldMapping("underlying", "Ticker"),
        FieldMapping("underlying_price", "Und Last", transform="number"),
        FieldMapping("qty", "Quantity", transform="signed_by",
                     params={"side_column": "NotThere"}),
    ])
    with pytest.raises(ValueError, match="NotThere"):
        apply_profile(RAW, bad)


def test_profile_roundtrips_through_json():
    original = profile()
    restored = IngestionProfile.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()
    assert apply_profile(RAW, restored, BATCH_DATE).equals(apply_profile(RAW, original, BATCH_DATE))


# --------------------------------------------------------------------------
# Onboarding helpers
# --------------------------------------------------------------------------


def test_suggestions_prefill_the_mapping_screen():
    suggested = {m.source: m.field for m in suggest_mappings(list(RAW.columns))}
    assert suggested["Acct No"] == "account"
    assert suggested["Ticker"] == "underlying"
    assert suggested["Quantity"] == "qty"
    assert suggested["C/P"] == "right"
    assert suggested["Expiration"] == "expiry"


def test_suggestions_never_map_one_field_twice():
    fields = [m.field for m in suggest_mappings(["account", "acct", "account_id", "symbol"])]
    assert len(fields) == len(set(fields))


def test_catalogues_are_available_for_the_ui():
    assert {f["name"] for f in describe()} >= set(REQUIRED)
    assert {t["name"] for t in describe_transforms()} >= {"signed_by", "call_put", "scale"}


def test_csv_is_read_as_text_so_types_are_explicit(tmp_path):
    """Inference is where silent corruption lives: leading zeros, mixed columns."""
    path = tmp_path / "positions.csv"
    path.write_text("acct,qty\n0001,5\n0002,6\n")
    frame = read_source(path, "csv")
    assert frame["acct"].to_list() == ["0001", "0002"]


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_a_clean_file_passes(mapped):
    report = validate(mapped)
    assert report.ok
    assert not report.errors


def test_nulls_in_required_fields_are_errors(mapped):
    broken = mapped.with_columns(
        pl.when(pl.arange(0, pl.len()) == 0).then(None).otherwise(pl.col("account")).alias("account")
    )
    report = validate(broken)
    assert not report.ok
    assert any(f.code == "null_required" for f in report.errors)


def test_a_mostly_bad_price_column_is_an_error_but_one_row_is_a_warning(mapped):
    one_bad = mapped.with_columns(
        pl.when(pl.arange(0, pl.len()) == 0).then(0.0)
        .otherwise(pl.col("underlying_price")).alias("underlying_price")
    )
    # One of four rows is 25%, over the 1% threshold, so this is an error.
    assert not validate(one_bad).ok

    big = pl.concat([mapped] * 100)
    mostly_good = big.with_columns(
        pl.when(pl.arange(0, pl.len()) == 0).then(0.0)
        .otherwise(pl.col("underlying_price")).alias("underlying_price")
    )
    report = validate(mostly_good)
    assert report.ok and any(f.code == "bad_price" for f in report.warnings)


def test_an_option_without_a_strike_cannot_be_priced(mapped):
    broken = mapped.with_columns(
        pl.when(pl.col("is_option")).then(0.0).otherwise(pl.col("strike")).alias("strike")
    )
    assert any(f.code == "option_without_strike" for f in validate(broken).errors)


def test_an_already_expired_option_is_an_error(mapped):
    broken = mapped.with_columns(
        pl.when(pl.col("is_option")).then(-5.0).otherwise(pl.col("dte")).alias("dte")
    )
    assert any(f.code == "expired_option" for f in validate(broken).errors)


def test_implausible_volatility_suggests_the_scale_transform(mapped):
    unscaled = mapped.with_columns((pl.col("iv") * 100).alias("iv"))
    finding = next(f for f in validate(unscaled).warnings if f.code == "implausible_iv")
    assert "scale transform" in finding.message


def test_a_truncated_file_is_caught_by_row_drift(mapped):
    """The classic silent failure: it loads, and it lies."""
    report = validate(mapped, ValidationContext(previous_rows=100))
    finding = next(f for f in report.errors if f.code == "row_drift")
    assert "truncated" in finding.message

    mild = validate(mapped, ValidationContext(previous_rows=5))
    assert mild.ok and any(f.code == "row_drift" for f in mild.warnings)


def test_an_empty_file_short_circuits(mapped):
    report = validate(mapped.head(0))
    assert not report.ok
    assert [f.code for f in report.findings] == ["empty_file"]


def test_findings_carry_samples_to_look_at(mapped):
    broken = mapped.with_columns(pl.lit(0.0).alias("underlying_price"))
    finding = next(f for f in validate(broken).errors if f.code == "bad_price")
    assert finding.count == mapped.height
    assert finding.sample and "underlying" in finding.sample[0]
