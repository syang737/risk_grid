# risk_grid

Browser-based post-trade stress and position analytics for broker-dealers.

Replacing the Citrix-delivered legacy systems that pivot a firm's post-trade
book by instrument and account level, showing greeks, reference data, and P&L
under configurable historical-stddev shock templates.

## Status

Pre-product. Two questions were worth answering before writing any product code,
and both now have measured answers:

- **Does the architecture scale?** Yes. Drilling into a pivot node is 8–155 ms
  from 100K to 5M positions, because the browser never receives positions — only
  the children of the expanded node. See [docs/architecture.md](docs/architecture.md).
- **Is the market real?** Yes, but tiered, and only one tier is addressable
  bootstrapped. See [docs/strategy.md](docs/strategy.md).

## Quick start

```bash
pip install -e ".[dev]"
pytest                          # pricer correctness
python spikes/perf_spike.py     # the scaling table
python spikes/eep_error.py      # pricing approximation error study
```

## Layout

| Path | What |
|---|---|
| `risk/pricing.py` | Vectorized Black-Scholes + greeks, Bjerksund-Stensland American |
| `risk/scenarios.py` | Shock templates: flat pct or per-underlying sigma units |
| `risk/engine.py` | Base valuation and the position × scenario P&L matrix |
| `risk/aggregate.py` | Server-side pivot, Arrow IPC node pages |
| `risk/synthetic.py` | Realistically-shaped synthetic book generator |
| `spikes/` | Performance and accuracy studies |
| `docs/` | Strategy and architecture |
