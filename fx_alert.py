"""
나무증권 원화 → 달러 환율 적정성 알림 봇 (서울외국환중개 실시간 크롤링 및 고정 스프레드 반영 버전 - yf 에러 수정본)
"""

import os
from dataclasses import dataclass
import sys
import json
import logging
import time
import subprocess
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple, TypedDict
import bisect

# 필수 라이브러리 (BeautifulSoup4 설치 필요: pip install beautifulsoup4)
import requests
from bs4 import BeautifulSoup
import yfinance as yf  # 👈 [수정 완료] 과거 1년 통계 빌드용 yfinance 임포트 복원

# =============================================
# 로깅 설정
# =============================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

@dataclass
class Config:
    NAMUH_SPREAD: float = 10.0  # 달러당 기본 스프레드 (10.0원 또는 11.5원)
    PREFER_RATE: float = 95.0  # 우대율 (%)
    PERCENTILE_THRESHOLD: float = 50.0  # 고환율 시국에 맞춘 50% 기준선 고정!
    CHECK_INTERVAL: int = 300
    CACHE_HOURS: int = 6
    DISCORD_WEBHOOK: str = ""  
    DISCORD_USER_ID: str = ""  

    @classmethod
    def load(cls) -> "Config":
        config_data = {
            "NAMUH_SPREAD": 10.0,
            "PREFER_RATE": 95.0,
            "PERCENTILE_THRESHOLD": 50.0,
            "CHECK_INTERVAL": 300,
            "CACHE_HOURS": 6,
            "DISCORD_WEBHOOK": "",
            "DISCORD_USER_ID": "",
        }

        if os.path.exists("config.json"):
            try:
                with open("config.json", "r", encoding="utf-8") as f:
                    file_data = json.load(f)
                    for key in config_data.keys():
                        val = file_data.get(key) or file_data.get(key.lower())
                        if val is not None:
                            config_data[key] = val
            except Exception as e:
                print(f"config.json 읽기 실패: {e}")

        for key in config_data.keys():
            env_val = os.getenv(key)
            if env_val is not None:
                config_data[key] = env_val

        return cls(
            NAMUH_SPREAD=float(config_data["NAMUH_SPREAD"]),
            PREFER_RATE=float(config_data["PREFER_RATE"]),
            PERCENTILE_THRESHOLD=float(config_data["PERCENTILE_THRESHOLD"]),
            CHECK_INTERVAL=int(config_data["CHECK_INTERVAL"]),
            CACHE_HOURS=int(config_data["CACHE_HOURS"]),
            DISCORD_WEBHOOK=str(config_data["DISCORD_WEBHOOK"]),
            DISCORD_USER_ID=str(config_data["DISCORD_USER_ID"]),
        )

# 설정 로드
config = Config.load()

WEBHOOK_URL = config.DISCORD_WEBHOOK
DISCORD_USER_ID = config.DISCORD_USER_ID

COLOR_GREEN = 0x1D9E75
COLOR_AMBER = 0xBA7517
COLOR_RED   = 0xA32D2D

class RateInfo(TypedDict):
    base_rate: float
    applied_rate: float
    spread_cost: float
    effective_spread: float

CACHE_FILE = Path("historical_rates_cache.json")

def save_cache(rates: list[float]):
    try:
        data = {"rates": rates, "timestamp": datetime.now().isoformat()}
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"캐시 저장 실패: {e}")

def load_cache() -> list[float] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if datetime.now() - datetime.fromisoformat(data["timestamp"]) < timedelta(hours=config.CACHE_HOURS):
            return data["rates"]
    except Exception:
        pass
    return None

# =============================================
# 🎯 서울외국환중개 실시간 매매기준율 크롤링 엔진
# =============================================
def get_current_rate() -> float | None:
    """서울외국환중개(SMBS) 웹사이트에서 실시간 미국 달러(USD) 매매기준율을 크롤링합니다."""
    url = "https://www.smbs.biz/ExRate/TodayExRate.jsp"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        
        # 인코딩 방어코드 (EUC-KR 처리)
        resp.encoding = resp.apparent_encoding or 'euc-kr'
        
        soup = BeautifulSoup(resp.text, "html.parser")
        
        tables = soup.find_all("table")
        for table in tables:
            text = table.get_text()
            if "미국" in text or "USD" in text:
                rows = table.find_all("tr")
                for row in rows:
                    cells = row.find_all(["td", "th"])
                    cell_texts = [c.get_text(strip=True) for c in cells]
                    
                    if any("미국" in txt or "USD" in txt for txt in cell_texts):
                        for txt in cell_texts:
                            clean_txt = txt.replace(",", "")
                            match = re.search(r"^\d+\.\d+$|^\d+$", clean_txt)
                            if match:
                                val = float(match.group())
                                if 900 <= val <= 2000:
                                    return val
                                    
    except Exception as e:
        logger.error(f"❌ 서울외국환중개 실시간 환율 크롤링 실패: {e}")
        
    return get_backup_rate()

def get_backup_rate() -> float | None:
    """서울외국환중개 서버가 일시 오류일 때 작동하는 2차 백업 환율망"""
    try:
        ticker = yf.Ticker("KRW=X")
        df = ticker.history(period="1d")
        if not df.empty:
            return float(df['Close'].iloc[-1])
    except Exception:
        pass
    return None

def get_historical_rates(days: int = 365) -> list[float]:
    """과거 1년 치 통계용 데이터셋을 안전하게 빌드합니다."""
    cached = load_cache()
    if cached:
        return cached

    try:
        logger.info(f"통계 분석을 위해 과거 {days}일간의 뼈대 환율 데이터를 빌드합니다...")
        ticker = yf.Ticker("KRW=X")
        df = ticker.history(period="1y")
        if df.empty:
            raise ValueError("통계 데이터가 비어 있습니다.")
        rates = [float(val) for val in df['Close'].dropna().tolist()]
        sorted_rates = sorted(rates)
        save_cache(sorted_rates)
        return sorted_rates
    except Exception as e:
        logger.error(f"통계 데이터 빌드 실패: {e}")
        return []

# =============================================
# 계산 및 비즈니스 로직
# =============================================
def calc_applied_rate(base_rate: float) -> RateInfo:
    """나무증권의 실제 우대 환율 계산법을 적용합니다."""
    spread_cost = config.NAMUH_SPREAD * (1 - config.PREFER_RATE / 100)
    spread_cost = round(spread_cost, 2)  
    
    applied_rate = base_rate + spread_cost
    effective_spread = (spread_cost / base_rate) * 100 

    return {
        "base_rate"       : round(base_rate, 2),
        "applied_rate"    : round(applied_rate, 2),
        "spread_cost"     : spread_cost,
        "effective_spread": round(effective_spread, 4),
    }

def calc_percentile(sorted_rates: list[float], current: float) -> float:
    n = len(sorted_rates)
    if n == 0:
        return 50.0
    idx = bisect.bisect_right(sorted_rates, current)
    return round((idx / n) * 100, 1)

def get_median_applied_rate(sorted_base_rates: list[float]) -> float:
    if not sorted_base_rates:
        return 0.0
    median_base = sorted_base_rates[len(sorted_base_rates) // 2]
    return calc_applied_rate(median_base)["applied_rate"]

def get_rating(percentile: float) -> Tuple[str, str]:
    if percentile <= 33:
        return "🟢 매수 적기", "최근 1년 중 상위권 매수 기회입니다."
    elif percentile <= 50:
        return "🟠 보통", "평균 수준입니다."
    elif percentile <= 70:
        return "🔴 비싼 편", "평균보다 불리합니다. 대기를 권장합니다."
    else:
        return "🔴 매우 비쌈", "최근 1년 중 고점 구간입니다."

# =============================================
# Discord 알림 및 Git 동기화
# =============================================
def send_discord_alert(rate_info: RateInfo, percentile: float, median_applied: float, is_recommended: bool) -> bool:
    if not WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK이 설정되지 않았습니다.")
        return False

    rating, comment  = get_rating(percentile)
    current_applied  = rate_info["applied_rate"]
    diff             = current_applied - median_applied
    comparison       = f"중간값보다 **{abs(diff):.2f}원 {'낮음 (유리)' if diff <= 0 else '높음 (불리)'}**"
    comparison_emoji = "🟢" if diff <= 0 else "🔴"

    filled = max(0, min(10, int(percentile / 10)))
    bar    = "🟩" * filled + "⬜" * (10 - filled)

    title = f"✅ 달러 매수 적기! {rating}" if is_recommended else f"📊 현재 환율 현황 — {rating}"
    color = COLOR_GREEN if is_recommended else COLOR_AMBER

    payload = {
        "username": "📉 나무증권 환전봇 (서울외국환 소스)",
        "embeds"  : [{
            "title"      : title,
            "description": f"현재 적용환율은 최근 1년 중 **하위 {percentile:.1f}%** 수준입니다.\n{comment}",
            "color"      : color,
            "fields"     : [
                {"name": "💱 서울외국환 기준율", "value": f"**₩{rate_info['base_rate']:,.2f}**",  "inline": True},
                {"name": "🏦 나무증권 적용환율", "value": f"**₩{current_applied:,.2f}**",         "inline": True},
                {"name": "💸 스프레드 수수료",   "value": f"₩{rate_info['spread_cost']:,.2f}",    "inline": True},
                {
                    "name" : "📉 적정성 (낮을수록 유리)",
                    "value": f"하위 **{percentile:.1f}%** — {rating}\n{bar}\n⬅️ 싼 구간       비싼 구간 ➡️",
                    "inline": False,
                },
                {
                    "name" : "📆 최근 1년 나무증권 적용환율",
                    "value": f"중간값 **₩{median_applied:,.2f}**\n{comparison_emoji} {comparison}",
                    "inline": False,
                },
            ],
            "timestamp": datetime.now(tz=__import__('datetime').timezone.utc).isoformat(),
            "footer"   : {"text": f"우대율 {config.PREFER_RATE}% • 알림 기준 하위 {config.PERCENTILE_THRESHOLD:.0f}%"},
        }],
    }

    if DISCORD_USER_ID and is_recommended:
        payload["content"] = f"<@{DISCORD_USER_ID}>"

    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Discord 알림 전송 성공")
        return True
    except Exception as e:
        logger.error(f"Discord 알림 실패: {e}")
        return False

def sync_config_to_git(rate_info: dict, percentile: float):
    config_path = Path("config.json")
    if config_path.exists():
        try:
            full_config = json.loads(config_path.read_text(encoding='utf-8'))
        except Exception as e:
            logger.error(f"❌ config.json 읽기 실패: {e}")
            full_config = {}
    else:
        full_config = {}

    if "exchange_status" not in full_config:
        full_config["exchange_status"] = {}
        
    full_config["exchange_status"]["LAST_CHECK_DATE"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_config["exchange_status"]["CURRENT_APPLIED_RATE"] = rate_info['applied_rate']
    full_config["exchange_status"]["CURRENT_PERCENTILE"] = percentile

    config_path.write_text(json.dumps(full_config, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info("📝 config.json의 실시간 환율 정보 자동 기입 완료.")

    try:
        status = subprocess.run(["git", "status", "--porcelain", str(config_path)], capture_output=True, text=True)
        if not status.stdout.strip():
            logger.info("ℹ️ 환율 변동 사항이 없어 Git 커밋을 생략합니다.")
            return

        is_github_action = os.getenv("GITHUB_ACTIONS") == "true"
        subprocess.run(["git", "config", "--local", "user.name", "Automated Bot"], check=True)
        subprocess.run(["git", "config", "--local", "user.email", "bot@example.com"], check=True)
        subprocess.run(["git", "add", str(config_path)], check=True)
        subprocess.run(["git", "commit", "-m", f"🤖 Auto-update config.json [Rate: {rate_info['applied_rate']}]"], check=True)
        
        if is_github_action:
            subprocess.run(["git", "push"], check=True)
        else:
            subprocess.run(["git", "push", "origin", "main"], check=True)
        logger.info("🚀 깃허브 서버로 동기화 완료.")
    except Exception as git_err:
        logger.error(f"❌ Git 동기화 중 에러 발생: {git_err}")

def analyze() -> Tuple[bool, RateInfo | dict, float, float]:
    current = get_current_rate()
    if current is None:
        return False, {}, 0.0, 0.0

    historical     = get_historical_rates(365)
    percentile     = calc_percentile(historical, current)
    median_applied = get_median_applied_rate(historical)
    rate_info      = calc_applied_rate(current)
    is_recommended = percentile <= config.PERCENTILE_THRESHOLD

    return is_recommended, rate_info, percentile, median_applied

# =============================================
# 실행 컨트롤러
# =============================================
def run_once():
    logger.info("=== 나무증권 환율 봇 (서울외국환 실시간 모드) ===")
    try:
        is_recommended, rate_info, percentile, median_applied = analyze()
        if not rate_info or len(rate_info) == 0:
            logger.error("환율 조회 실패 - 서울외국환중개 서버 응답 없음")
            return

        logger.info(f"실시간 적용환율: ₩{rate_info['applied_rate']:,.2f} | 백분위: 하위 {percentile:.1f}%")
        send_discord_alert(rate_info, percentile, median_applied, is_recommended)
        sync_config_to_git(rate_info, percentile)
    except Exception as e:
        logger.error(f"run_once 오류 발생: {e}")

def run_monitor():
    logger.info("=== 나무증권 환율 봇 — 서울외국환 모니터링 모드 ===")
    consecutive_errors = 0
    while True:
        try:
            is_recommended, rate_info, percentile, median_applied = analyze()
            if not rate_info or len(rate_info) == 0:
                consecutive_errors += 1
            else:
                consecutive_errors = 0
                logger.info(f"조회 성공 | 적용환율: ₩{rate_info['applied_rate']:,.2f} | 하위 {percentile:.1f}%")

                if is_recommended:
                    send_discord_alert(rate_info, percentile, median_applied, True)
                    sync_config_to_git(rate_info, percentile)
                    break
            time.sleep(config.CHECK_INTERVAL)
        except KeyboardInterrupt:
            logger.info("👋 사용자에 의해 모니터링이 종료되었습니다.")
            break
        except Exception as e:
            # 💡 아래처럼 e를 로그 출력에 사용하면 비활성 상태가 깔끔하게 해결됩니다!
            logger.error(f"⚠️ 모니터링 중 예기치 못한 오류 발생: {e}")
            time.sleep(60)
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "monitor":
        run_monitor()
    else:
        run_once()