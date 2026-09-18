# risk_grid — hosting

What to run this on, and why. Every number here is measured on this repo;
`python spikes/storage_spike.py` reproduces them. Test box: 4 cores, 15GB RAM —
deliberately modest, so treat these as a floor.

Target shape: 1–5M positions per firm, rebuilt intraday every 30–60 minutes,
multi-tenant SaaS, container-per-firm isolation.

---

## The finding that sets the budget

Phase 1 held the whole batch in RAM because that was the fastest thing to build,
and the architecture doc claimed that was a performance requirement. It is not.

At 2M positions × 50 scenarios:

| | |
|---|---|
| Reprice a batch from scratch | 13.0 s |
| Load it from Parquet, eager | 1.2 s — **10.7x faster** |
| Load it from Parquet, lazy | 0.02 s |
| Pivot by sector, in RAM | 163 ms |
| Pivot by sector, lazy Parquet scan, all 50 scenario columns | 291 ms |
| Pivot by sector, lazy Parquet scan, 6 scenario columns | **61 ms** |
| Pivot by sector, materialised rollup | **0.4 ms** |
| Drill one level, cold Parquet scan | 219 ms |
| Drill one level, cached | 0.1 ms |

Reading that honestly:

- **Serving from Parquet costs about 1.8x on a cold full scan** and is *faster*
  when the column template needs few scenario columns, because projection
  pushdown reads only those column chunks. Since a template typically shows four
  Sdev columns and Max Risk, the common case is the fast one.
- **Rollups make the common case disappear entirely.** The root level of every
  dimension is precomputed at build time and answers in under a millisecond.
- **RAM buys very little here** — and RAM is the resource that sizes the bill.

End-to-end over HTTP, including authentication, the control-plane lookup,
aggregation and JSON serialization, on a 200k-position batch served from Parquet:

| | |
|---|---|
| Root level (rollup-served) | 6.8 ms |
| Root, sorted by Max Risk | 6.4 ms |
| Drill into a sector | 5.9 ms |
| Filtered (bypasses the rollup) | 5.8 ms |
| Drill two levels, 300 rows | 7.4 ms |

## Artifacts and retention

A batch is written once and read many times, so it is stored three ways:

| Artifact | Size at 2M | What it is for |
|---|---|---|
| `batch.parquet` | 0.54 GB | positions + greeks + scenario matrix; what the grid scans |
| `positions.parquet` | 0.06 GB — **9.2x smaller** | the raw book; enough to reprice, so cold retention keeps only this |
| `rollups/*.parquet` | 1.93 MB total | precomputed root levels, 0.4% of the batch |
| `manifest.json` | — | what this batch is and what building it cost |

Rollups are chosen by **cardinality, not by name**: any dimension under 50,000
groups qualifies. At 2M positions that is every dimension except `contract`
(343k groups, 83 MB on its own), which is computed on demand and cached instead.
Each rollup is built with every measure, every scenario column and every detail
dimension, so a narrower column template is a column selection rather than a
different aggregation — one artifact serves them all.

Storage per firm per year, at 14 batches/day × 250 days:

| Policy | Volume | S3 Standard |
|---|---|---|
| Keep every full batch | 1.89 TB | ~$43/mo |
| Keep positions only | 0.20 TB | ~$5/mo |
| **30 days full, positions-only beyond** | **0.43 TB** | **~$10/mo** |

The tiered policy keeps the ability to reconstruct any historical batch (reprice
from `positions.parquet`) for a quarter of the cost of keeping them whole.

---

## Was Lightsail wrong?

Less wrong than expected. [Memory-optimized Lightsail bundles landed in February
2026](https://aws.amazon.com/about-aws/whats-new/2026/02/amazon-lightsail-memory-optimized-instances)
with up to 512 GB, and given the measurements above a pilot does not need
anything like that — a 16 GB instance is comfortable.

So the reasons to prefer EC2 are commercial and compliance-shaped, not capacity:

- **No spot instances.** Repricing is bursty and interruption-tolerant, which is
  exactly what spot is for — roughly 70% off that workload.
- **No Savings Plans.** Up to 72% off the steady query servers.
- **Weaker isolation and audit primitives.** CloudTrail, KMS with
  customer-managed keys, per-customer security groups and VPC endpoints. FINRA
  RN 21-29 makes every customer diligence you, and SOC 2 Type 2 is table stakes
  for that conversation (see `docs/strategy.md` §5).

**Recommendation: containerise now and split the services, so the pilot can run
wherever is cheapest and moving is a deployment change rather than a rewrite.**

---

## Topology

| Service | Shape | Why |
|---|---|---|
| **Query**, one container per firm | 2 vCPU / 4 GB | Started with `RISK_GRID_FIRM` and refuses any other firm, so position data never shares a process. Needs little RAM: the process is ~400 MB, rollups ~2 MB, and hot Parquet lives in the host page cache. |
| **Build worker**, shared pool | Compute-optimized (c8g), spot | CPU-bound with a ~3 MB working set — chunking keeps float64 temporaries in L3 — so it wants cores, not RAM. |
| **Control plane** | RDS Postgres, db.t4g.small | Firms, users, API keys, batch index, audit log. Metadata only; never a risk number. |
| **Object storage** | S3, prefix per firm | Batch artifacts, rollups, raw drops, quarantine. |

Build load is smaller than it looks: at 3M positions a reprice is roughly 20 s,
so 14 builds a day is about 5 minutes of CPU per firm per day. Thirty firms is
~2.3 CPU-hours a day, which one small always-on worker absorbs with room for the
intraday peak.

### Isolation

Two independent layers, because a missing `WHERE` clause is the most ordinary
bug there is and one cross-firm leak would end the company:

1. **Authorisation** — a principal belongs to exactly one firm (platform admins
   belong to none and must name one explicitly). Checked on every call.
2. **Containment** — the process refuses any firm other than the one it was
   started for, independent of who authenticated. Deployed, that means one
   container per firm.

Storage keys are namespaced `firms/{firm}/batches/{batch}/…`, and saved column
templates and shock configs are namespaced the same way — they are user content
and would leak just as readily as positions.

### Cost

Steady state at 3M positions and 14 batches/day:

| | 1 firm (pilot) | 30 firms |
|---|---|---|
| Query | ~$40/mo | 1–2 × r8g.2xlarge, ~$520/mo |
| Build | shared with query | ~$210/mo on-demand, ~$65 on spot |
| Postgres | ~$25/mo | ~$50/mo |
| S3, tiered retention | ~$15/mo | ~$450/mo |
| **Total** | **~$80–150/mo** | **~$1,100/mo** |

Roughly 1% of revenue against the $1.5–4M ARR `docs/strategy.md` targets.

---

## Running it

Settings come from a `.env` file (copy `.env.example`) or from the environment,
which takes precedence — so a container's orchestrator stays in charge and a
file baked into an image cannot override it.

```bash
cp .env.example .env

python -m control.bootstrap --firm acme --name "Acme Securities" --email ops@acme.test
python -m risk.build --firm acme --synthetic 200000
python -m uvicorn api.main:app --port 8000

python -m control.keys issue --email ops@acme.test   # replace a lost token
```

| Variable | Meaning |
|---|---|
| `RISK_GRID_STORE` | Directory or `s3://bucket/prefix` |
| `RISK_GRID_FIRM` | The only firm this process may serve. Unset means dev mode |
| `RISK_GRID_DATABASE_URL` | SQLAlchemy URL; SQLite locally, Postgres deployed |
| `RISK_GRID_TEMPLATES` | Root for per-firm column templates and shock configs |
| `RISK_GRID_BATCH_CACHE` | Loaded batches held per firm (default 3) |
| `RISK_GRID_SMTP_*` | Email delivery; unset means reports print to the console |
| `RISK_GRID_NO_DOTENV` | Skip `.env` entirely. Set by the test suite |

---

## Not built yet

Worth stating plainly, because the numbers above can make this look more
finished than it is:

- **`S3Store` has not been run against real S3.** It is written and the
  interface matches `LocalStore`, which is what the tests exercise. Treat the
  first deploy as the test.
- **No infrastructure-as-code.** Topology is documented, not codified. Terraform
  before the second customer, not the tenth.
- **No backups or disaster recovery.** S3 versioning and RDS snapshots are the
  obvious answer; neither is configured, and the retention policy above assumes
  a lifecycle rule that does not exist yet.
- **No autoscaling.** Fixed instances. Given the load numbers, that is fine well
  past the first handful of customers.
- **No secrets management.** API keys are hashed in Postgres, but nothing yet
  manages database credentials or S3 access beyond environment variables.
- **The 30-day-hot lifecycle transition** is a policy, not yet a lifecycle rule.
