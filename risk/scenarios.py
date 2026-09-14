"""Shock templates.

A scenario is a joint move in (underlying, implied vol, time). The grid is the
cross product of the axes a user configures, which is how the incumbent
templates work: a row of underlying moves against a column of vol shifts.

Underlying moves can be expressed two ways:

  "pct"    a flat percentage applied to every underlying
  "sigma"  a multiple of each underlying's own historical daily stddev, scaled
           to the horizon. This is the "historical stddev move" template, and
           it is the one that actually differentiates a stress screen -- a 2
           sigma move means something different for a utility than for a
           biotech, and a flat +/-10% grid hides that.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Trading days per year, for scaling daily stddev to a horizon.
TRADING_DAYS = 252.0


@dataclass(frozen=True)
class ScenarioGrid:
    """A set of S scenarios applied jointly to every position."""

    underlying_shock: np.ndarray  # (S,) pct (e.g. 0.10) or sigma multiples
    vol_shock: np.ndarray  # (S,) additive vol points, e.g. 0.05 == +5 vol
    days_forward: np.ndarray  # (S,) calendar days of decay
    labels: tuple[str, ...]
    shock_unit: str = "pct"  # "pct" | "sigma"

    def __post_init__(self) -> None:
        n = len(self.underlying_shock)
        if not (len(self.vol_shock) == len(self.days_forward) == len(self.labels) == n):
            raise ValueError("scenario axes must all have the same length")
        if self.shock_unit not in ("pct", "sigma"):
            raise ValueError(f"unknown shock_unit: {self.shock_unit}")

    def __len__(self) -> int:
        return len(self.underlying_shock)

    def index_of(self, price: float, vol: float, tol: float = 1e-9) -> int:
        """Locate a scenario by its (price, vol) coordinates.

        Column templates select scenarios this way rather than by position, so
        a template survives a change to the shock config -- swapping a 2-point
        price axis for a 4-point one keeps "-2 sigma, flat vol" pointing at the
        same thing instead of silently shifting to a different column.
        """
        hits = np.flatnonzero(
            (np.abs(self.underlying_shock - price) <= tol) & (np.abs(self.vol_shock - vol) <= tol)
        )
        if not len(hits):
            raise KeyError(f"no scenario at price={price}, vol={vol}")
        return int(hits[0])

    def coordinates(self) -> list[tuple[float, float]]:
        return list(zip(self.underlying_shock.tolist(), self.vol_shock.tolist()))

    def move_pct(self, sigma_daily: np.ndarray | None, horizon_days: float = 1.0) -> np.ndarray:
        """Resolve underlying shocks to actual percentage moves.

        Returns (S,) for "pct", or (N, S) for "sigma" where each position's move
        depends on its own underlying's volatility.
        """
        if self.shock_unit == "pct":
            return self.underlying_shock
        if sigma_daily is None:
            raise ValueError("sigma_daily is required for a sigma-unit grid")
        scale = np.sqrt(horizon_days)
        return sigma_daily[:, None] * scale * self.underlying_shock[None, :]


def _cross(under: np.ndarray, vols: np.ndarray, days: float, unit: str, fmt: str) -> ScenarioGrid:
    u_grid, v_grid = np.meshgrid(under, vols, indexing="ij")
    u_flat, v_flat = u_grid.ravel(), v_grid.ravel()
    labels = tuple(
        f"{fmt.format(u)} / {v * 100:+.0f}v" for u, v in zip(u_flat, v_flat)
    )
    return ScenarioGrid(
        underlying_shock=u_flat,
        vol_shock=v_flat,
        days_forward=np.full(u_flat.shape, days, dtype=np.float64),
        labels=labels,
        shock_unit=unit,
    )


def pct_grid(
    moves: np.ndarray | None = None,
    vol_shocks: np.ndarray | None = None,
    days_forward: float = 1.0,
) -> ScenarioGrid:
    """Flat percentage grid. The default is a 10 x 5 = 50 scenario template."""
    if moves is None:
        moves = np.linspace(-0.30, 0.30, 10)
    if vol_shocks is None:
        vol_shocks = np.array([-0.10, -0.05, 0.0, 0.05, 0.10])
    return _cross(np.asarray(moves, float), np.asarray(vol_shocks, float),
                  days_forward, "pct", "{:+.1%}")


def sigma_grid(
    sigmas: np.ndarray | None = None,
    vol_shocks: np.ndarray | None = None,
    days_forward: float = 1.0,
) -> ScenarioGrid:
    """Historical-stddev grid: moves in per-underlying sigma units."""
    if sigmas is None:
        sigmas = np.array([-4.0, -3.0, -2.0, -1.5, -1.0, 1.0, 1.5, 2.0, 3.0, 4.0])
    if vol_shocks is None:
        vol_shocks = np.array([-0.10, -0.05, 0.0, 0.05, 0.10])
    return _cross(np.asarray(sigmas, float), np.asarray(vol_shocks, float),
                  days_forward, "sigma", "{:+.1f}sd")


def daily_sigma_from_returns(returns: np.ndarray) -> np.ndarray:
    """Per-underlying daily stddev from a (U, D) matrix of daily log returns."""
    return np.nanstd(returns, axis=1, ddof=1)


def annualize(sigma_daily: np.ndarray) -> np.ndarray:
    return sigma_daily * np.sqrt(TRADING_DAYS)
