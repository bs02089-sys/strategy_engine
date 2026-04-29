import yfinance as yf
import numpy as np
import requests
from bs4 import BeautifulSoup
import sqlite3
import datetime
import re
import os
import shutil
from dotenv import load_dotenv
from colorama import Fore, Style, init

# ========================
# 설정
# ========================
EMA_ALPHA = 0.3
DB_FILE = "exchange.db"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
}

load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

init(autoreset=True)

# ========================
# DB 초기화 (강력 마이그레이션)
# ========================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='adjustment'")
    if cur.fetchone():
        backup_file = f"exchange_backup_{datetime.date.today().isoformat()}.db"
        shutil.copy2(DB_FILE, backup_file)
        print(Fore.YELLOW + f"기존 DB 백업 생성됨: {backup_file}" + Style.RESET_ALL)
        cur.execute("DROP TABLE IF EXISTS adjustment")

    cur.execute("""
        CREATE TABLE adjustment (
            date TEXT PRIMARY KEY,
            naver REAL,
            namu REAL,
            factor REAL,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn


def get_latest_namu_rate(conn) -> float:
    """DB에서 가장 최근 나무증권 적용환율 가져오기"""
    cur = conn.cursor()
    cur.execute("""
        SELECT namu FROM adjustment 
        ORDER BY date DESC LIMIT 1
    """)
    row = cur.fetchone()
    return row[0] if row else None


def save_to_db(conn, naver: float, namu: float, factor: float):
    cur = conn.cursor()
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now().isoformat()
    
    cur.execute("""
        INSERT INTO adjustment (date, naver, namu, factor, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            naver = excluded.naver,
            namu = excluded.namu,
            factor = excluded.factor,
            created_at = excluded.created_at
    """, (today, naver, namu, factor, now))
    conn.commit()


# ========================
# 환율 가져오는 함수들
# ========================
def get_yfinance_rate() -> float:
    try:
        ticker = yf.Ticker("USDKRW=X")
        hist = ticker.history(period="2d")
        if hist.empty:
            raise ValueError("No data")
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        print(Fore.RED + f"[ERROR] yfinance: {e}" + Style.RESET_ALL)
        return 1450.0


def get_naver_rate() -> float:
    url = "https://finance.naver.com/marketindex/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")

        selectors = ["div.market1 div.head_info span.value", "span.value", ".head_info .value", "strong"]
        for sel in selectors:
            tag = soup.select_one(sel)
            if tag and tag.text.strip():
                text = re.sub(r'[^0-9.]', '', tag.text.strip())
                if text and 1000 < float(text) < 3000:
                    return float(text)

        match = re.search(r'(\d{4,5}\.?\d{0,2})', soup.get_text())
        if match:
            return float(match.group(1).replace(",", ""))
    except Exception as e:
        print(Fore.RED + f"[ERROR] 네이버: {e}" + Style.RESET_ALL)
    return None


def send_discord(message: str):
    if not WEBHOOK_URL:
        return
    try:
        requests.post(WEBHOOK_URL, json={"content": message}, timeout=10)
    except:
        pass


# ========================
# 메인
# ========================
def main():
    print(Fore.WHITE + Style.BRIGHT + "\n=== USD/KRW 환율 보정 & 매수 트리거 계산기 (완전 자동) ===\n" + Style.RESET_ALL)

    conn = init_db()

    # 1. 시장 기준환율
    ref_price = get_yfinance_rate()

    # 2. 네이버 환율
    naver_price = get_naver_rate() or ref_price

    # 3. 나무증권 적용환율 → DB에서 자동 불러오기
    namu_actual = get_latest_namu_rate(conn)
    is_first_run = namu_actual is None
    if is_first_run:
        namu_actual = naver_price  # 최초 실행 시 네이버 환율로 대체
        print(Fore.YELLOW + "DB에 이전 기록이 없어 네이버 환율을 임시 사용합니다." + Style.RESET_ALL)

    print(Fore.CYAN + f"나무증권 적용환율 (자동 불러오기):{Style.RESET_ALL} {namu_actual:.2f} KRW/USD")

    # 4. 보정 계수 계산
    observed_factor = namu_actual / naver_price
    prev_factor = 1.0 if is_first_run else None  # 첫 실행은 1.0
    if not is_first_run:
        cur = conn.cursor()
        cur.execute("SELECT factor FROM adjustment ORDER BY date DESC LIMIT 1")
        row = cur.fetchone()
        prev_factor = row[0] if row else 1.0

    adjustment_factor = EMA_ALPHA * observed_factor + (1 - EMA_ALPHA) * prev_factor

    # 5. DB 저장
    save_to_db(conn, naver_price, namu_actual, adjustment_factor)

    # 6. 계산
    namu_estimated = naver_price * adjustment_factor
    spread = (namu_estimated - ref_price) / ref_price

    # 7. 변동성
    try:
        df = yf.Ticker("USDKRW=X").history(period="6mo", interval="1d")
        df["ret"] = np.log(df["Close"] / df["Close"].shift(1))
        sigma_d = float(df["ret"].std())
    except:
        sigma_d = 0.0065

    triggers = {
        "1차 매수": ref_price * (1 - 0.5 * sigma_d),
        "2차 매수": ref_price * (1 - 1.0 * sigma_d),
        "3차 매수": ref_price * (1 - 1.5 * sigma_d),
    }
    adjusted_triggers = {k: v * (1 + spread) for k, v in triggers.items()}

    # ========================
    # 출력
    # ========================
    print(f"\n{Fore.CYAN}시장 기준 (yfinance):{Style.RESET_ALL} {ref_price:.2f}")
    print(f"{Fore.CYAN}네이버 금융:{Style.RESET_ALL} {naver_price:.2f}")
    print(f"{Fore.YELLOW}보정 계수:{Style.RESET_ALL} {adjustment_factor:.6f} (스프레드 {spread:+.2%})\n")

    print(f"{Fore.MAGENTA}▶ 나무증권 반영 매수 기준가{Style.RESET_ALL}")
    for name, price in adjusted_triggers.items():
        print(f"   {name} : {price:.2f} KRW")

    # Discord
    send_discord(f"""📊 환율 자동화 결과 (완전 자동) - {datetime.date.today().isoformat()}

시장: {ref_price:.2f} | 네이버: {naver_price:.2f} | 나무증권(자동): {namu_actual:.2f}
보정계수: {adjustment_factor:.5f} | 스프레드: {spread:+.2%}

1차: {adjusted_triggers['1차 매수']:.2f}
2차: {adjusted_triggers['2차 매수']:.2f}
3차: {adjusted_triggers['3차 매수']:.2f}""")

    conn.close()
    print(Fore.GREEN + "\n✅ 완전 자동 계산 완료!" + Style.RESET_ALL)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(Fore.RED + f"\n[CRITICAL] 오류: {e}" + Style.RESET_ALL)