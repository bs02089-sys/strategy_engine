# Sigma DCA Manager

Automated LOC (Limit-on-Close) target price calculator and Discord briefing bot for a systematic, volatility-based DCA (Dollar-Cost Averaging) strategy on a fixed basket of U.S. tech/semiconductor tickers.

The script runs on a schedule (e.g. via GitHub Actions), recalculates volatility (“sigma”) per ticker, evaluates simple macro/technical buy-sell signals, computes a daily LOC target price for each position, and sends a formatted daily briefing to Discord.

---

## Features

- **Automatic volatility (sigma) refresh** — Recomputes each ticker's daily sigma using either an EWMA (RiskMetrics-style) or simple historical standard deviation model, refreshing automatically when data goes stale (>90 days) or when the volatility method/parameters change.
- **LOC target price calculation** — Converts sigma into a daily limit-on-close buy target: `target = prev_close × (1 − sigma × entry_multiplier)`.
- **Market risk discount** — Applies an additional discount to the LOC price when an external `signal_report.json` bear-market risk score is elevated (≥6 or ≥10 out of 14).
- **Macro/technical signal engine** — Flags buy/sell zones per ticker using VIX level and 20/60-day moving averages, with different rules for rotation/cyclical positions vs. long-term core positions.
- **Rotation position auto-reset** — Tickers configured as `ROTATION_3M` are automatically checked for maturity (business days elapsed) and reset with a freshly recalculated sigma when their holding period expires.
- **Discord briefing** — Sends a daily embed with price, signal status, LOC target price, and rotation/maturity notices; also sends a lightweight monthly "system alive" ping on the 1st of each month.
- **Resilient price lookup** — Multi-attempt `yfinance` lookup with an `info` fallback in case historical data is temporarily unavailable.
- **Atomic config writes** — Portfolio state is persisted to `portfolio_config.json` using a temp-file + move pattern to avoid partial writes.

---

## How it works

1. Load `portfolio_config.json` (per-ticker settings + last known sigma).
2. Reset any `ROTATION_3M` position that has reached its maturity date, recalculating sigma and restarting its cycle.
3. Refresh sigma for any ticker whose last update is stale or whose volatility settings changed.
4. Save the updated config back to disk.
5. For each position: fetch the previous close, evaluate buy/sell signals (VIX + moving averages), and compute the LOC target price (with risk-based discount if applicable).
6. Assemble and send a Discord embed with the full briefing.
7. If it's the 1st of the month, send a separate monthly heartbeat ping.

---

## Configuration — `portfolio_config.json`

| Field | Description |
|---|---|
| `DISCORD_WEBHOOK` | Discord webhook URL used to send briefings. Leave blank locally; set via secret/env var in CI. |
| `DISCORD_USER_ID` | Discord user ID to `@mention` in the briefing (optional). |
| `POSITIONS.<TICKER>.LOOKBACK_DAYS` | Number of trading days used to compute volatility. |
| `POSITIONS.<TICKER>.ENTRY_MULTIPLIER` | Multiplier applied to sigma to derive the target discount from the previous close. |
| `POSITIONS.<TICKER>.VOL_METHOD` | `EWMA` or `STD` / `HISTORICAL` / `SIMPLE`. |
| `POSITIONS.<TICKER>.EWMA_LAMBDA` | Decay factor for the EWMA volatility model (used only when `VOL_METHOD = EWMA`). |
| `POSITIONS.<TICKER>.DAILY_SIGMA` | Cached daily volatility estimate (auto-updated by the script). |
| `POSITIONS.<TICKER>.LAST_SIGMA_UPDATE` | Date the sigma was last recalculated. |
| `POSITIONS.<TICKER>.START_DATE` | Cycle/position start date, used for rotation maturity and `D+n` display. |
| `POSITIONS.<TICKER>.INVEST_TYPE` | `LONG_YEAR`, `ROTATION_3M`, or `END_DEC` — determines signal logic and rotation behavior. |
| `STRATEGY.*` | Global strategy parameters (cycle length, buy/hold windows, ordering policy). |
| `LAST_MONTHLY_PING` | Tracks the last month a heartbeat ping was sent, to avoid duplicates. |

> **Note:** Never commit real Discord webhook URLs or tokens. Keep `portfolio_config.json` fields blank in the repo and inject secrets at runtime (see below).

---

## Requirements

```
pip install yfinance pandas requests numpy pytz pandas_market_calendars
```

`pandas_market_calendars` provides NYSE holiday-aware business-day counting for rotation maturity checks (`business_days_elapsed`). It's technically optional — the script falls back to a plain business-day count (weekends only, no holiday adjustment) if it isn't installed — but it's included above so behavior matches production (GitHub Actions).

> Note: the `holidays` package is **not** used anywhere in `sigma_DCA_manager.py`; only `pandas_market_calendars` is imported for holiday data. Install `pandas_market_calendars`, not `holidays`.

Python 3.10+ is required (uses `X | None` union type hints).

---

## Local setup (Windows / VS Code)

```powershell
uv venv strategy_engine
.\strategy_engine\Scripts\activate
uv pip install yfinance pandas requests numpy pytz pandas_market_calendars

python sigma_DCA_manager.py
```

> On Windows, use `python`, not `python3`.

Set secrets locally via a `.env` file or environment variables — the script reads `DISCORD_WEBHOOK` / `DISCORD_USER_ID` from the environment first, falling back to the values in `portfolio_config.json`.

---

## Running via GitHub Actions

The workflow lives at `.github/workflows/sigma_dca_manager.yml`:

```yaml
name: Sigma DCA Manager Engine

on:
  schedule:
    - cron: "15 23 * * 1-5"
  workflow_dispatch:

jobs:
  run-dca-manager:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    env:
      DISCORD_WEBHOOK: ${{ secrets.DISCORD_WEBHOOK }}
      DISCORD_USER_ID: ${{ secrets.DISCORD_USER_ID }}

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install yfinance pandas requests numpy pytz pandas_market_calendars

      - name: Run DCA Manager
        run: python sigma_DCA_manager.py > sigma_log.txt 2>&1

      - name: Sync and Notify
        if: always()
        run: |
          git config --global user.name "DCA Bot"
          git config --global user.email "bot@example.com"

          [ -f "sigma_log.txt" ] && git add sigma_log.txt
          [ -f "portfolio_config.json" ] && git add portfolio_config.json
          [ -f "sigma_history.csv" ] && git add sigma_history.csv

          git commit -m "update: dca-log $(date +'%Y-%m-%d')" || echo "No changes to commit"
          git push
```

**Setup steps:**

1. Add `DISCORD_WEBHOOK` and `DISCORD_USER_ID` as **repository Secrets** (Settings → Secrets and variables → Actions).
2. Make sure the workflow has `permissions: contents: write` (already set above) so it can push the updated config/log/history files back to the repo.
3. The schedule (`cron: "15 23 * * 1-5"`, UTC, Mon–Fri) runs after the U.S. market close. Adjust the cron expression if you want a different time.
4. The `Sync and Notify` step runs `if: always()`, so run output/logs get committed even if the script errors out — useful for debugging failed runs directly from `sigma_log.txt` in the repo history.
5. The dependency list installs `pandas_market_calendars` (not `holidays`), so NYSE holiday-aware business-day counting in `business_days_elapsed()` actually works in CI — an earlier version of this workflow installed the unused `holidays` package instead, which silently left rotation maturity checks running without holiday adjustment.

**Files updated/committed by the workflow each run:**

| File | Purpose |
|---|---|
| `sigma_log.txt` | Full stdout/stderr capture of the run (overwritten each time). |
| `portfolio_config.json` | Updated sigma values, rotation `START_DATE`, monthly ping tracker. |
| `sigma_history.csv` | Append-only log of every sigma recalculation. |

> ⚠️ **Common pitfall:** Local edits to `portfolio_config.json` or the script must be committed and pushed before the Action runs — otherwise the workflow executes against a stale version of the file.

---

## Output files

- `portfolio_config.json` — updated in place after each run (sigma values, rotation start dates, monthly ping tracker).
- `sigma_history.csv` — append-only log of every sigma recalculation (`Date, Ticker, Sigma`).
- `sigma_log.txt` — full run log (stdout/stderr), regenerated by the GitHub Actions workflow on every run.
- `signal_report.json` *(optional, external)* — if present, its `total_score` field (0–14) is used to apply a risk-based discount to the LOC price.

---

## Disclaimer

This project is a personal, self-directed trading automation tool. It is **not financial advice**. All signals, sigma calculations, and LOC targets are mechanical outputs of the configured rules and should be reviewed before placing any real order. Use at your own risk.
