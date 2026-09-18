"""Mapping a firm's export onto the canonical schema.

Every transform here is a named, parameterised operation rather than code, so
onboarding a firm is filling in a form rather than writing a parser. That is
the whole strategic bet of the admin layer: the incumbents' moat is dozens of
bespoke clearing-file parsers, and configuration beats code at scale.

Profiles are versioned. A firm changing their export must produce a new version
rather than silently reinterpreting history -- last quarter's batches were built
under the old mapping and have to stay reproducible.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Callable

import polars as pl

from .schema import BY_NAME, REQUIRED

# --------------------------------------------------------------------------
# Transforms
# --------------------------------------------------------------------------

TransformFn = Callable[[pl.Expr, dict, "MappingContext"], pl.Expr]
TRANSFORMS: dict[str, TransformFn] = {}


@dataclass
class MappingContext:
    """Everything a transform might need beyond the column itself."""

    batch_date: date
    frame_columns: tuple[str, ...] = ()


def transform(name: str) -> Callable[[TransformFn], TransformFn]:
    def register(fn: TransformFn) -> TransformFn:
        TRANSFORMS[name] = fn
        return fn

    return register


@transform("identity")
def _identity(expr: pl.Expr, params: dict, ctx: MappingContext) -> pl.Expr:
    return expr


@transform("trim")
def _trim(expr: pl.Expr, params: dict, ctx: MappingContext) -> pl.Expr:
    return expr.cast(pl.Utf8).str.strip_chars()


@transform("upper")
def _upper(expr: pl.Expr, params: dict, ctx: MappingContext) -> pl.Expr:
    return expr.cast(pl.Utf8).str.strip_chars().str.to_uppercase()


@transform("number")
def _number(expr: pl.Expr, params: dict, ctx: MappingContext) -> pl.Expr:
    """Parse a number that may carry thousands separators, currency or parens.

    Accounting-style negatives -- (1,234) meaning -1234 -- turn up in exports
    often enough to be worth handling rather than silently producing nulls.
    """
    text = expr.cast(pl.Utf8).str.strip_chars()
    negative = text.str.starts_with("(") & text.str.ends_with(")")
    cleaned = text.str.replace_all(r"[,$()\s]", "").str.replace_all(r"^\+", "")
    value = cleaned.cast(pl.Float64, strict=False)
    return pl.when(negative).then(-value).otherwise(value)


@transform("scale")
def _scale(expr: pl.Expr, params: dict, ctx: MappingContext) -> pl.Expr:
    """Multiply by a constant. Strikes sent in thousandths, vol sent as percent."""
    return _number(expr, params, ctx) * float(params.get("factor", 1.0))


@transform("abs")
def _abs(expr: pl.Expr, params: dict, ctx: MappingContext) -> pl.Expr:
    return _number(expr, params, ctx).abs()


@transform("negate")
def _negate(expr: pl.Expr, params: dict, ctx: MappingContext) -> pl.Expr:
    return -_number(expr, params, ctx)


@transform("signed_by")
def _signed_by(expr: pl.Expr, params: dict, ctx: MappingContext) -> pl.Expr:
    """Apply a sign from a separate side column.

    Plenty of exports send quantity unsigned with a LONG/SHORT column beside it;
    loading that without the sign would report a flat book as doubly long.
    """
    side = params.get("side_column")
    if not side:
        raise ValueError("signed_by needs a side_column")
    if side not in ctx.frame_columns:
        raise ValueError(f"signed_by side_column {side!r} is not in the file")
    shorts = [str(v).upper() for v in params.get("short_values", ["S", "SHORT", "SELL", "-"])]
    magnitude = _number(expr, params, ctx).abs()
    is_short = pl.col(side).cast(pl.Utf8).str.strip_chars().str.to_uppercase().is_in(shorts)
    return pl.when(is_short).then(-magnitude).otherwise(magnitude)


@transform("call_put")
def _call_put(expr: pl.Expr, params: dict, ctx: MappingContext) -> pl.Expr:
    """Normalise whatever a firm calls a call into C, and a put into P."""
    text = expr.cast(pl.Utf8).str.strip_chars().str.to_uppercase()
    calls = [str(v).upper() for v in params.get("call_values", ["C", "CALL", "CALLS", "1"])]
    puts = [str(v).upper() for v in params.get("put_values", ["P", "PUT", "PUTS", "0", "-1"])]
    return (
        pl.when(text.is_in(calls)).then(pl.lit("C"))
        .when(text.is_in(puts)).then(pl.lit("P"))
        .otherwise(pl.lit("-"))
    )


@transform("date")
def _date(expr: pl.Expr, params: dict, ctx: MappingContext) -> pl.Expr:
    """Parse a date to an ISO string, trying the given format then common ones."""
    text = expr.cast(pl.Utf8).str.strip_chars()
    formats = [params["format"]] if params.get("format") else []
    formats += ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y%m%d", "%d-%b-%Y", "%m/%d/%y"]

    parsed = pl.lit(None, dtype=pl.Date)
    for fmt in formats:
        parsed = pl.coalesce(parsed, text.str.to_date(fmt, strict=False))
    return parsed.dt.to_string("%Y-%m-%d")


@transform("map_values")
def _map_values(expr: pl.Expr, params: dict, ctx: MappingContext) -> pl.Expr:
    """Explicit lookup, for codes only the firm understands."""
    table: dict = params.get("values", {})
    text = expr.cast(pl.Utf8).str.strip_chars()
    result = pl.lit(params.get("default"))
    for source, target in table.items():
        result = pl.when(text == str(source)).then(pl.lit(target)).otherwise(result)
    return result


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------


@dataclass
class FieldMapping:
    """One canonical field, and where its value comes from."""

    field: str
    source: str | None = None
    constant: Any = None
    transform: str = "identity"
    params: dict = field(default_factory=dict)

    def validate(self, columns: tuple[str, ...]) -> list[str]:
        problems = []
        if self.field not in BY_NAME:
            problems.append(f"unknown canonical field: {self.field}")
        if self.transform not in TRANSFORMS:
            problems.append(f"unknown transform: {self.transform}")
        if self.source is None and self.constant is None:
            problems.append(f"{self.field}: needs either a source column or a constant")
        if self.source is not None and self.source not in columns:
            problems.append(f"{self.field}: source column {self.source!r} is not in the file")
        return problems

    def expr(self, ctx: MappingContext) -> pl.Expr:
        if self.source is None:
            return pl.lit(self.constant).alias(self.field)
        built = TRANSFORMS[self.transform](pl.col(self.source), self.params, ctx)
        target = BY_NAME[self.field].dtype
        if target in (pl.Float64, pl.Int64):
            built = built.cast(target, strict=False)
        return built.alias(self.field)


@dataclass
class IngestionProfile:
    """How one firm's file becomes a canonical position frame."""

    firm_id: str
    name: str = "default"
    version: int = 1
    file_format: str = "csv"
    read_options: dict = field(default_factory=dict)
    mappings: list[FieldMapping] = field(default_factory=list)

    def mapped_fields(self) -> set[str]:
        return {m.field for m in self.mappings}

    def validate(self, columns: tuple[str, ...]) -> list[str]:
        problems: list[str] = []
        for mapping in self.mappings:
            problems.extend(mapping.validate(columns))

        seen = [m.field for m in self.mappings]
        for name in {f for f in seen if seen.count(f) > 1}:
            problems.append(f"{name}: mapped more than once")

        missing = [f for f in REQUIRED if f not in self.mapped_fields()]
        if missing:
            problems.append(f"required fields not mapped: {', '.join(missing)}")
        return problems

    def to_dict(self) -> dict:
        return {
            "firm_id": self.firm_id,
            "name": self.name,
            "version": self.version,
            "file_format": self.file_format,
            "read_options": self.read_options,
            "mappings": [asdict(m) for m in self.mappings],
        }

    @classmethod
    def from_dict(cls, data: dict) -> IngestionProfile:
        return cls(
            firm_id=data["firm_id"],
            name=data.get("name", "default"),
            version=data.get("version", 1),
            file_format=data.get("file_format", "csv"),
            read_options=data.get("read_options", {}),
            mappings=[FieldMapping(**m) for m in data.get("mappings", [])],
        )


# --------------------------------------------------------------------------
# Reading and applying
# --------------------------------------------------------------------------


def read_source(path, file_format: str = "csv", options: dict | None = None) -> pl.DataFrame:
    """Read a firm's file as raw strings.

    Everything comes in as text on purpose: type inference is where silent
    corruption lives -- a strike column with one blank row becomes a string
    column, an account number with leading zeros loses them. The profile's
    transforms decide types explicitly.
    """
    options = dict(options or {})
    if file_format == "parquet":
        return pl.read_parquet(path)
    return pl.read_csv(
        path,
        infer_schema_length=0,  # all Utf8
        separator=options.get("separator", ","),
        skip_rows=options.get("skip_rows", 0),
        encoding=options.get("encoding", "utf8"),
        null_values=options.get("null_values", ["", "NULL", "null", "NA", "N/A"]),
        truncate_ragged_lines=options.get("truncate_ragged_lines", False),
    )


class MappingError(Exception):
    """The profile does not fit the file. Carries every problem, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def apply_profile(
    frame: pl.DataFrame, profile: IngestionProfile, batch_date: date | None = None
) -> pl.DataFrame:
    """Map a raw frame onto the canonical schema and fill in what is derivable."""
    batch_date = batch_date or datetime.now().date()
    ctx = MappingContext(batch_date=batch_date, frame_columns=tuple(frame.columns))

    problems = profile.validate(ctx.frame_columns)
    if problems:
        raise MappingError(problems)

    mapped = frame.select([m.expr(ctx) for m in profile.mappings])
    return finalise(mapped, batch_date)


def finalise(frame: pl.DataFrame, batch_date: date) -> pl.DataFrame:
    """Fill defaults and derive the fields the engine needs but nobody maps."""
    present = set(frame.columns)
    out = frame

    # Defaults for anything optional and unmapped.
    for name, spec in BY_NAME.items():
        if name not in present and spec.default is not None:
            out = out.with_columns(pl.lit(spec.default).cast(spec.dtype).alias(name))
    present = set(out.columns)

    if "strike" not in present:
        out = out.with_columns(pl.lit(0.0).alias("strike"))
    if "right" not in present:
        out = out.with_columns(pl.lit("-").alias("right"))

    # An option is a line with a strike. Inferring from the strike rather than
    # trusting a type column means a firm need not map one.
    if "instrument_type" not in present:
        out = out.with_columns(
            pl.when(pl.col("strike").fill_null(0) > 0)
            .then(pl.lit("OPTION")).otherwise(pl.lit("EQUITY"))
            .alias("instrument_type")
        )
    out = out.with_columns(
        (pl.col("instrument_type").cast(pl.Utf8).str.to_uppercase() == "OPTION").alias("is_option")
    )
    out = out.with_columns(
        (pl.col("right").cast(pl.Utf8).str.to_uppercase() == "C").alias("is_call")
    )

    if "multiplier" not in present:
        out = out.with_columns(
            pl.when(pl.col("is_option")).then(100.0).otherwise(1.0).alias("multiplier")
        )

    # Days to expiry, from an expiry that may be a date or already a day count.
    if "dte" not in present:
        as_date = pl.col("expiry").cast(pl.Utf8).str.to_date("%Y-%m-%d", strict=False)
        as_days = pl.col("expiry").cast(pl.Float64, strict=False)
        days_to = (as_date - pl.lit(batch_date)).dt.total_days().cast(pl.Float64)
        out = out.with_columns(
            pl.when(pl.col("is_option"))
            .then(pl.coalesce(days_to, as_days, pl.lit(0.0)).clip(0.0))
            .otherwise(0.0)
            .alias("dte")
        )

    if "master_account" not in present:
        out = out.with_columns(pl.col("account").alias("master_account"))

    if "contract" not in present:
        # A strike truncated to an integer would put the 147 and 147.5 lines on
        # one contract identifier, merging two instruments in the grid -- so
        # whole strikes print whole and fractional ones keep their decimals.
        strike = pl.col("strike").cast(pl.Float64, strict=False)
        strike_label = (
            pl.when(strike == strike.round(0))
            .then(strike.cast(pl.Int64, strict=False).cast(pl.Utf8))
            .otherwise(strike.round(2).cast(pl.Utf8).str.strip_chars_end("0"))
        )
        out = out.with_columns(
            pl.when(pl.col("is_option"))
            .then(
                pl.col("underlying") + " " + pl.col("expiry").cast(pl.Utf8) + " "
                + strike_label + pl.col("right")
            )
            .otherwise(pl.col("underlying"))
            .alias("contract")
        )

    if "position_id" not in out.columns:
        out = out.with_columns(pl.arange(0, pl.len(), dtype=pl.Int64).alias("position_id"))

    return out


def sigma_from(frame: pl.DataFrame) -> tuple[pl.DataFrame, "Any", bool]:
    """Pull out the per-position daily sigma, or fall back to implied vol.

    Historical stddev properly comes from a price history, not a position file.
    When a firm does not supply it the shock template is still usable but is
    calibrated off implied vol, so the caller is told which it got rather than
    being left to assume.
    """
    import numpy as np

    if "sigma_daily" in frame.columns:
        values = frame["sigma_daily"].to_numpy()
        if np.isfinite(values).any() and np.nanmax(values) > 0:
            return frame.drop("sigma_daily"), np.nan_to_num(values, nan=0.01), True

    iv = frame["iv"].fill_null(0.0).to_numpy() if "iv" in frame.columns else None
    if iv is None:
        iv = np.full(frame.height, 0.30)
    fallback = np.maximum(iv, 0.01) / np.sqrt(252.0)
    return frame.drop("sigma_daily") if "sigma_daily" in frame.columns else frame, fallback, False


# --------------------------------------------------------------------------
# Onboarding helpers
# --------------------------------------------------------------------------

# Names a field plausibly goes by. Used only to pre-fill the mapping screen --
# a wrong guess is visible and one click to fix, which beats an empty form.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "account": ("acct", "accountid", "accountnumber", "acctno", "accountno", "subaccount"),
    "master_account": ("master", "masteracct", "parentaccount", "parentacct", "mastercode"),
    "desk": ("businessunit", "bu", "group", "tradinggroup", "book"),
    "firm": ("entity", "legalentity", "company"),
    "underlying": ("symbol", "ticker", "root", "underlyingsymbol", "undsym", "sym", "rootsymbol"),
    "sector": ("product", "productgroup", "classification", "assetclass"),
    "industry": ("industrygroup", "subsector", "subindustry"),
    "instrument_type": ("sectype", "securitytype", "instrumenttype", "type", "prodtype"),
    "strike": ("strikeprice", "k", "exerciseprice"),
    "expiry": ("expiration", "expdate", "maturity", "expirydate", "expirationdate", "maturitydate"),
    "right": ("callput", "cp", "putcall", "optiontype", "cpflag", "pc"),
    "contract": ("occsymbol", "osi", "contractid", "instrumentid", "secid"),
    "qty": ("quantity", "position", "pos", "shares", "contracts", "netqty", "netposition", "size"),
    "multiplier": ("contractsize", "mult", "pointvalue", "lotsize"),
    "underlying_price": ("undprice", "undlast", "undpx", "undmark", "spot", "spotprice",
                         "underlyinglast", "underlyingpx", "underlyingmark", "last",
                         "lastprice", "closeprice", "refprice", "mark"),
    "iv": ("impliedvol", "impvol", "vol", "sigma", "volatility", "ivol"),
    "dte": ("daystoexpiry", "dtm", "daystomaturity"),
    "rate": ("interestrate", "riskfreerate", "rfr"),
    "div_yield": ("dividendyield", "divyield", "yield", "dvd"),
    "sigma_daily": ("dailysigma", "dailyvol", "histvol", "historicalvol", "stddev"),
}


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def suggest_mappings(columns: list[str]) -> list[FieldMapping]:
    """Guess a starting profile from the column names in a sample file.

    Pre-filling the mapping screen is most of why onboarding can be hours
    instead of weeks; the guesses are shown for confirmation, never applied
    silently.
    """
    lookup: dict[str, str] = {}
    for field_name, aliases in SYNONYMS.items():
        lookup[_normalise(field_name)] = field_name
        for alias in aliases:
            lookup.setdefault(_normalise(alias), field_name)

    used: set[str] = set()
    out: list[FieldMapping] = []
    for column in columns:
        target = lookup.get(_normalise(column))
        if target is None or target in used:
            continue
        used.add(target)
        out.append(FieldMapping(field=target, source=column, transform=_default_transform(target)))
    return out


def _default_transform(field_name: str) -> str:
    if field_name in ("qty", "strike", "underlying_price", "iv", "multiplier",
                      "rate", "div_yield", "dte", "sigma_daily"):
        return "number"
    if field_name == "right":
        return "call_put"
    if field_name == "expiry":
        return "date"
    if field_name in ("underlying", "instrument_type"):
        return "upper"
    return "trim"


def describe_transforms() -> list[dict]:
    """Transform catalogue for the mapping UI."""
    return [
        {"name": "identity", "params": [], "description": "Use the value as-is"},
        {"name": "trim", "params": [], "description": "Strip surrounding whitespace"},
        {"name": "upper", "params": [], "description": "Trim and uppercase"},
        {"name": "number", "params": [],
         "description": "Parse a number, handling commas, currency and (1,234) negatives"},
        {"name": "scale", "params": ["factor"],
         "description": "Parse a number and multiply, e.g. 0.01 for vol sent as a percent"},
        {"name": "abs", "params": [], "description": "Parse a number and drop the sign"},
        {"name": "negate", "params": [], "description": "Parse a number and flip the sign"},
        {"name": "signed_by", "params": ["side_column", "short_values"],
         "description": "Take magnitude here and the sign from a LONG/SHORT column"},
        {"name": "call_put", "params": ["call_values", "put_values"],
         "description": "Normalise an option right to C or P"},
        {"name": "date", "params": ["format"],
         "description": "Parse a date to ISO; tries common formats if none given"},
        {"name": "map_values", "params": ["values", "default"],
         "description": "Explicit lookup table for firm-specific codes"},
    ]
