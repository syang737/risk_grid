"""Does the architecture actually hold at book scale?

Measures the three things that decide it:

  1. matrix build   -- repricing every position under every scenario (the only
                       genuinely expensive step, and it happens once per snapshot)
  2. pivot          -- groupby-sum over the matrix; this is what a user feels
                       every time they drag a dimension or expand a node
  3. wire           -- Arrow IPC serialization of one node page

Run:  python spikes/perf_spike.py [--scales 100000,1000000,5000000]
"""

from __future__ import annotations

import argparse
import gc
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import polars as pl

from risk.aggregate import (INSTRUMENT_DRILL, PivotRequest, aggregate,
                            attach_scenarios, pivot, to_arrow_ipc)
from risk.engine import base_valuation, scenario_pnl
from risk.scenarios import sigma_grid
from risk.synthetic import generate_book


def peak_rss_gb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


class timer:
    def __init__(self, label: str, sink: dict):
        self.label, self.sink = label, sink

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.sink[self.label] = (time.perf_counter() - self.t0) * 1000.0


def run_scale(n: int, n_scenarios_axis=(10, 5), repeats: int = 3) -> dict:
    t: dict[str, float] = {}
    grid = sigma_grid()
    assert len(grid) == n_scenarios_axis[0] * n_scenarios_axis[1]

    with timer("generate", t):
        book, sigma_daily = generate_book(n, seed=7)

    with timer("base_valuation", t):
        book = base_valuation(book)

    with timer("matrix_build", t):
        pnl = scenario_pnl(book, grid, sigma_daily=sigma_daily, horizon_days=1.0)

    # For reference only: the fast pricing model is too inaccurate to ship
    # (see spikes/eep_error.py), but the gap sizes the cost of being exact.
    with timer("matrix_build_eep", t):
        scenario_pnl(book, grid, sigma_daily=sigma_daily, horizon_days=1.0, model="eep")

    matrix_gb = pnl.nbytes / 1024**3

    with timer("attach", t):
        df = attach_scenarios(book, pnl, grid.labels)

    del pnl
    gc.collect()

    # The interaction that matters: top-of-book, then drilling in.
    def bench(label, fn):
        # Warm once, then take the best of `repeats` -- we care about the
        # steady-state interaction cost, not first-touch page faults.
        fn()
        best = min(_time(fn) for _ in range(repeats))
        t[label] = best

    def _time(fn):
        t0 = time.perf_counter()
        fn()
        return (time.perf_counter() - t0) * 1000.0

    bench("pivot_by_desk", lambda: pivot(df, ["desk"]))
    bench("pivot_by_underlying", lambda: pivot(df, ["underlying"]))
    bench("pivot_desk_x_account", lambda: pivot(df, ["desk", "account"]))
    bench("pivot_by_expiry_strike", lambda: pivot(df, ["expiry", "strike"]))

    top_desk = pivot(df, ["desk"]).sort("positions", descending=True)["desk"][0]
    drill = PivotRequest(dimensions=("desk", "account"))
    bench("drill_into_desk", lambda: aggregate(df, drill.child(top_desk)))

    # The distinct-value cells behind the chip renderer: this is the cost of
    # fixing the incumbent's useless [21] cell.
    with_details = PivotRequest(dimensions=INSTRUMENT_DRILL,
                                detail_dimensions=("account", "underlying", "expiry", "desk"))
    bench("pivot_with_detail_cells", lambda: aggregate(df, with_details))

    node = pivot(df, ["desk"])
    with timer("arrow_ipc", t):
        payload = to_arrow_ipc(node)

    t["_rows_returned"] = len(node)
    t["_payload_kb"] = len(payload) / 1024
    t["_matrix_gb"] = matrix_gb
    t["_peak_rss_gb"] = peak_rss_gb()

    del df
    gc.collect()
    return t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scales", default="100000,1000000,5000000")
    args = ap.parse_args()
    scales = [int(s) for s in args.scales.split(",")]

    rows = []
    for n in scales:
        print(f"=== {n:,} positions ===", flush=True)
        r = run_scale(n)
        rows.append((n, r))
        for k, v in r.items():
            unit = "" if k.startswith("_") else " ms"
            print(f"  {k:24s} {v:10.1f}{unit}", flush=True)

    print("\n\n## Results\n")
    cols = ["matrix_build", "pivot_by_desk", "pivot_by_underlying",
            "pivot_desk_x_account", "pivot_by_expiry_strike",
            "pivot_with_detail_cells", "drill_into_desk", "arrow_ipc"]
    header = "| positions | " + " | ".join(c.replace("_", " ") for c in cols) + " | matrix | peak RSS |"
    print(header)
    print("|" + "---|" * (len(cols) + 3))
    for n, r in rows:
        cells = " | ".join(f"{r[c]:.0f} ms" for c in cols)
        print(f"| {n:,} | {cells} | {r['_matrix_gb']:.2f} GB | {r['_peak_rss_gb']:.1f} GB |")


if __name__ == "__main__":
    main()
