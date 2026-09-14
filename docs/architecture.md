# risk_grid — architecture

All numbers here are measured on this repo, not estimated. Reproduce with
`python spikes/perf_spike.py`. Test box: 4 cores, 15GB RAM — deliberately modest,
so these are a floor rather than a ceiling.

---

## The core idea

Shock P&L is **independent per position**, and aggregation is **a sum**.

So compute a position × scenario P&L matrix once, and every pivot the user drags
afterwards is a groupby-sum over that matrix. No repricing on interaction.
Drilling from firm level into a single account is the same operation at a
different grain.

```
positions (N)                    scenarios (S = 50)
     │                                  │
     └──────────► reprice ◄─────────────┘
                     │
                (N × S) float32 P&L matrix        ← built once per snapshot
                     │
              groupby-sum on any dimension        ← every user interaction
                     │
            expanded tree nodes only (~25 rows)
                     │
                Arrow IPC over the wire
```

Two consequences worth stating plainly:

- **The browser never receives positions.** It receives the children of whatever
  node was expanded — typically tens of rows. The "millions of rows in a browser"
  problem is avoided rather than solved.
- **Cost scales with the data under the expanded node**, not with book size.

Post-trade scope makes this work: the book is a snapshot, so the matrix is
rebuilt on a schedule rather than maintained against a tick stream.

---

## Measured results

50 scenarios (10 sigma-moves × 5 vol shifts), ~85% options, exact American
repricing.

| positions | snapshot build | pivot by desk | pivot by underlying | desk × account | expiry × strike | drill into node | Arrow IPC | matrix | peak RSS |
|---|---|---|---|---|---|---|---|---|---|
| 100,000 | 1.2 s | 6 ms | 19 ms | 75 ms | 54 ms | 8 ms | 2 ms | 0.02 GB | 0.5 GB |
| 1,000,000 | 9.5 s | 27 ms | 144 ms | 207 ms | 238 ms | 31 ms | 2 ms | 0.19 GB | 1.6 GB |
| 5,000,000 | 40.3 s | 120 ms | 1,610 ms | 1,601 ms | 1,643 ms | 155 ms | 2 ms | 0.93 GB | 5.4 GB |

A node page is ~13 KB on the wire regardless of book size.

### Reading these honestly

**The interactive claim holds.** Drilling into a node — by far the most common
interaction — is 8–155 ms across the whole range. Full-book re-pivots are 27–238
ms at 1M.

**At 5M, full-book re-pivots reach 1.6 s**, which is past the interactive
threshold. Mitigation is straightforward and not yet built: the top few pivot
levels do not change between snapshots, so they can be materialized once when the
matrix is built. Only drill-downs need to be computed live, and those are already
fast. This is worth doing before any customer has a 5M-row book, not after.

**The snapshot build missed the bar I originally set** ("a few seconds"). That
bar was wrong, not the result: the build is a batch step that runs once per
snapshot, where 40 s is operationally irrelevant. The number that had to be
interactive is the pivot, and it is.

**Memory is a non-issue.** 5.4 GB peak for a 5M-position book on a 15 GB box.

### What made it fast

Two changes, both worth more than any language choice:

1. **Chunk size, for cache residency — 3–4x.** At 50 scenarios an 8,000-row
   chunk keeps float64 temporaries near 3 MB, which fits L3. Chunks of 250,000
   measured 3–4x slower at identical thread counts. This dwarfed the gain from
   threading.
2. **Branch-free pricing.** Masking expired/zero-vol rows forces fancy-index
   copies of every input; clipping and patching afterwards is faster at book
   scale. Put prices come from put-call parity rather than two more normal CDF
   evaluations.

Threading adds ~2x on 4 cores on top of that. The work is embarrassingly parallel
and numpy/scipy release the GIL, so threads suffice — no pickling, no processes.

---

## A rejected shortcut, and why it matters

The obvious optimization is to reprice European in each scenario and carry the
**early exercise premium** through unchanged from the base snapshot. It is ~2.7x
faster (3.5 s vs 9.5 s at 1M).

It is also not accurate enough to ship, and the way it fails is instructive.
Measured on a 200k book with `spikes/eep_error.py`:

| Metric | Result |
|---|---|
| Total worst-case P&L error | **0.00%** |
| Max per-scenario error, scenarios with large P&L | **44%** |
| Median per-scenario relative error | 5.8% |
| Share of error from puts | **96%** |

The headline number is reassuring and misleading. Aggregate worst-case lands
almost exactly right because errors cancel; per scenario there is a systematic
~$3MM bias in the near-the-money rows. The cause is mechanical: as the underlying
falls, puts go deep in the money and their early exercise premium grows — which
is exactly the quantity this model holds fixed. `test_american_put_premium_grows_with_moneyness`
in `tests/test_pricing.py` pins that behaviour.

Kept as `model="eep"` for previews, clearly marked. **Default is exact
Bjerksund-Stensland.** The lesson generalizes: validate an approximation against
the number the screen actually displays, not against a portfolio aggregate where
errors cancel.

---

## Layout

```
risk/
  pricing.py     Vectorized Black-Scholes + greeks; Bjerksund-Stensland 1993
                 for American. No per-position loops anywhere.
  scenarios.py   Shock templates. Moves in flat pct or per-underlying sigma
                 units ("historical stddev" templates).
  engine.py      Base valuation and the (N x S) P&L matrix. Chunked + threaded.
  aggregate.py   Server-side pivot; Arrow IPC serialization of node pages.
  synthetic.py   Book generator shaped like a real BD book (skewed accounts,
                 chains fanning across expiries/strikes) — pivot cost depends
                 on cardinality and skew, not just row count.
spikes/
  perf_spike.py  The table above.
  eep_error.py   The approximation error study above.
tests/
  test_pricing.py  Hull reference values, put-call parity, greeks vs finite
                   difference, American >= European >= intrinsic.
```

### Why Bjerksund-Stensland 1993 and not 2002

2002 is more accurate but needs a **bivariate** normal CDF, which has no fast
vectorized implementation in numpy/scipy. At book scale that cost outweighs the
accuracy gain for stress scenarios. Revisit if a customer's book is
dividend-heavy enough to care.

---

## Not yet built

- **Frontend.** Recommendation stands: server-side pivot with TanStack Virtual
  and custom cells, rather than an off-the-shelf grid. The UX is the entire
  product thesis and should not be outsourced, and AG Grid's enterprise pivot
  features carry licence costs a bootstrapped effort should avoid.
  [FINOS Perspective](https://github.com/finos/perspective) (Apache 2.0,
  C++/WASM streaming pivot engine) is the right benchmark and a viable fallback.
- **API layer.** FastAPI returning Arrow IPC; `aggregate.to_arrow_ipc` is the
  serialization boundary and is already in place.
- **Materialized top-level pivots**, per the 5M note above.
- **Data ingestion.** The actual hard problem. See strategy.md §3.

### Margin engine, staged

1. **Phase 1 (done).** BS greeks + user-defined shock templates with
   per-underlying sigma calibration. Usable as a post-trade stress product.
2. **Phase 2.** TIMS / RBH replication for equity options. **Gated on** OCC
   margin parameter and theoretical price file access, which may depend on
   clearing-member status. Confirm before committing.
3. **Phase 3.** SPAN, only if futures are in scope for target customers.
