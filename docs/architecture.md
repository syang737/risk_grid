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
positions (N)                    scenarios (S)
     │                                 │
     └──────────► reprice ◄────────────┘
                     │
                (N × S) float32 P&L matrix        ← built once per batch
                     │
              groupby-sum on any dimension        ← every user interaction
                     │
            expanded tree nodes only (~25 rows)
                     │
                    JSON over the wire (~13 KB)
```

Two consequences worth stating plainly:

- **The browser never receives positions.** It receives the children of whatever
  node was expanded — typically tens of rows. The "millions of rows in a browser"
  problem is avoided rather than solved.
- **Cost scales with the data under the expanded node**, not with book size.

Post-trade scope makes this work: the book is a snapshot, so the matrix is
rebuilt per batch rather than maintained against a tick stream.

---

## One pivot model

The incumbent exposes four fixed groupings as toolbar tabs (Master Accts,
Accounts, Instruments, Contracts). All four are the same operation with
different dimension orders, so `PivotRequest` carries the order instead:

```
dimensions  ("sector", "underlying", "contract")   ordered row grouping
path        (("sector", "Technology"),)            expanded ancestors, as filters
filters     (Filter("worst", "lessThan", -1e6),)   user column filters
measures    from the active column template
detail_dimensions                                  rendered as value-or-count cells
sort / offset / limit                              presentation only
```

Children of a node are `group_by(dimensions[len(path)])` filtered by `path +
filters`. Any order works, including interleaving account and instrument
dimensions, and the leaf level returns raw positions.

### Distinct-value cells

A dimension that is not the grouping key aggregates both `n_unique` and `first`.
If the group holds exactly one value the cell shows it; otherwise the cell is
null and carries a count, which the client renders as a clickable chip
(`2,528 accounts`). Clicking it inserts that dimension at the row's depth and
re-expands — which is how cross-dimension drill is invoked.

This is the fix for the incumbent's `[21]` cells, and the two problems turn out
to be one: the useless number was sitting exactly where the missing affordance
belonged. The behaviour is visible in one screen — a sector row reads
"25 desks", while each account row beneath it shows its actual desk name,
because within one account there is only one.

### Filters split by what they refer to

Not every filter can run in the same place, and getting this wrong is silent:

- A filter on a **dimension or raw attribute** (sector, `iv`, `dte`) selects
  positions and runs **before** the groupby.
- A filter on a **measure, a scenario, or Max Risk** refers to the aggregate the
  user is looking at and runs **after**. Applying `delta > 1000` per position
  would drop rows and change every remaining group's sum into something nobody
  asked for.

### Worst-of-sum, everywhere

Max Risk at any level is the minimum across the *summed* scenario columns, never
the sum of each child's own worst. Summing worsts assumes every position bottoms
out in the same scenario simultaneously, which overstates risk badly. The
totals row makes this visible: in a 200k-position book the portfolio's worst
single scenario is meaningfully better than adding up each sector's worst,
because sectors bottom out in different scenarios.

---

## Measured results

50 scenarios (10 sigma-moves × 5 vol shifts), ~85% options, exact American
repricing.

| positions | matrix build | pivot by desk | pivot by underlying | desk × account | expiry × strike | with detail cells | drill into node | wire | matrix | peak RSS |
|---|---|---|---|---|---|---|---|---|---|---|
| 100,000 | 1.1 s | 9 ms | 19 ms | 70 ms | 54 ms | 15 ms | 5 ms | 2 ms | 0.02 GB | 0.5 GB |
| 1,000,000 | 7.0 s | 36 ms | 175 ms | 242 ms | 292 ms | 164 ms | 31 ms | 1 ms | 0.19 GB | 1.6 GB |
| 5,000,000 | 41.7 s | 119 ms | 1,390 ms | 1,472 ms | 1,479 ms | 1,191 ms | 165 ms | 1 ms | 0.93 GB | 5.8 GB |

**Method, because it changes how to read the table.** Pivot figures are the best
of three after a warm-up, which is the steady-state interaction cost. Matrix
build is a single cold sample per scale, so it is the noisy column: the spike
reported 14.3 s at 1M on a box that was also running the dev stack, while three
consecutive runs on a quiet box gave 10.1 / 7.0 / 6.9 s. The 7.0 s above is the
steady-state figure; treat the 5M number as similarly generous.

### Reading these honestly

**The interactive claim holds.** Drilling into a node — by far the most common
interaction — is 5–165 ms across the whole range. Full-book re-pivots are
36–292 ms at 1M.

**At 5M, full-book re-pivots reach 1.5 s**, past the interactive threshold. The
aggregate cache below covers the repeat case, and `Batch.warm()` covers the
first screen; what remains uncovered is the first time a user picks an unusual
dimension order on a 5M book.

**The snapshot build is a batch step**, run once per batch, where tens of
seconds is operationally irrelevant. An earlier plan set a "few seconds" bar for
it; that bar was wrong, not the result.

**Distinct-value cells cost about 15–20 ms per dimension per million rows** —
164 ms at 1M for four of them, against 36 ms for the same pivot without. Cheap
enough to ship, not cheap enough to compute for dimensions nobody is displaying,
so only requested ones are aggregated.

**Memory is a non-issue.** 5.8 GB peak for a 5M-position book on a 15 GB box.

### What made it fast

1. **Chunk size, for cache residency — 3–4x.** At 50 scenarios an 8,000-row
   chunk keeps float64 temporaries near 3 MB, which fits L3. Chunks of 250,000
   measured 3–4x slower at identical thread counts. This dwarfed threading.
2. **Branch-free pricing.** Masking expired rows forces fancy-index copies of
   every input; clipping and patching afterwards is faster at book scale. Put
   prices come from put-call parity rather than two more normal CDF calls.
3. **Threading** adds roughly 2x on 4 cores. The work is embarrassingly parallel
   and numpy/scipy release the GIL, so threads suffice — no pickling.

---

## Batches and the aggregate cache

A batch is a named, timestamped snapshot (`US WBL IntraDay 2026-08-13 14:16:42`),
modelled explicitly because post-trade stress is snapshot-shaped and because
retrofitting batch-vs-batch comparison later would be painful.

Two caches, for different reasons:

- **`BatchStore`** holds loaded batches across requests, LRU by count. A grid
  session fires dozens of pivots at one batch and a 5M-row batch is several GB.
- **`Batch._aggregates`** holds computed aggregates keyed on request identity,
  **excluding sort and paging** — those are applied to the cached frame. Capped
  by estimated bytes rather than entry count, because a contract-level aggregate
  is a million rows while a desk-level one is 25.

Measured at 500k positions: a root pivot costs 73 ms cold, **0.5 ms warm**;
re-sorting it 0.9 ms; paging it 0.4 ms.

### The cache key is also correctness

Cache keys include only dimensions up to the current depth, since deeper ones
cannot affect this level's rows. That makes the totals request — which truncates
to the root dimension — hash identical to the root display request, so both read
one aggregate.

That sharing is not merely an optimisation. Float summation is not associative,
so recomputing the same aggregate by a different route can land a group on the
other side of a post-aggregation filter. Before this, an entire sector (736
positions) appeared in the totals and not in the rows above them.

---

## A rejected shortcut, and why it matters

The obvious optimisation is to reprice European in each scenario and carry the
**early exercise premium** through unchanged from the base snapshot. It is
roughly 2.7x faster.

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
is exactly the quantity this model holds fixed.
`test_american_put_premium_grows_with_moneyness` pins that behaviour.

Kept as `model="eep"` for previews, clearly marked. **Default is exact
Bjerksund-Stensland.** The lesson generalises: validate an approximation against
the number the screen displays, not against a portfolio aggregate where errors
cancel.

### Why Bjerksund-Stensland 1993 and not 2002

2002 is more accurate but needs a **bivariate** normal CDF, which has no fast
vectorized implementation in numpy/scipy. At book scale that cost outweighs the
accuracy gain for stress scenarios. Revisit if a customer's book is
dividend-heavy enough to care.

---

## API

FastAPI, speaking AG Grid's server-side row model shape so the browser
datasource needs no translation layer:

| AG Grid field | Maps to |
|---|---|
| `rowGroupCols` / `dimensions` | `PivotRequest.dimensions` |
| `groupKeys` | `path` (cast back to column dtype — strike is numeric) |
| `filterModel` | `filters`, split pre/post as above |
| `sortModel` | `sort` |
| `startRow` / `endRow` | `offset` / `limit` |

OR-combined filters are refused with a 400 rather than silently narrowing a risk
view. Non-finite floats are nulled before serialization — an expired deep-OTM
option can produce NaN, which is not valid JSON and would throw in the browser
on an otherwise fine payload.

**Wire format.** An earlier version of this document specified Arrow IPC. That
was right for bulk payloads and wrong for the row model: blocks are ~100 rows,
where JSON is a few tens of KB and needs no Arrow dependency or conversion step
in the browser. `aggregate.to_arrow_ipc` remains for full-book export.

---

## Frontend

React + TypeScript + Vite, AG Grid Enterprise on the server-side row model.

The phase-1 plan argued for a custom grid on TanStack Virtual, on the grounds
that the UX is the product thesis. Seeing the requirements — tree drill, column
move/sort/filter, pinned totals, virtualization — changed that: it is precisely
what AG Grid Enterprise's server-side row model does, at $999/developer
perpetual. Building it is 2–3 months on the *generic* part of the UI. The
differentiation is the shock template editor, sigma calibration, and not being
Citrix, none of which is the grid widget.

| File | What |
|---|---|
| `grid/RiskGrid.tsx` | Grid, tree drill, pinned totals, chip-drill re-expansion |
| `grid/datasource.ts` | Row model datasource; stable row ids are the node's full route |
| `grid/columns.ts` | Column defs from the resolved template; set filters for low-cardinality dimensions, text filters above that |
| `grid/cells.tsx` | Chip renderer, signed-number colouring |
| `panels/DimensionPicker.tsx` | Drill order and which dimensions show as chip columns |
| `panels/ShockConfigPanel.tsx` | Live shock grid editor, applies by repricing the batch |

Chip drill changes the dimension order, which reloads the grid — so the clicked
row's route is saved and re-expanded once its level arrives, or the user loses
their place on every drill.

### The drill order has one owner

The drill order can be set three ways — the side panel, a chip drill, and
dragging in AG Grid's own row-group bar — and all three write the same React
state, because that state is what the datasource sends to `/api/grid/rows`.

That was not true at first. Only the dimensions in the drill order and the
dimensions shown as columns had column definitions at all, and none of them set
`enableRowGroup`, so AG Grid refused every drop: a chip could be dragged out of
the row-group bar and never back in. Worse, dragging one out regrouped the grid
locally while the request kept the old order, so the grid and the server
disagreed about what a row meant. Now every dimension has a column definition,
`onColumnRowGroupChanged` writes the panel's order back into state, and
`onColumnVisible` does the same for the columns shown as chips — otherwise
un-hiding one in the columns tool panel gives a column of blanks, since the
server is told explicitly which detail dimensions to compute. A dimension
dragged out of the drill order reappears as one of those columns, which is
AG Grid's own behaviour and the reading that loses nothing: the level is gone
but its values are still on screen.

One rule is enforced rather than mirrored: the last dimension cannot be dragged
out, because no grouping at all means a flat list of every position in the book.

Adding a detail column or switching the column template changes the *request*
without changing the row model, so AG Grid had no reason to reload and the new
columns sat empty until something else happened to trigger a fetch. Both now
refresh the server-side store explicitly, without purging it, so expanded nodes
keep their place.

`spikes/grid_interaction.py` drives all of this in a browser and asserts on the
requests rather than the DOM — regrouping the grid locally while the request
keeps the old order is exactly the failure a screenshot would not catch.

### The demo book is made of real instruments

`risk/synthetic.py` draws from `risk/universe.py`, a hard-coded table of about
730 US tickers with their sector and industry. Classification is a lookup, not
a draw — the generator used to assign a sector at random, so the same symbol
could be a bank on one seed and a utility on the next. Strikes snap to the
listed ladder and expiries to real Fridays, and the contract identifier is
built the same way `ingest.mapping.finalise` builds one for a firm's file, so a
synthetic book and an ingested book agree.

Prices in that table are indicative levels for demo and load-test data, not
market data, and the module says so. Volatility is generated from a per-sector
base rather than tabulated, because a per-symbol vol number would be a much
stronger claim with nothing behind it.

---

## Not yet built

- **Real data ingestion.** The actual hard problem: every clearing relationship
  (Apex, Pershing, BAML, Wedbush, Broadridge BPS) is a bespoke file format. See
  strategy.md §3.
- **Batch comparison.** The batch object exists to make this possible; nothing
  reads two at once yet.
- **Saving column templates from the UI.** The API supports it; only shock
  configs have an editor.
- **By-strike vol shocks.** `scenarios.py` shifts the surface in parallel only.
  A config requesting it is rejected rather than silently ignored.

### Margin engine, staged

1. **Phase 1 (done).** BS greeks + user-defined shock templates with
   per-underlying sigma calibration.
2. **Phase 2.** TIMS / RBH replication for equity options. **Gated on** OCC
   margin parameter and theoretical price file access, which may depend on
   clearing-member status. Confirm before committing.
3. **Phase 3.** SPAN, only if futures are in scope for target customers.
