"""Can OpenFIGI resolve the identifiers people send us?

Pricing requests arrive as spreadsheets of CUSIPs, ISINs and tickers. Massive
prices by ticker only, and its `?cusip=` lookup is one call per CUSIP and never
echoes the CUSIP back. OpenFIGI maps up to 100 identifiers per request and
returns a composite FIGI, which Massive also carries -- an exact join key.

This measures the hit rate before anything gets built on it:

  1. classify    -- CUSIP / ISIN / ticker by shape, check digits validated
  2. resolve     -- batched OpenFIGI /v3/mapping
  3. price       -- previous close from Massive grouped daily: one call per run
  4. report      -- the US composite listing per identifier, and what is left

Stdlib only, so it runs anywhere. Set OPENFIGI_API_KEY for 100-id batches and
the higher rate limit; without it, batches are 10 and requests 25/minute. Set
MASSIVE_API_KEY to add prices; without it, the spike only resolves.

Run:  python spikes/figi_spike.py [ID ...] [--file ids.csv]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

MAPPING_URL = "https://api.openfigi.com/v3/mapping"
GROUPED_URL = "https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/{day}"

# AAPL, MSFT, BRK.B, SPY by CUSIP; AAPL and HSBC by ISIN; a Treasury; a ticker;
# and AAPL's CUSIP with a bad check digit.
SAMPLE = [
    "037833100",
    "594918104",
    "084670702",
    "78462F103",
    "US0378331005",
    "GB0005405286",
    "91282CJL6",
    "NVDA",
    "037833101",
]


def _char_value(c: str) -> int:
    if c.isdigit():
        return int(c)
    if c.isalpha():
        return ord(c.upper()) - ord("A") + 10
    return {"*": 36, "@": 37, "#": 38}[c]


def cusip_valid(s: str) -> bool:
    total = 0
    for i, c in enumerate(s[:8]):
        v = _char_value(c)
        if i % 2:
            v *= 2
        total += v // 10 + v % 10
    return (10 - total % 10) % 10 == int(s[8])


def isin_valid(s: str) -> bool:
    # Letters expand to two digits, then Luhn over the whole string.
    digits = "".join(str(_char_value(c)) for c in s[:11]) + s[11]
    total = 0
    for i, d in enumerate(reversed(digits)):
        v = int(d)
        if i % 2:
            v *= 2
        total += v // 10 + v % 10
    return total % 10 == 0


CUSIP_RE = re.compile(r"^[0-9A-Z*@#]{8}[0-9]$")
ISIN_RE = re.compile(r"^[A-Z]{2}[0-9A-Z]{9}[0-9]$")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-/]{0,9}$")


def classify(raw: str) -> tuple[str, str | None]:
    """Return (normalised id, OpenFIGI idType), or idType None if unusable."""
    s = raw.strip().upper()
    if ISIN_RE.match(s):
        return s, "ID_ISIN" if isin_valid(s) else None
    if CUSIP_RE.match(s):
        return s, "ID_CUSIP" if cusip_valid(s) else None
    if TICKER_RE.match(s):
        return s, "TICKER"
    return s, None


def job(ident: str, id_type: str) -> dict:
    j = {"idType": id_type, "idValue": ident}
    if id_type == "TICKER":
        # Bare tickers are ambiguous worldwide; we only price US listings.
        j["exchCode"] = "US"
    return j


def post(jobs: list[dict], key: str | None) -> list[dict]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-OPENFIGI-APIKEY"] = key
    req = urllib.request.Request(
        MAPPING_URL, data=json.dumps(jobs).encode(), headers=headers, method="POST"
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 3:
                raise
            wait = int(e.headers.get("ratelimit-reset") or 2**attempt * 3)
            print(f"  rate limited, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


def pick(rows: list[dict]) -> tuple[dict | None, str]:
    """The US composite listing, or the reason there isn't exactly one."""
    us = [r for r in rows if r.get("exchCode") == "US"]
    if len(us) == 1:
        return us[0], "ok"
    if len(us) > 1:
        return us[0], f"ambiguous ({len(us)} US rows)"
    # Bonds and other OTC instruments carry no exchange code at all.
    if len(rows) == 1:
        return rows[0], "ok (no exchange)"
    return rows[0], f"no US listing ({len(rows)} rows)"


def massive_ticker(row: dict) -> str | None:
    # Massive prices equities only; a bond's FIGI "ticker" is a description.
    if row.get("marketSector") != "Equity" or not row.get("ticker"):
        return None
    # OpenFIGI writes share classes as BRK/B; Massive as BRK.B.
    return row["ticker"].replace("/", ".")


def _eastern_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Windows without the tzdata package; an hour off in summer is fine
        # for a cutoff this far from the close.
        return datetime.now(timezone(timedelta(hours=-5)))


def previous_closes(key: str) -> tuple[date, dict[str, float]]:
    """Every US close for the latest completed session, in one call.

    Today counts only once the evening session is over, so a run during market
    hours never picks up a partial bar. Weekends and holidays come back empty
    and are walked past.
    """
    now = _eastern_now()
    day = now.date() if now.hour >= 20 else now.date() - timedelta(days=1)
    for _ in range(10):
        # Unadjusted: the price the security actually closed at.
        url = GROUPED_URL.format(day=day.isoformat()) + "?adjusted=false&include_otc=true"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.load(resp)
        if body.get("resultsCount"):
            return day, {r["T"]: r["c"] for r in body["results"]}
        day -= timedelta(days=1)
    raise RuntimeError(f"no grouped daily results in the 10 days before {now.date()}")


def read_ids(path: str) -> list[str]:
    with open(path, newline="") as f:
        return [row[0] for row in csv.reader(f) if row and row[0].strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ids", nargs="*", help="identifiers (default: a built-in sample)")
    ap.add_argument("--file", help="CSV or text file; first column is the identifier")
    args = ap.parse_args()

    raw = (read_ids(args.file) if args.file else []) + args.ids or SAMPLE
    key = os.environ.get("OPENFIGI_API_KEY")
    batch = 100 if key else 10

    classified = [classify(r) for r in raw]
    valid = [(i, t) for i, t in classified if t]
    results: dict[tuple[str, str], dict] = {}

    t0 = time.perf_counter()
    for start in range(0, len(valid), batch):
        chunk = valid[start : start + batch]
        for (ident, id_type), res in zip(chunk, post([job(*v) for v in chunk], key)):
            results[(ident, id_type)] = res
        if not key and start + batch < len(valid):
            time.sleep(2.5)  # 25 requests/minute unauthenticated
    elapsed = time.perf_counter() - t0

    massive_key = os.environ.get("MASSIVE_API_KEY")
    as_of, closes = previous_closes(massive_key) if massive_key else (None, {})

    cols = ["input", "type", "status", "ticker", "massive", "close", "compositeFIGI", "sector", "secType", "name"]
    out = []
    for ident, id_type in classified:
        if not id_type:
            out.append([ident, "-", "invalid / bad check digit", "", "", "", "", "", "", ""])
            continue
        res = results[(ident, id_type)]
        if "data" not in res:
            out.append([ident, id_type, res.get("warning") or res.get("error", "?"), "", "", "", "", "", "", ""])
            continue
        row, status = pick(res["data"])
        mt = massive_ticker(row)
        if not massive_key:
            close = ""
        elif mt is None:
            close = "-"
        elif mt in closes:
            close = f"{closes[mt]:g}"
        else:
            close = "no close"
        out.append([
            ident,
            id_type,
            status,
            row.get("ticker") or "",
            mt or "not priceable",
            close,
            row.get("compositeFIGI") or "",
            row.get("marketSector") or "",
            row.get("securityType") or "",
            (row.get("name") or "")[:30],
        ])

    widths = [max(len(c), *(len(r[i]) for r in out)) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for r in out:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))

    resolved = sum(1 for r in out if r[2].startswith("ok"))
    print(
        f"\n{resolved}/{len(out)} resolved cleanly, "
        f"{len(classified) - len(valid)} rejected before sending, "
        f"{len(valid)} ids in {elapsed:.1f}s ({'keyed' if key else 'no key'})"
    )
    if massive_key:
        priced = sum(1 for r in out if r[5] not in ("", "-", "no close"))
        print(f"{priced}/{len(out)} priced at the {as_of} close ({len(closes)} tickers in that session)")


if __name__ == "__main__":
    main()
