"""Column templates and shock configs.

These are the two dropdowns in the incumbent's toolbar, and they are
deliberately separate things:

  ShockConfig     defines the scenario grid -- what gets computed.
  ColumnTemplate  defines which of those scenarios, and which greeks, are
                  displayed and in what order.

Keeping them apart is why the incumbent can compute a 10 x 5 grid and show only
the four price columns at flat vol. Template columns therefore address
scenarios by (price, vol) coordinate rather than by index, so swapping the
shock config does not silently repoint a column at a different scenario.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .aggregate import GREEK_COLUMNS
from .scenarios import ScenarioGrid, pct_grid, sigma_grid

# Display formats the frontend knows how to render.
FORMATS = ("integer", "money", "decimal", "percent", "text")


# --------------------------------------------------------------------------
# Shock configs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ShockAxis:
    """One axis of the scenario grid."""

    count: int = 2          # points per side
    step: float = 1.0       # increment per point (sigma multiples, or absolute)
    symmetric: bool = True
    include_zero: bool = False

    def points(self) -> np.ndarray:
        pos = [self.step * (i + 1) for i in range(self.count)]
        vals = list(pos)
        if self.symmetric:
            vals = [-p for p in reversed(pos)] + vals
        if self.include_zero:
            vals = sorted(vals + [0.0])
        return np.array(vals, dtype=float)


@dataclass(frozen=True)
class ShockConfig:
    """A named scenario grid definition."""

    name: str = "Exposure"
    # "sigma" moves are per-underlying historical stddev multiples; "pct" is a
    # flat percentage applied to every underlying.
    price_type: str = "sigma"
    price: ShockAxis = field(default_factory=lambda: ShockAxis(count=2, step=1.0, symmetric=True))
    vol: ShockAxis = field(default_factory=lambda: ShockAxis(count=2, step=0.05, symmetric=True, include_zero=True))
    horizon_days: float = 1.0
    # Shocking the smile per strike rather than shifting it in parallel. Not
    # implemented -- scenarios.py only does parallel shifts. Rejected rather
    # than silently ignored so a config cannot quietly lie about what it ran.
    by_strike: bool = False

    def __post_init__(self) -> None:
        if self.price_type not in ("sigma", "pct"):
            raise ValueError(f"unknown price_type: {self.price_type}")
        if self.by_strike:
            raise NotImplementedError(
                "by-strike vol shocks are not implemented; scenarios.py shifts "
                "the surface in parallel only"
            )

    def to_grid(self) -> ScenarioGrid:
        build = sigma_grid if self.price_type == "sigma" else pct_grid
        return build(
            self.price.points(),
            self.vol.points(),
            days_forward=self.horizon_days,
        )

    @property
    def scenario_count(self) -> int:
        return len(self.price.points()) * len(self.vol.points())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ShockConfig:
        data = dict(data)
        for axis in ("price", "vol"):
            if isinstance(data.get(axis), dict):
                data[axis] = ShockAxis(**data[axis])
        return cls(**data)


# --------------------------------------------------------------------------
# Column templates
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    """One grid column.

    `kind="measure"` names a frame column (delta, market_value, worst).
    `kind="scenario"` addresses a scenario by its (price, vol) coordinate.
    """

    kind: str
    label: str
    name: str = ""
    price: float | None = None
    vol: float | None = None
    format: str = "money"
    width: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("measure", "scenario"):
            raise ValueError(f"unknown column kind: {self.kind}")
        if self.kind == "measure" and not self.name:
            raise ValueError("measure columns need a name")
        if self.kind == "scenario" and (self.price is None or self.vol is None):
            raise ValueError("scenario columns need price and vol coordinates")


@dataclass(frozen=True)
class ResolvedColumn:
    """A template column bound to an actual frame column for a given grid."""

    field: str
    label: str
    format: str
    width: int | None = None
    missing: bool = False


@dataclass(frozen=True)
class ColumnTemplate:
    name: str = "Exposure"
    columns: tuple[Column, ...] = ()

    def resolve(self, grid: ScenarioGrid) -> list[ResolvedColumn]:
        """Bind columns to frame fields for a specific scenario grid.

        A scenario column whose coordinate is absent from the grid is returned
        marked `missing` rather than dropped, so the UI can say the template and
        the config disagree instead of quietly showing fewer columns.
        """
        out: list[ResolvedColumn] = []
        for col in self.columns:
            if col.kind == "measure":
                out.append(ResolvedColumn(col.name, col.label, col.format, col.width))
                continue
            try:
                idx = grid.index_of(col.price, col.vol)
            except KeyError:
                out.append(ResolvedColumn("", col.label, col.format, col.width, missing=True))
            else:
                out.append(ResolvedColumn(f"s{idx:03d}", col.label, col.format, col.width))
        return out

    def measures(self) -> tuple[str, ...]:
        """Measure columns this template needs, for the pivot request."""
        named = tuple(c.name for c in self.columns if c.kind == "measure" and c.name in GREEK_COLUMNS)
        return named or GREEK_COLUMNS

    def to_dict(self) -> dict:
        return {"name": self.name, "columns": [asdict(c) for c in self.columns]}

    @classmethod
    def from_dict(cls, data: dict) -> ColumnTemplate:
        return cls(name=data["name"], columns=tuple(Column(**c) for c in data.get("columns", [])))


# --------------------------------------------------------------------------
# Defaults, mirroring the incumbent's "Exposure" screen
# --------------------------------------------------------------------------


def default_shock_config() -> ShockConfig:
    """Two price points per side at flat and shocked vol: a 4 x 5 = 20 grid."""
    return ShockConfig()


def default_column_template() -> ColumnTemplate:
    """Current NLV, the four Sdev contract-risk columns, and Max Risk."""
    cols = [
        Column("measure", "Current NLV", name="market_value", format="money", width=130),
        Column("measure", "Und Qty", name="und_qty", format="integer", width=110),
    ]
    for sd in (-2.0, -1.0, 1.0, 2.0):
        cols.append(Column("scenario", f"{sd:+.0f} Sdev : Contract Risk",
                           price=sd, vol=0.0, format="money", width=150))
    cols += [
        Column("measure", "Max Risk", name="worst", format="money", width=140),
        Column("measure", "Delta", name="delta", format="integer", width=110),
        Column("measure", "Gamma", name="gamma", format="decimal", width=110),
        Column("measure", "Vega", name="vega", format="decimal", width=110),
        Column("measure", "Theta", name="theta", format="decimal", width=110),
    ]
    return ColumnTemplate(name="Exposure", columns=tuple(cols))


def greeks_column_template() -> ColumnTemplate:
    return ColumnTemplate(
        name="Greeks",
        columns=(
            Column("measure", "Current NLV", name="market_value", format="money", width=130),
            Column("measure", "Delta", name="delta", format="integer", width=110),
            Column("measure", "Gamma", name="gamma", format="decimal", width=110),
            Column("measure", "Vega", name="vega", format="decimal", width=110),
            Column("measure", "Theta", name="theta", format="decimal", width=110),
            Column("measure", "Rho", name="rho", format="decimal", width=110),
        ),
    )


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


class TemplateStore:
    """JSON-backed storage, so templates follow a user rather than a machine."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.columns_dir = self.root / "columns"
        self.shocks_dir = self.root / "shocks"
        for d in (self.columns_dir, self.shocks_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._seed_defaults()

    def _seed_defaults(self) -> None:
        if not any(self.columns_dir.glob("*.json")):
            for tpl in (default_column_template(), greeks_column_template()):
                self.save_template(tpl)
        if not any(self.shocks_dir.glob("*.json")):
            self.save_config(default_shock_config())

    @staticmethod
    def _slug(name: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_ " else "-" for c in name)
        return safe.strip().replace(" ", "-").lower() or "unnamed"

    # -- column templates ---------------------------------------------------

    def list_templates(self) -> list[ColumnTemplate]:
        return sorted(
            (ColumnTemplate.from_dict(json.loads(p.read_text())) for p in self.columns_dir.glob("*.json")),
            key=lambda t: t.name,
        )

    def get_template(self, name: str) -> ColumnTemplate:
        path = self.columns_dir / f"{self._slug(name)}.json"
        if not path.exists():
            raise KeyError(name)
        return ColumnTemplate.from_dict(json.loads(path.read_text()))

    def save_template(self, template: ColumnTemplate) -> ColumnTemplate:
        path = self.columns_dir / f"{self._slug(template.name)}.json"
        path.write_text(json.dumps(template.to_dict(), indent=2))
        return template

    def delete_template(self, name: str) -> None:
        (self.columns_dir / f"{self._slug(name)}.json").unlink(missing_ok=True)

    # -- shock configs ------------------------------------------------------

    def list_configs(self) -> list[ShockConfig]:
        return sorted(
            (ShockConfig.from_dict(json.loads(p.read_text())) for p in self.shocks_dir.glob("*.json")),
            key=lambda c: c.name,
        )

    def get_config(self, name: str) -> ShockConfig:
        path = self.shocks_dir / f"{self._slug(name)}.json"
        if not path.exists():
            raise KeyError(name)
        return ShockConfig.from_dict(json.loads(path.read_text()))

    def save_config(self, config: ShockConfig) -> ShockConfig:
        path = self.shocks_dir / f"{self._slug(config.name)}.json"
        path.write_text(json.dumps(config.to_dict(), indent=2))
        return config

    def delete_config(self, name: str) -> None:
        (self.shocks_dir / f"{self._slug(name)}.json").unlink(missing_ok=True)
