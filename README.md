# CUPRUM — Live Data Layer

Path 1: a daily snapshot pipeline that turns the static CUPRUM terminal into
a live one, with zero hosting cost.

```
fetch_data.py        the fetcher (COMEX, CFTC, ECB + manual overrides)
data.json            its output — committed to the repo, read by the page
requirements.txt     pandas, yfinance, requests
.github/workflows/   GitHub Action, runs 23:00 UTC Mon-Fri, auto-commits
cuprum.html          the terminal; fetches data.json on load
```

## Local dry run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python fetch_data.py        # writes data.json
python -m http.server 8000  # open http://localhost:8000/cuprum.html
```

## Daily refresh

Push the repo to GitHub, enable Actions, done. The workflow:
1. Runs the fetcher every weekday at 23:00 UTC.
2. If `data.json` changed, commits and pushes it.
3. The terminal reads the new snapshot the next time anyone opens it.

For an Artifact-published terminal, replace step 3 with a scripted
re-publish (either locally or in the Action — add a step that pushes the
updated HTML + data.json via the Artifact API).

## What's live, what's manual

| Panel         | Source                   | Automatic? |
|---------------|--------------------------|------------|
| Price + chart | Yahoo Finance (HG=F)     | ✅         |
| COT z-score   | CFTC weekly CSV          | ✅         |
| FX (CLP/PEN)  | ECB daily XML            | ✅         |
| Inventories   | LME/SHFE/COMEX           | ⚠️ manual override, update weekly |
| China PMI     | NBS / Caixin             | ⚠️ manual override, update monthly |

The two manual sources are in `fetch_data.py` as `LME_OVERRIDE` and
`PMI_OVERRIDE` dicts — a 30-second edit when numbers change. Promote them
to automatic fetches later by:

- **LME** — scrape `https://www.lme.com/en/market-data/reports/` or
  subscribe to Fastmarkets / SMM feeds.
- **PMI** — parse a Reuters / Bloomberg headline via RSS or an LLM pass.

## What the terminal does with `data.json`

On page load, `cuprum.html` fetches `data.json`. If present, it overlays
live values on top of the baked-in defaults:

- Top-right ticker: price, change, change %
- Header badge: "Updated HH:MM UTC"
- Price panel: history replaced with real 18-month closes
- Positioning panel: COT z-score and net position
- Macro row: USDCLP, EUR/USD

If `data.json` is missing or stale (> 48h old), the badge turns amber and
the baked defaults are used — the terminal still renders.

## Promoting to Path 2 (intraday)

When you outgrow daily snapshots, swap the Action for a small FastAPI
backend on Fly.io / Railway. The browser contract (`fetch("/api/snapshot")`
returns the same JSON shape) stays identical, so only the host changes.
