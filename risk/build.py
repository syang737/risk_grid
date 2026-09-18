"""Build worker: turn a book into a persisted, indexed batch.

Split from the query service because the two want different machines. Building
is CPU-bound with a small working set (chunking keeps the float64 temporaries
near 3MB), so it wants cores and suits spot instances; serving wants to sit
still and answer quickly. Fusing them also means a rebuild competes with the
queries it is about to invalidate.

    python -m risk.build --firm acme --synthetic 250000
    python -m risk.build --firm acme --positions drop.parquet
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import asdict
from pathlib import Path

import polars as pl

from config import load_env

from .batch import build_batch, write_batch
from .storage import LocalStore, ObjectStore, S3Store
from .synthetic import generate_book
from .templates import ShockConfig, TemplateStore


def open_store(uri: str) -> ObjectStore:
    if uri.startswith("s3://"):
        bucket, _, prefix = uri[5:].partition("/")
        return S3Store(bucket, prefix)
    return LocalStore(uri)


def load_positions_file(path: Path) -> tuple[pl.DataFrame, "object"]:
    """Read a prepared position file.

    Mapping arbitrary clearing-firm exports onto the canonical schema is the
    admin layer's job (ingest/); this accepts a file already in that shape.
    """
    frame = pl.read_parquet(path) if path.suffix == ".parquet" else pl.read_csv(path)
    if "sigma_daily" in frame.columns:
        return frame.drop("sigma_daily"), frame["sigma_daily"].to_numpy()

    # Without a supplied volatility history, fall back to each position's own
    # implied vol -- defensible for a first batch, and visible as a warning
    # rather than silently pretending the shock template is calibrated.
    import numpy as np

    print("warning: no sigma_daily column; calibrating sigma from implied vol", file=sys.stderr)
    return frame, np.maximum(frame["iv"].to_numpy(), 0.01) / np.sqrt(252.0)


def build(
    firm_id: str,
    store: ObjectStore,
    positions: pl.DataFrame,
    sigma_daily,
    config: ShockConfig,
    label: str,
    batch_id: str | None = None,
    register: bool = True,
) -> dict:
    """Reprice, write artifacts, and index the result."""
    started = time.perf_counter()
    batch = build_batch(
        positions, sigma_daily, grid=config.to_grid(), label=label,
        batch_id=batch_id, shock_config=config.name, firm_id=firm_id,
    )
    priced = time.perf_counter() - started

    manifest = write_batch(batch, store)
    manifest.timings["reprice"] = priced
    manifest.timings["total"] = time.perf_counter() - started

    from .storage import BatchLayout, write_manifest

    write_manifest(store, BatchLayout(firm_id, batch.id), manifest)

    if register:
        from control.db import get_database
        from control.service import register_batch

        with get_database().transaction() as session:
            register_batch(session, asdict(manifest))

    return asdict(manifest)


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(description="Build a risk_grid batch")
    parser.add_argument("--firm", required=True)
    parser.add_argument("--store", default="./data", help="directory or s3://bucket/prefix")
    parser.add_argument("--templates", default=str(Path.home() / ".risk_grid"))
    parser.add_argument("--config", default="Exposure", help="shock config name")
    parser.add_argument("--label", default="IntraDay")
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--positions", type=Path, help="parquet or csv in canonical schema")
    parser.add_argument("--synthetic", type=int, help="generate N synthetic positions instead")
    parser.add_argument("--no-register", action="store_true", help="skip the control-plane index")
    args = parser.parse_args(argv)

    if not args.positions and not args.synthetic:
        parser.error("one of --positions or --synthetic is required")

    config = TemplateStore(args.templates).get_config(args.config)
    if args.synthetic:
        positions, sigma = generate_book(args.synthetic, seed=7)
    else:
        positions, sigma = load_positions_file(args.positions)

    manifest = build(
        args.firm, open_store(args.store), positions, sigma, config,
        args.label, args.batch_id, register=not args.no_register,
    )

    print(f"built {manifest['batch_id']} for {manifest['firm_id']}: "
          f"{manifest['positions']:,} positions, "
          f"{len(manifest['rollups'])} rollups, "
          f"{manifest['timings']['total']:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
