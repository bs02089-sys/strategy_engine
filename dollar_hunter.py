# dollar_hunter.py
# USD/KRW 200-day SMA Hunter – Discord Alert Bot

import os
import yfinance as yf
import requests
from datetime import datetime
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# Get Discord webhook from .env
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
if not DISCORD_WEBHOOK:
    raise ValueError("DISCORD_WEBHOOK not found in .env file!")

def send_to_discord(message: str):
    payload = {"content": message}
    try:
        requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    except Exception as e:
        print(f"Failed to send to Discord: {e}")

# Fetch USD/KRW data
try:
    df = yf.Ticker("USDKRW=X").history(period="2y", interval="1d")["Close"].to_frame()
    df.rename(columns={"Close": "Rate"}, inplace=True)
    df["SMA200"] = df["Rate"].rolling(window=200).mean()
except Exception as e:
    send_to_discord(f"Error fetching data: {e}")
    raise

# Latest values
latest = df.iloc[-1]
rate_today = round(latest["Rate"], 2)
sma200 = round(latest["SMA200"], 2)
diff_krw = round(rate_today - sma200, 1)
diff_pct = round((rate_today - sma200) / sma200 * 100, 2)

# Signal logic
if rate_today <= sma200:
    title = "DOLLAR HUNTING TIME!! 200SMA BREACHED!!"
    body = (
        f"USD/KRW just dropped to **{rate_today:,} KRW**\n"
        f"200-day SMA: {sma200:,} KRW\n"
        f"Long-term accumulation zone activated!"
    )
    message = f"||@everyone|| **{title}**\n{body}"
else:
    title = "Dollar Hunter – Daily Update"
    body = (
        f"USD/KRW: {rate_today:,} KRW\n"
        f"200-day SMA: {sma200:,} KRW\n"
        f"→ {diff_krw:+} KRW ({diff_pct:+}%) vs SMA200\n"
        f"Wait for ~{sma200:,.0f} KRW or lower."
    )
    message = f"**{title}**\n{body}"

send_to_discord(message)
print(f"Alert sent @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
