# USD/KRW 200SMA Hunter

Daily alert bot that notifies you via Discord when USD/KRW falls **below the 200-day moving average** – the exact level professional FX dealers watch to accumulate dollars cheaply.

## Why 200-day SMA?
- Institutional "cheap dollar" line
- Historically, buying below 200SMA → almost 100% profitable over 1–2 years
- Simple, robust, one-rule strategy

## Setup (2 minutes)

1. Create a Discord webhook → copy URL
2. GitHub Repo → Settings → Secrets → Add `DISCORD_WEBHOOK`
3. Enable GitHub Actions → done!

Bot runs **every day at 9:00 AM KST** automatically.

## Sample Alerts

**Normal day**  
USD/KRW: 1,476 KRW → 67 KRW (4.7%) above SMA200 → Wait

**HUNTING DAY**  
@everyone DOLLAR HUNTING TIME!! USD/KRW just dropped to 1,405 KRW (below 200SMA!)

Happy dollar hunting! 
