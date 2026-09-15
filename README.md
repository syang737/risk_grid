# risk_grid

Browser-based post-trade stress and position analytics for broker-dealers.

Replacing the Citrix-delivered legacy systems that pivot a firm's post-trade
book by instrument and account level, showing greeks, reference data, and P&L
under configurable historical-stddev shock templates.

## What works today

A running stack: a synthetic book of any size, valued and stressed, served
through a pivot API into a browser grid.

- **Pivot any way.** One composable request — an ordered dimension list, an
  expansion path, filters, measures — replaces what the incumbent hardcodes as
  four toolbar tabs. Drill account → instrument or instrument → account.
- **Expand in place.** Parents stay visible, several branches open at once, so
  you can compare siblings instead of losing your place.
- **Counts you can click.** A dimension that is not the grouping key shows its
  value when the group holds one, and otherwise a chip — `2,528 accounts` —
  that regroups underneath the row when clicked. That is the incumbent's
  useless `[21]` cell turned into the cross-dimension drill.
- **Shock configs and column templates**, edited live. Templates address
  scenarios by `(price, vol)` coordinate, so changing the config reports which
  columns it can no longer supply instead of silently repointing them.
- **Pinned totals** that describe exactly the rows on screen, worst-of-sum.
- **Multi-tenant**, with position data isolated per firm by both authorisation
  and containment — a query process serves exactly one firm and refuses the rest.
- **Batches are persisted**, so restarting costs 1.2s instead of repricing, and
  the root level of every dimension answers from a rollup in under a millisecond.

![the grid](docs/images/grid.png)

## Running it

```bash
pip install -e ".[dev]"

# Control plane, a firm, and an API key (printed once).
python -m control.bootstrap --firm acme --name "Acme Securities" \
    --email ops@acme.test --role firm_admin

# Build a batch. A separate process from the query service on purpose.
python -m risk.build --firm acme --store ./data --synthetic 200000

# Query service, pinned to one firm.
RISK_GRID_FIRM=acme python -m uvicorn api.main:app --port 8000

# Frontend. Put the token from bootstrap in web/.env.local as VITE_API_TOKEN.
cd web && npm install && npm run dev           # http://localhost:5173
```

Settings and deployment topology are in [docs/hosting.md](docs/hosting.md).
AG Grid Enterprise runs unlicensed with a watermark; set `VITE_AG_GRID_LICENSE`
to clear it.

```bash
pytest                          # 100 tests
python spikes/perf_spike.py     # the scaling table
python spikes/storage_spike.py  # persistence, rollups, retention cost
python spikes/eep_error.py      # pricing approximation study
```

## Why it scales

Shock P&L is independent per position and aggregation is a sum, so the
position × scenario matrix is built once and every pivot is a groupby-sum over
it. The browser never receives positions — only the children of the expanded
node, about 13 KB. Full numbers in [docs/architecture.md](docs/architecture.md).

## Layout

| Path | What |
|---|---|
| `risk/pricing.py` | Vectorized Black-Scholes + greeks, Bjerksund-Stensland American |
| `risk/scenarios.py` | Shock grids: flat percent or per-underlying sigma units |
| `risk/engine.py` | Base valuation and the position × scenario P&L matrix |
| `risk/aggregate.py` | Composable pivots, distinct-value cells, totals |
| `risk/batch.py` | Snapshot identity, batch store, rollups, aggregate cache |
| `risk/storage.py` | Parquet artifacts, manifest, local and S3 backends |
| `risk/build.py` | Build worker: reprice, write artifacts, index the batch |
| `control/` | Firms, users, API keys, batch index, audit log |
| `risk/templates.py` | Column templates and shock configs |
| `risk/synthetic.py` | Realistically-shaped synthetic book generator |
| `api/` | FastAPI, AG Grid server-side row model contract |
| `web/` | React + AG Grid Enterprise frontend |
| `spikes/` | Performance and accuracy studies |
| `docs/` | [Strategy](docs/strategy.md), [architecture](docs/architecture.md), [hosting](docs/hosting.md) |
