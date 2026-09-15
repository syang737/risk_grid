"""What does persisting a batch cost, and what does it buy?

The all-in-RAM design from phase 1 is the most expensive way to run this, and
RAM is the resource that sizes the bill. This measures whether it is necessary:

  1. build vs load      -- is reloading a batch cheaper than repricing it?
  2. artifact sizes     -- what does retention actually cost?
  3. serving latency    -- RAM vs a lazy Parquet scan vs a materialised rollup
  4. retention          -- projected storage per firm per year

Every number quoted in docs/hosting.md comes from here.

Run:  python spikes/storage_spike.py [--positions 2000000]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import polars as pl

from risk.aggregate import PivotRequest, aggregate, scenario_columns
from risk.batch import build_batch, load_batch, write_batch
from risk.scenarios import sigma_grid
from risk.storage import BatchLayout, LocalStore, read_manifest
from risk.synthetic import generate_book

# S3 Standard, us-east-1, per GB-month.
S3_GB_MONTH = 0.023


def timed(fn):
    t0 = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - t0


def best(fn, reps: int = 5) -> float:
    fn()
    return min(timed(fn)[1] for _ in range(reps)) * 1000


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--positions", type=int, default=2_000_000)
    parser.add_argument("--batches-per-day", type=int, default=14)
    parser.add_argument("--trading-days", type=int, default=250)
    args = parser.parse_args()

    grid = sigma_grid()
    print(f"=== {args.positions:,} positions, {len(grid)} scenarios ===\n")

    positions, sigma = generate_book(args.positions, seed=7)

    with tempfile.TemporaryDirectory() as tmp:
        store = LocalStore(tmp)

        batch, build_s = timed(
            lambda: build_batch(positions, sigma, grid=grid, batch_id="b1", firm_id="f1")
        )
        print(f"build (reprice from scratch)        {build_s:8.1f} s")

        _, write_s = timed(lambda: write_batch(batch, store))
        print(f"write artifacts                     {write_s:8.1f} s")

        loaded, load_s = timed(lambda: load_batch(store, "f1", "b1"))
        print(f"load from Parquet (lazy)            {load_s:8.2f} s")
        eager, eager_s = timed(lambda: load_batch(store, "f1", "b1", eager=True))
        print(f"load from Parquet (eager)           {eager_s:8.2f} s")
        print(f"  -> reloading beats repricing by   {build_s / max(eager_s, 1e-9):8.1f}x\n")

        manifest = read_manifest(store, BatchLayout("f1", "b1"))
        full = manifest.sizes["full"]
        cold = manifest.sizes["positions"]
        rollups = sum(v for k, v in manifest.sizes.items() if k.startswith("rollup_"))

        print("artifact sizes")
        print(f"  full batch                        {full/1e9:8.2f} GB")
        print(f"  positions only (repriceable)      {cold/1e9:8.2f} GB  ({full/cold:.1f}x smaller)")
        print(f"  all rollups                       {rollups/1e6:8.2f} MB  ({100*rollups/full:.1f}% of full)")
        skipped = [d for d in loaded.dimensions if d not in manifest.rollups]
        print(f"  rolled up                         {len(manifest.rollups)} dimensions")
        print(f"  too high-cardinality to roll up   {skipped or 'none'}\n")

        request = PivotRequest(
            dimensions=("sector", "underlying", "contract"),
            detail_dimensions=("account", "desk"),
        )
        scen = scenario_columns(batch.frame)
        uri = store.uri(BatchLayout("f1", "b1").full)

        def scan_pivot(columns):
            return (
                pl.scan_parquet(uri)
                .group_by("sector")
                .agg([pl.len()] + [pl.col(c).sum() for c in columns]
                     + [pl.col("delta").sum(), pl.col("market_value").sum()])
                .collect()
            )

        print("pivot by sector")
        print(f"  in RAM                            {best(lambda: aggregate(batch.frame, request)):8.1f} ms")
        print(f"  scan Parquet, all {len(scen)} scenarios   {best(lambda: scan_pivot(scen)):8.1f} ms")
        print(f"  scan Parquet, 6 scenarios         {best(lambda: scan_pivot(scen[:6])):8.1f} ms")
        print(f"  materialised rollup               {best(lambda: loaded.aggregate(request)):8.1f} ms")

        # Drilling has to be measured on a batch that has not seen the request
        # before, or the aggregate cache answers it and the number below
        # describes the cache rather than the scan.
        drill = request.child(aggregate(batch.frame, request).rows["sector"][0])
        cold_total = 0.0
        reps = 3
        for _ in range(reps):
            fresh = load_batch(store, "f1", "b1")
            cold_total += timed(lambda: fresh.aggregate(drill))[1]
        print(f"  drill one level, cold scan        {cold_total / reps * 1000:8.1f} ms")
        print(f"  drill one level, cached           {best(lambda: loaded.aggregate(drill)):8.1f} ms\n")

        per_year = args.batches_per_day * args.trading_days
        print(f"storage per firm per year ({args.batches_per_day} batches/day x {args.trading_days} days)")
        for label, size in (
            ("keep every full batch", full),
            ("keep positions only", cold),
            ("30d full + positions cold", full * args.batches_per_day * 30 / per_year + cold),
        ):
            tb = size * per_year / 1e12
            print(f"  {label:34s} {tb:7.2f} TB   ~${tb * 1000 * S3_GB_MONTH:6.0f}/mo")


if __name__ == "__main__":
    main()
