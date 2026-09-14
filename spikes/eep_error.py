"""How wrong is the fast pricing model?

`scenario_pnl(model="eep")` holds the early exercise premium fixed at its base
value instead of repricing it in every scenario. That is a real approximation
and it should not be taken on trust -- this measures it against the exact
Bjerksund-Stensland reprice across the whole shock grid.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from risk.engine import base_valuation, scenario_pnl
from risk.scenarios import sigma_grid
from risk.synthetic import generate_book


def main(n: int = 200_000) -> None:
    book, sigma = generate_book(n, seed=11)
    book = base_valuation(book)
    grid = sigma_grid()

    exact = scenario_pnl(book, grid, sigma_daily=sigma, model="american", dtype=np.float64)
    fast = scenario_pnl(book, grid, sigma_daily=sigma, model="eep", dtype=np.float64)

    err = fast - exact
    gross = np.abs(exact)

    print(f"positions {n:,}   scenarios {len(grid)}\n")

    # Portfolio level is what the screen actually leads with.
    tot_e, tot_f = exact.sum(axis=0), fast.sum(axis=0)
    rel = np.abs(tot_f - tot_e) / np.maximum(np.abs(tot_e), 1.0)
    print("Portfolio P&L per scenario:")
    print(f"  max abs error      {np.abs(tot_f - tot_e).max():>14,.0f}")
    print(f"  max relative error {rel.max():>14.2%}")
    print(f"  median rel error   {np.median(rel):>14.2%}")

    worst_e, worst_f = exact.min(axis=1).sum(), fast.min(axis=1).sum()
    print(f"\nWorst-case total: exact {worst_e:,.0f}  fast {worst_f:,.0f} "
          f"({abs(worst_f - worst_e) / max(abs(worst_e), 1):.2%})")

    print(f"\nPosition level:")
    print(f"  mean abs error     {np.abs(err).mean():>14,.2f}")
    print(f"  99th pct abs error {np.percentile(np.abs(err), 99):>14,.2f}")
    print(f"  error / gross P&L  {np.abs(err).sum() / gross.sum():>14.3%}")

    # Where it goes wrong: puts driven deep into the money.
    is_put = (~book["is_call"].to_numpy()) & book["is_option"].to_numpy()
    if is_put.any():
        print(f"  share of total error from puts {np.abs(err)[is_put].sum() / np.abs(err).sum():.1%}")


if __name__ == "__main__":
    main()
