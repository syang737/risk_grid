"""Can Massive alone resolve and price what people send us?

The OpenFIGI spike resolves identifiers elsewhere; this one asks how far Massive
gets on its own, as the fallback and as the price source:

  1. classify    -- CUSIP / ISIN / ticker by shape, check digits validated;
                    US and Canadian ISINs carry the CUSIP in characters 3-11
  2. resolve     -- one /v3/reference/tickers?cusip= call per CUSIP, retried
                    with active=false so delisted securities say so
  3. price       -- previous close from grouped daily: one call per run
  4. report      -- per identifier, and the calls it took

Massive never echoes the CUSIP back, so step 2 cannot be batched or bulk-joined.

Stdlib only, so it runs anywhere. Needs MASSIVE_API_KEY.

Run:  python spikes/massive_spike.py [ID ...] [--file ids.csv]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from figi_spike import SAMPLE, classify, read_ids

BASE = "https://api.massive.com"


class Client:
    def __init__(self, key: str) -> None:
        self.key = key
        self.calls = 0

    def get(self, path: str, **params) -> dict:
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        # Header auth, so the key never lands in a URL or a log line.
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.key}"})
        for attempt in range(4):
            self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 3:
                    raise
                wait = 2**attempt * 3
                print(f"  rate limited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
        raise AssertionError("unreachable")


def by_cusip(client: Client, cusip: str) -> tuple[str | None, str, str]:
    """(ticker, status, name) for one CUSIP."""
    for active in ("true", "false"):
        rows = client.get("/v3/reference/tickers", cusip=cusip, active=active).get("results") or []
        if not rows:
            continue
        row = rows[0]
        if active == "false":
            status = f"delisted {(row.get('delisted_utc') or '')[:10]}".strip()
        elif len(rows) > 1:
            status = f"ambiguous ({len(rows)} rows)"
        else:
            status = "ok"
        return row["ticker"], status, row.get("name") or ""
    return None, "not found", ""


def _eastern_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Windows without the tzdata package; an hour off in summer is fine
        # for a cutoff this far from the close.
        return datetime.now(timezone(timedelta(hours=-5)))


def previous_closes(client: Client) -> tuple[date, dict[str, float]]:
    """Every US close for the latest completed session, in one call.

    Today counts only once the evening session is over, so a run during market
    hours never picks up a partial bar. Weekends and holidays come back empty
    and are walked past.
    """
    now = _eastern_now()
    day = now.date() if now.hour >= 20 else now.date() - timedelta(days=1)
    for _ in range(10):
        # Unadjusted: the price the security actually closed at.
        body = client.get(
            f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}",
            adjusted="false",
            include_otc="true",
        )
        if body.get("resultsCount"):
            return day, {r["T"]: r["c"] for r in body["results"]}
        day -= timedelta(days=1)
    raise RuntimeError(f"no grouped daily results in the 10 days before {now.date()}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ids", nargs="*", help="identifiers (default: a built-in sample)")
    ap.add_argument("--file", help="CSV or text file; first column is the identifier")
    args = ap.parse_args()

    key = os.environ.get("MASSIVE_API_KEY")
    if not key:
        sys.exit("set MASSIVE_API_KEY")
    client = Client(key)
    raw = (read_ids(args.file) if args.file else []) + args.ids or SAMPLE

    t0 = time.perf_counter()
    resolved = []  # input, type, ticker, status, name
    for ident, id_type in (classify(r) for r in raw):
        if id_type is None:
            resolved.append((ident, "-", None, "invalid / bad check digit", ""))
        elif id_type == "TICKER":
            # Massive writes share classes as BRK.B.
            resolved.append((ident, id_type, ident.replace("/", "."), "as given", ""))
        elif id_type == "ID_ISIN" and ident[:2] not in ("US", "CA"):
            resolved.append((ident, id_type, None, "non-US ISIN, no CUSIP", ""))
        else:
            cusip = ident[2:11] if id_type == "ID_ISIN" else ident
            resolved.append((ident, id_type, *by_cusip(client, cusip)))
    resolve_s, resolve_calls = time.perf_counter() - t0, client.calls

    as_of, closes = previous_closes(client)
    price_calls = client.calls - resolve_calls

    cols = ["input", "type", "status", "ticker", "close", "name"]
    out = []
    for ident, id_type, ticker, status, name in resolved:
        if ticker is None:
            close = ""
        elif ticker in closes:
            close = f"{closes[ticker]:g}"
        else:
            close = "no close"
        out.append([ident, id_type, status, ticker or "", close, name[:30]])

    widths = [max(len(c), *(len(r[i]) for r in out)) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for r in out:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))

    priced = sum(1 for r in out if r[4] not in ("", "no close"))
    print(
        f"\nresolve: {resolve_calls} calls in {resolve_s:.1f}s\n"
        f"price:   {price_calls} calls, {len(closes)} tickers in the {as_of} session\n"
        f"{priced}/{len(out)} priced"
    )


if __name__ == "__main__":
    main()
