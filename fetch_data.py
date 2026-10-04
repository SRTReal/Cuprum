#!/usr/bin/env python3
"""
CUPRUM live-data fetcher.
Pulls copper market data from free public sources and writes data.json
for the terminal to consume. Run daily via cron or GitHub Actions.

Sources:
  - COMEX HG front-month price + 18-month history   (Yahoo Finance, 15m delayed)
  - CFTC Commitments of Traders, Copper             (CFTC public, weekly)
  - ECB daily FX reference rates                    (ECB public, daily)
  - China PMI headline                              (manual override, see PMI_OVERRIDE)
  - LME warehouse snapshot                          (manual override, see LME_OVERRIDE)

Every fetcher is wrapped in try/except: one failing source does NOT abort
the whole run. The resulting data.json always has a "generated_at" field
and, per source, either real values or an {"error": "..."} marker so the
front-end can render a "stale" badge gracefully.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yfinance as yf

OUT = Path(__file__).parent / "data.json"
UA = {"User-Agent": "cuprum-fetcher/1.0 (+https://github.com/your/repo)"}
TIMEOUT = 30

# ----- Manual overrides (easy to update; see README) -----
PMI_OVERRIDE = {
    "caixin_mfg": 50.6,          # Caixin Manufacturing PMI, latest print
    "nbs_mfg":    50.2,          # NBS Manufacturing PMI, latest print
    "report_date": "2026-10-01",
}
LME_OVERRIDE = {
    # LME stocks require a scrape of a changing HTML endpoint; for a weekly
    # project cadence, update these by hand or wire a Selenium scrape later.
    "lme_cu":   185_000,
    "shfe_cu":   42_000,
    "comex_cu":  28_000,
    "report_date": "2026-10-03",
}


# -------------- Fetchers --------------

def fetch_price() -> dict[str, Any]:
    """COMEX HG front-month: last, change, 18-month daily close history."""
    hg = yf.Ticker("HG=F")
    hist = hg.history(period="18mo", interval="1d")
    if hist.empty:
        raise RuntimeError("yfinance returned no rows for HG=F")
    closes = hist["Close"].dropna()
    last = float(closes.iloc[-1])
    prev = float(closes.iloc[-2])
    history = [
        {"d": idx.strftime("%Y-%m-%d"), "c": round(float(c), 4)}
        for idx, c in closes.items()
    ]
    return {
        "symbol": "HG=F",
        "last": round(last, 4),
        "change": round(last - prev, 4),
        "change_pct": round((last - prev) / prev * 100, 3),
        "unit": "USD/lb",
        "history": history,
    }


def fetch_cot() -> dict[str, Any]:
    """CFTC Disaggregated COT, copper futures-only. Weekly, Tuesday-as-of."""
    year = datetime.now(timezone.utc).year
    url = f"https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip"
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        with z.open(z.namelist()[0]) as f:
            df = pd.read_csv(f, low_memory=False)

    # Match any row whose market name contains "COPPER" and is listed on COMEX.
    copper = df[
        df["Market_and_Exchange_Names"].str.contains("COPPER", na=False, case=False)
        & df["Market_and_Exchange_Names"].str.contains("COMMODITY EXCHANGE", na=False, case=False)
    ].copy()
    if copper.empty:
        raise RuntimeError("no copper rows in CFTC report")

    copper = copper.sort_values("Report_Date_as_YYYY-MM-DD")
    rows = []
    for _, r in copper.iterrows():
        rows.append({
            "d":   r["Report_Date_as_YYYY-MM-DD"],
            "net": int(r["M_Money_Positions_Long_All"] - r["M_Money_Positions_Short_All"]),
            "oi":  int(r["Open_Interest_All"]),
        })
    # 2-year z-score (~104 weekly reports)
    window = rows[-104:] if len(rows) >= 104 else rows
    nets = [h["net"] for h in window]
    mu = sum(nets) / len(nets)
    var = sum((x - mu) ** 2 for x in nets) / len(nets)
    sigma = var ** 0.5 or 1.0
    latest = rows[-1]
    return {
        "report_date": latest["d"],
        "net": latest["net"],
        "oi":  latest["oi"],
        "net_pct_oi": round(latest["net"] / latest["oi"] * 100, 2),
        "z_score": round((latest["net"] - mu) / sigma, 2),
        "history": rows[-26:],
    }


def fetch_fx() -> dict[str, Any]:
    """ECB daily reference rates (free, no key, XML)."""
    import xml.etree.ElementTree as ET
    r = requests.get(
        "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
        headers=UA, timeout=TIMEOUT,
    )
    r.raise_for_status()
    root = ET.fromstring(r.text)
    ns = {"e": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
    rates = {c.get("currency"): float(c.get("rate"))
             for c in root.findall(".//e:Cube[@currency]", ns)}
    eur_usd = rates.get("USD")
    if not eur_usd:
        raise RuntimeError("ECB EURUSD missing")
    out = {"EURUSD": round(eur_usd, 4)}
    for ccy in ("CLP", "PEN", "CNY", "AUD"):
        if ccy in rates:
            out[f"USD{ccy}"] = round(rates[ccy] / eur_usd, 4)
    return out


def fetch_inventory() -> dict[str, Any]:
    """Combined warehouse stocks snapshot (manual override today).

    LME/SHFE/COMEX publish daily/weekly reports; wire a scraper in later.
    """
    d = dict(LME_OVERRIDE)
    d["total"] = d["lme_cu"] + d["shfe_cu"] + d["comex_cu"]
    return d


def fetch_pmi() -> dict[str, Any]:
    """China PMI (manual override; monthly print)."""
    return dict(PMI_OVERRIDE)


# -------------- Driver --------------

FETCHERS = [
    ("price",     fetch_price),
    ("cot",       fetch_cot),
    ("fx",        fetch_fx),
    ("inventory", fetch_inventory),
    ("pmi",       fetch_pmi),
]


def main() -> int:
    data: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {
            "price":     "Yahoo Finance (HG=F, ~15m delayed)",
            "cot":       "CFTC disaggregated futures, weekly",
            "fx":        "ECB reference rates, daily",
            "inventory": "LME / SHFE / COMEX (manual override)",
            "pmi":       "NBS / Caixin (manual override)",
        },
    }
    failures = 0
    for name, fn in FETCHERS:
        try:
            data[name] = fn()
            print(f"  ✓ {name}")
        except Exception as e:  # noqa: BLE001
            data[name] = {"error": f"{type(e).__name__}: {e}"}
            print(f"  ✗ {name}: {e}", file=sys.stderr)
            failures += 1
    OUT.write_text(json.dumps(data, indent=2, default=str))
    print(f"→ wrote {OUT}  ({OUT.stat().st_size:,} bytes, {failures} failure(s))")
    # Exit 0 even on partial failure — a daily job should still commit what it got.
    return 0


if __name__ == "__main__":
    sys.exit(main())
