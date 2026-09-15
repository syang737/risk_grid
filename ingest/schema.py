"""The canonical position schema.

Every firm's export is mapped onto this. It is deliberately small: the fewer
fields a firm must supply, the shorter onboarding is, so anything that can be
derived or defaulted is.

The incumbents' moat is dozens of bespoke clearing-file parsers. Making this a
schema plus a mapping profile rather than a parser per firm is what inverts
that -- see docs/strategy.md section 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import polars as pl


@dataclass(frozen=True)
class Field:
    name: str
    dtype: Any
    required: bool = False
    default: Any = None
    derived_from: tuple[str, ...] = ()
    description: str = ""

    @property
    def mappable(self) -> bool:
        """Derived fields can still be mapped, if a firm supplies them directly."""
        return True


# Fields a firm must supply in some form. Everything else is derived or
# defaulted, and the mapping UI shows which is which.
FIELDS: tuple[Field, ...] = (
    # -- identity ---------------------------------------------------------
    Field("account", pl.Utf8, required=True, description="Account the position sits in"),
    Field("master_account", pl.Utf8, derived_from=("account",),
          description="Grouping above account; defaults to the account"),
    Field("desk", pl.Utf8, default="UNASSIGNED", description="Desk or business unit"),
    Field("firm", pl.Utf8, default="FIRM", description="Top of the account hierarchy"),

    # -- instrument -------------------------------------------------------
    Field("underlying", pl.Utf8, required=True, description="Underlying symbol"),
    Field("sector", pl.Utf8, default="Unclassified",
          description="Product or sector classification"),
    Field("instrument_type", pl.Utf8, derived_from=("strike",),
          description="OPTION or EQUITY; inferred from the presence of a strike"),
    Field("strike", pl.Float64, default=0.0, description="Option strike; 0 for equity"),
    Field("expiry", pl.Utf8, default="-", description="Expiry date or tenor label"),
    Field("right", pl.Utf8, default="-", description="C or P; '-' for equity"),
    Field("contract", pl.Utf8, derived_from=("underlying", "expiry", "strike", "right"),
          description="Tradeable line identifier; built from the parts if absent"),

    # -- position ---------------------------------------------------------
    Field("qty", pl.Float64, required=True, description="Signed quantity; negative is short"),
    Field("multiplier", pl.Float64, derived_from=("instrument_type",),
          description="Contract multiplier; 100 for options, 1 for equity"),

    # -- market -----------------------------------------------------------
    Field("underlying_price", pl.Float64, required=True, description="Underlying mark"),
    Field("iv", pl.Float64, default=0.0, description="Implied volatility as a decimal"),
    Field("dte", pl.Float64, derived_from=("expiry",),
          description="Calendar days to expiry; computed from expiry against the batch date"),
    Field("rate", pl.Float64, default=0.042, description="Risk-free rate"),
    Field("div_yield", pl.Float64, default=0.0, description="Continuous dividend yield"),

    # -- calibration ------------------------------------------------------
    Field("sigma_daily", pl.Float64, derived_from=("iv",),
          description="Underlying's daily stddev; falls back to implied vol if absent"),
)

BY_NAME: dict[str, Field] = {f.name: f for f in FIELDS}
REQUIRED = tuple(f.name for f in FIELDS if f.required)

# Derived by `finalise` rather than mapped, and computed from other fields.
DERIVED = tuple(f.name for f in FIELDS if f.derived_from)


def describe() -> list[dict]:
    """Field catalogue for the mapping UI."""
    return [
        {
            "name": f.name,
            "required": f.required,
            "derived": bool(f.derived_from),
            "derivedFrom": list(f.derived_from),
            "default": f.default,
            "description": f.description,
        }
        for f in FIELDS
    ]
