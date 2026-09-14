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

![the grid](docs/images/grid.png)

## Running it

```bash
pip install -e ".[dev]"
python -m uvicorn api.main:app --port 8000     # API + a synthetic batch
cd web && npm install && npm run dev           # http://localhost:5173
```

`RISK_GRID_POSITIONS` sets the synthetic book size (default 250,000).
AG Grid Enterprise runs unlicensed with a watermark; set
`VITE_AG_GRID_LICENSE` to clear it.

```bash
pytest                          # 72 tests
python spikes/perf_spike.py     # the scaling table
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
| `risk/batch.py` | Snapshot identity, batch store, aggregate cache |
| `risk/templates.py` | Column templates and shock configs |
| `risk/synthetic.py` | Realistically-shaped synthetic book generator |
| `api/` | FastAPI, AG Grid server-side row model contract |
| `web/` | React + AG Grid Enterprise frontend |
| `spikes/` | Performance and accuracy studies |
| `docs/` | [Strategy](docs/strategy.md) and [architecture](docs/architecture.md) |
