# risk_grid — strategy

Status: first pass. Written to be argued with, not agreed with.

---

## 1. What post-trade stress actually is

The product being replaced is a pivot over the firm's post-trade book, delivered
as a Windows application over Citrix. Two main axes:

- **instrument level** — instrument type, underlying, expiry, strike
- **account level** — firm, desk, account

Within any cell of that pivot it shows greeks, security reference data (DTE,
strike, mark, multiplier), and P&L under a set of configurable shocks. The shock
templates are user-defined and driven by historical standard deviation moves, so
a "2 sigma down" scenario means a different percentage move for each underlying.

That last detail matters more than it looks. A flat ±10% grid is easy and wrong:
it treats a utility and a biotech as the same risk. Per-underlying sigma
calibration is the part of the incumbent worth keeping, and the part worth doing
better than they do.

It is **post-trade**, which means it runs on an end-of-day or intraday snapshot
rather than a tick stream. This single fact removes market data plumbing,
streaming infrastructure, and tick handling from the problem. See
[architecture.md](architecture.md) — it is the reason a small team can build
this credibly.

### Screenshots

> **TODO** — captures of the incumbent post-trade stress screen and GlobalRisk,
> to anchor the UX comparison. Annotate: click depth to reach a position, time
> to first paint, what the shock template editor looks like.

For comparison, the working prototype is in
[architecture.md](architecture.md) and `docs/images/`.

---

## 2. Competitive landscape

The market splits into three tiers, and only one of them is addressable.

### Tier 1 — builds in-house

Bulge bracket, large market makers, IBKR. They have quant and infrastructure
teams and will never buy this. Their spend on the function is real but shows up
as headcount, not vendor revenue — which is exactly why vendor revenue is a poor
proxy for the value of the function.

**Not addressable at any price.** Do not model them in any forecast.

### Tier 2 — buys enterprise

| Vendor | Note |
|---|---|
| Nasdaq / Adenza (Calypso + AxiomSL) | ~$590M revenue, ~15% organic growth; [acquired by Nasdaq for $10.5B](https://ir.nasdaq.com/news-releases/news-release-details/nasdaq-accelerates-its-transformation-leading-technology) |
| [Cboe Hanweck](https://www.cboe.com/services/analytics/hanweck/stress_tests/) | Closest direct analog: price/vol/time stress vectors, aggregation across positions, expected shortfall and tail risk. Also runs the margin engine behind OCC's STANS. |
| ION, FIS Securities Processing Suite, TS Imagine | Front-to-back platforms with risk bundled in |

This tier is large and real. The Adenza number is the single best evidence that
this category is not a niche. But it is reached through multi-year RFP cycles
against incumbents with reference customers, and a bootstrapped effort will not
win there cold.

### Tier 3 — buys SMB  ← **the entry point**

Small and mid-sized carrying/clearing broker-dealers and options-heavy
introducing BDs that lack the infrastructure to build their own.

| Vendor | Note |
|---|---|
| [KRM22](https://krm22.com/risk-manager/) | Risk Manager: real-time P&L to strike level, greeks, what-if, SOD/intraday margin across SPAN/SPAN2/TIMS/RBH, stress via stddev formulas and 20% vol shocks |
| [GlobalRisk Corporation](https://globalrisk.com/) | FirmRisk position monitoring bundled with net capital, customer reserve, books & records and FOCUS reporting |

KRM22 plc (AIM: KRM) is worth reading closely because it is public and therefore
legible: **ARR £7.9m** as of H1 2026 across all modules and geographies, FY25
revenue £7.4m at ~10% growth, net loss £2.0m. Eight years and two acquisitions
(PRIME Analytics, Object+) to get there, plus a Trading Technologies distribution
partnership.

Two readings of that, both worth holding:

1. **It is not the ceiling.** KRM22 serves the small-to-mid segment. Adenza at
   $590M shows what the category looks like further up. Do not size the market
   from KRM22's income statement — an earlier draft of this document did exactly
   that and reached a conclusion that was too pessimistic for the wrong reason.
2. **It is evidence about this tier specifically.** A focused public company
   with a distribution channel plateauing near £8m says something real about how
   hard Tier 3 is to monetize.

One detail with strategic weight: KRM22's recent ARR growth came from a
**Margin-as-a-Service API**, not from GUI seats. The incumbent is monetizing the
engine and, in effect, conceding the presentation layer.

---

## 3. Where the $200K actually goes

Not the UI. A risk screen is maybe 15% of what a $200K contract buys. The rest:

- **Clearing integrations.** Every relationship (Apex, Pershing, BAML, Wedbush,
  Broadridge BPS) is its own file format and its own project. Dozens of bespoke
  parsers is the incumbents' real moat, and it does not get more glamorous at
  scale.
- **Regulatory computation.** GlobalRisk's stickiness is net capital, customer
  reserve, and FOCUS. The FinOp signs their name to those numbers and will not
  move them to a startup.

Implication for positioning: **a better grid is a feature, not a wedge.** Nobody
rips out the system that produces their FOCUS report because a challenger's
pivot is snappier. The realistic entry is running in parallel on the same data
as an *additional* cost, which means the pitch has to be a savings story (Citrix
seats, ops time) or a capability story (shocks the incumbent cannot express,
per-underlying sigma calibration, intraday re-runs) — never an aesthetics story.

The defensible asset is the **scenario engine**, not the grid. A grid is copied
in a quarter. A stress engine people trust is not.

---

## 4. Market sizing

Size from a count of firms, not from a vendor's revenue.

FINRA's liquidity rule proposal scopes to **approximately 125 member firms**,
described as those that carry customer accounts and clear transactions. That is
close to a direct count of the target universe.

- Tier 3 within it: perhaps 60–90 firms
- Plus options prop shops and introducing BDs that still need stress
- At $40–80K: an addressable pool of roughly **$4–16M**
- Capturing 20–30% over several years: **$1.5–4M ARR**

A strong bootstrapped outcome and a poor venture one. The smallness of the pool
is a feature here — it deters funded competitors from chasing you into it.

> **TODO** — confirm the carrying-firm count against the current FINRA Industry
> Snapshot rather than relying on the rule proposal's scoping estimate.

---

## 5. Regulatory posture

**A software vendor selling to broker-dealers needs no FINRA membership and no
FINRA partner.** There is no registration regime for risk-software vendors. What
actually applies:

| Concern | Applies? |
|---|---|
| FINRA membership / licensing for the vendor | **No.** |
| [FINRA RN 21-29](https://www.finra.org/rules-guidance/notices/21-29) (vendor oversight) | Indirectly — the *customer* must diligence you. Means SOC 2 Type 2, BCP, incident response, or procurement ends the conversation. |
| Reg S-P amendments (2024) | Yes where customer info is touched — written policies, incident notification. |
| **Exchange Act Rule 17a-3(a)(23)** | Firms over specified thresholds must make and keep current records documenting credit, market and liquidity risk management controls. If this product becomes the system of record for those controls, 17a-4 retention follows. **Confirm against rule text before scoping v1.** |
| SEC 17a-4 books & records | Follows from the above, and definitively in scope if the product produces net capital or FOCUS inputs. |
| [SEC Rule 15c3-5](https://www.sec.gov/rules-regulations/staff-guidance/trading-markets-frequently-asked-questions/divisionsmarketregfaq-0) (market access) | Only if the numbers drive pre-trade controls. Vendor tools are explicitly permitted so long as the BD keeps "direct and exclusive control." Post-trade scope keeps this out of the order path entirely. |
| Reg SCI | No — exchanges, SIPs, clearing agencies, large ATSs only. |

**Scope decision:** compute stress and margin, do not produce regulatory
filings. Subject to the 17a-3(a)(23) question, this keeps the heaviest
recordkeeping obligations out of v1 while still building the engine that
constitutes the moat.

---

## 6. Scaling

The "millions of position rows" concern does not survive contact with the
measurements. Full numbers and method in [architecture.md](architecture.md);
the summary, measured on a 4-core box with 15GB RAM:

| Book size | Snapshot build | Drill into a node | Full re-pivot |
|---|---|---|---|
| 1M positions | 7 s | 31 ms | 36–292 ms |
| 5M positions | 42 s | 165 ms | 119 ms – 1.5 s |

Repeat pivots are served from a per-batch aggregate cache: 73 ms cold, 0.5 ms
warm, with re-sorting and paging under 1 ms.

Interaction is comfortably interactive at every scale tested. The snapshot build
is a batch step that runs once per snapshot, where tens of seconds is
operationally irrelevant.

The genuinely hard engineering problem is not compute. It is the **position and
mark data pipeline** — see §3.

---

## 7. Open todos

> **TODO** — user to add.

- [ ] Screenshots (§1)
- [ ] Name the incumbent explicitly, if it should be named
- [ ] Confirm carrying-firm count against FINRA Industry Snapshot (§4)
- [ ] Confirm 17a-3(a)(23) against rule text (§5)
- [ ] Decide whether futures/SPAN are in scope for target customers
- [ ] Establish whether OCC margin parameter file access is obtainable
