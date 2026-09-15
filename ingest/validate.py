"""Deciding whether a mapped file is fit to build a batch from.

The failure that actually hurts is not a file that errors -- it is a file that
loads. A truncated export, a column that quietly became null, a strike field
that shifted: each produces a batch that looks fine and is wrong, and a risk
screen nobody can trust is worse than no risk screen.

So findings are split by what they should cause:

  error    quarantine the file. No batch is built. Someone is told.
  warning  build the batch, record the warning, show it against the batch.

Nothing here is silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import polars as pl

ERROR = "error"
WARNING = "warning"

# A little bad data is a data problem; a lot is a broken feed. Thresholds are
# what separate the two.
BAD_PRICE_ERROR_FRACTION = 0.01
ROW_DRIFT_WARNING = 0.20
ROW_DRIFT_ERROR = 0.50
MAX_PLAUSIBLE_IV = 5.0


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    count: int = 0
    sample: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity, "code": self.code, "message": self.message,
            "count": self.count, "sample": self.sample,
        }


@dataclass
class ValidationReport:
    rows: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return f"{self.rows:,} rows, no findings"
        parts = [f"{self.rows:,} rows"]
        if self.errors:
            parts.append(f"{len(self.errors)} error(s): " + "; ".join(f.message for f in self.errors))
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s): " + "; ".join(f.message for f in self.warnings))
        return " | ".join(parts)

    def to_dict(self) -> dict:
        return {
            "rows": self.rows,
            "ok": self.ok,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class ValidationContext:
    """What the last good batch looked like, so drift can be detected."""

    previous_rows: int | None = None
    previous_accounts: int | None = None


Rule = Callable[[pl.DataFrame, ValidationContext], list[Finding]]
RULES: list[Rule] = []


def rule(fn: Rule) -> Rule:
    RULES.append(fn)
    return fn


def _sample(frame: pl.DataFrame, mask: pl.Expr, columns: list[str], limit: int = 3) -> list:
    # Deduplicated because callers naturally pass context columns plus the
    # offending one, and those overlap whenever the offender is the context.
    available = list(dict.fromkeys(c for c in columns if c in frame.columns))
    if not available:
        return []
    return frame.filter(mask).select(available).head(limit).to_dicts()


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


@rule
def no_rows(frame: pl.DataFrame, ctx: ValidationContext) -> list[Finding]:
    if frame.height == 0:
        return [Finding(ERROR, "empty_file", "the file mapped to zero rows")]
    return []


@rule
def required_fields_present(frame: pl.DataFrame, ctx: ValidationContext) -> list[Finding]:
    from .schema import REQUIRED

    out = []
    for name in REQUIRED:
        if name not in frame.columns:
            out.append(Finding(ERROR, "missing_field", f"required field {name!r} is absent"))
            continue
        nulls = frame[name].null_count()
        if nulls:
            out.append(Finding(
                ERROR, "null_required",
                f"{name!r} is null in {nulls:,} of {frame.height:,} rows",
                count=nulls,
                sample=_sample(frame, pl.col(name).is_null(), ["account", "underlying", name]),
            ))
    return out


@rule
def prices_are_positive(frame: pl.DataFrame, ctx: ValidationContext) -> list[Finding]:
    if "underlying_price" not in frame.columns:
        return []
    mask = (pl.col("underlying_price") <= 0) | pl.col("underlying_price").is_null()
    bad = frame.filter(mask).height
    if not bad:
        return []

    fraction = bad / frame.height
    severity = ERROR if fraction > BAD_PRICE_ERROR_FRACTION else WARNING
    return [Finding(
        severity, "bad_price",
        f"underlying price is zero or negative on {bad:,} rows ({fraction:.1%})",
        count=bad,
        sample=_sample(frame, mask, ["account", "underlying", "underlying_price"]),
    )]


@rule
def options_are_coherent(frame: pl.DataFrame, ctx: ValidationContext) -> list[Finding]:
    """An option without a strike, or already expired, cannot be priced."""
    if "is_option" not in frame.columns:
        return []
    out = []

    no_strike = pl.col("is_option") & ((pl.col("strike") <= 0) | pl.col("strike").is_null())
    count = frame.filter(no_strike).height
    if count:
        out.append(Finding(
            ERROR, "option_without_strike",
            f"{count:,} option rows have no strike",
            count=count,
            sample=_sample(frame, no_strike, ["account", "underlying", "expiry", "strike"]),
        ))

    if "dte" in frame.columns:
        expired = pl.col("is_option") & (pl.col("dte") < 0)
        count = frame.filter(expired).height
        if count:
            out.append(Finding(
                ERROR, "expired_option",
                f"{count:,} option rows expired before the batch date",
                count=count,
                sample=_sample(frame, expired, ["underlying", "expiry", "dte"]),
            ))

    if "right" in frame.columns:
        unknown = pl.col("is_option") & ~pl.col("right").is_in(["C", "P"])
        count = frame.filter(unknown).height
        if count:
            out.append(Finding(
                ERROR, "unknown_right",
                f"{count:,} option rows have neither C nor P as their right",
                count=count,
                sample=_sample(frame, unknown, ["underlying", "right"]),
            ))
    return out


@rule
def volatility_is_plausible(frame: pl.DataFrame, ctx: ValidationContext) -> list[Finding]:
    if "iv" not in frame.columns or "is_option" not in frame.columns:
        return []
    out = []

    missing = pl.col("is_option") & ((pl.col("iv") <= 0) | pl.col("iv").is_null())
    count = frame.filter(missing).height
    if count:
        out.append(Finding(
            WARNING, "missing_iv",
            f"{count:,} option rows have no implied vol; they will price at intrinsic",
            count=count,
            sample=_sample(frame, missing, ["underlying", "strike", "iv"]),
        ))

    # A vol above 500% is almost always a unit error -- 30 meaning 30%, not 3000%.
    absurd = pl.col("iv") > MAX_PLAUSIBLE_IV
    count = frame.filter(absurd).height
    if count:
        out.append(Finding(
            WARNING, "implausible_iv",
            f"{count:,} rows have implied vol above {MAX_PLAUSIBLE_IV:.0%}; check the scale "
            f"(a percent column needs the scale transform)",
            count=count,
            sample=_sample(frame, absurd, ["underlying", "iv"]),
        ))
    return out


@rule
def quantities_are_meaningful(frame: pl.DataFrame, ctx: ValidationContext) -> list[Finding]:
    if "qty" not in frame.columns:
        return []
    mask = (pl.col("qty") == 0) | pl.col("qty").is_null()
    count = frame.filter(mask).height
    if not count:
        return []
    return [Finding(
        WARNING, "zero_quantity",
        f"{count:,} rows have zero quantity and contribute nothing",
        count=count,
        sample=_sample(frame, mask, ["account", "underlying", "qty"]),
    )]


@rule
def row_count_has_not_drifted(frame: pl.DataFrame, ctx: ValidationContext) -> list[Finding]:
    """A truncated file is the classic silent failure: it loads, and it lies."""
    if not ctx.previous_rows:
        return []

    drift = abs(frame.height - ctx.previous_rows) / ctx.previous_rows
    if drift < ROW_DRIFT_WARNING:
        return []

    direction = "fewer" if frame.height < ctx.previous_rows else "more"
    severity = ERROR if drift >= ROW_DRIFT_ERROR else WARNING
    return [Finding(
        severity, "row_drift",
        f"{frame.height:,} rows is {drift:.0%} {direction} than the previous batch "
        f"({ctx.previous_rows:,}); the file may be truncated or doubled",
        count=abs(frame.height - ctx.previous_rows),
    )]


@rule
def accounts_have_not_vanished(frame: pl.DataFrame, ctx: ValidationContext) -> list[Finding]:
    if not ctx.previous_accounts or "account" not in frame.columns:
        return []
    now = frame["account"].n_unique()
    if now >= ctx.previous_accounts * (1 - ROW_DRIFT_WARNING):
        return []
    return [Finding(
        WARNING, "accounts_missing",
        f"{now:,} distinct accounts, down from {ctx.previous_accounts:,}",
        count=ctx.previous_accounts - now,
    )]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def validate(frame: pl.DataFrame, ctx: ValidationContext | None = None) -> ValidationReport:
    """Run every rule and return everything found, not just the first failure."""
    ctx = ctx or ValidationContext()
    report = ValidationReport(rows=frame.height)
    for rule_fn in RULES:
        try:
            report.findings.extend(rule_fn(frame, ctx))
        except Exception as exc:
            # A rule that cannot run is an ERROR, not a warning. Downgrading it
            # would let a file nobody managed to check pass as checked, which is
            # precisely the failure this module exists to prevent -- and it did
            # exactly that once, hiding nulls in a required field behind a
            # crash in the sampling helper.
            report.findings.append(Finding(
                ERROR, "rule_failed",
                f"validation rule {rule_fn.__name__} could not run: {exc}",
            ))
        if report.findings and report.findings[-1].code == "empty_file":
            break  # every later rule would just restate it
    return report
