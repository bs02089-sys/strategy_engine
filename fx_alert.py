"""
나무증권 원화 → 달러 환율 적정성 알림 봇 (yfinance 연동 및 고정 스프레드 반영 버전)
"""

import os
import sys
import json
import logging
import time
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass
from typing import Tuple, TypedDict
import bisect

# 필수 라이브러리
import requests
import yfinance as yf

# =============================================
# 로깅 설정
# =============================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# =============================================
# 설정 (나무증권 달러 스프레드는 '원' 기준 고정값)
# =============================================
@dataclass
class Config:
    # 💡 나무증권의 실제 달러 기본 스프레드는 달러당 10.0원 또는 11.5원입니다.
    # 본인의 앱 화면(매매기준율과 적용환율의 차이)을 확인하고 맞추면 오차가 0원이 됩니다.
    NAMUH_SPREAD: float = 10.0       # 달러당 기본 스프레드 (10.0원 또는 11.5원)
    PREFER_RATE: float = 95.0        # 우대율 (%)
    PERCENTILE_THRESHOLD: float = 33.0
    CHECK_INTERVAL: int = 300
    CACHE_HOURS: int = 6

    @classmethod
    def load(cls) -> "Config":
        return cls(
            NAMUH_SPREAD=float(os.getenv("NAMUH_SPREAD", "10.0")),
            PREFER_RATE=float(os.getenv("PREFER_RATE", "95")),
            PERCENTILE_THRESHOLD=float(os.getenv("PERCENTILE_THRESHOLD", "33")),
            CHECK_INTERVAL=int(os.getenv("CHECK_INTERVAL", "300")),
        )


config = Config.load()

WEBHOOK_URL     = os.getenv("DISCORD_WEBHOOK", "")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID", "")

COLOR_GREEN = 0x1D9E75
COLOR_AMBER = 0xBA7517
COLOR_RED   = 0xA32D2D


# =============================================
# 타입 정의
# =============================================
class RateInfo(TypedDict):
    base_rate: float
    applied_rate: float
    spread_cost: float
    effective_spread: float


# =============================================
# 캐싱
# =============================================
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
# 환율 조회 (yfinance 라이브러리 기반으로 통일)
# =============================================
def get_current_rate() -> float | None:
    """야후 파이낸스에서 실시간 USDKRW 환율을 가져옵니다."""
    try:
        # 'KRW=X'는 야후 파이낸스의 달러/원 환율 티커입니다.
        ticker = yf.Ticker("KRW=X")
        # 최근 1일 데이터를 분 단위로 신속하게 조회
        df = ticker.history(period="1d", interval="1m")
        if not df.empty:
            return float(df['Close'].iloc[-1])
        
        # 분 단위 데이터가 일시적으로 빌 경우 일별 데이터로 백업 조회
        df_backup = ticker.history(period="1d")
        if not df_backup.empty:
            return float(df_backup['Close'].iloc[-1])
            
    except Exception as e:
        logger.error(f"yfinance 실시간 환율 조회 실패: {e}")
    return None


def get_historical_rates(days: int = 365) -> list[float]:
    """야후 파이낸스에서 과거 1년 치 환율 데이터를 가져와 정렬 후 반환합니다."""
    cached = load_cache()
    if cached:
        return cached

    try:
        logger.info(f"yfinance를 통해 과거 {days}일간의 환율 데이터를 가져옵니다...")
        ticker = yf.Ticker("KRW=X")
        # 1년(1y) 치 일별 데이터를 조회
        df = ticker.history(period="1y")
        
        if df.empty:
            raise ValueError("과거 데이터가 비어 있습니다.")

        # 종가(Close) 리스트 추출 후 정렬
        rates = [float(val) for val in df['Close'].dropna().tolist()]
        sorted_rates = sorted(rates)
        
        save_cache(sorted_rates)
        return sorted_rates
    except Exception as e:
        logger.error(f"yfinance 과거 환율 조회 실패: {e}")
        return []


# =============================================
# 계산 함수 (실제 나무증권 원화 고정 스프레드 공식)
# =============================================
def calc_applied_rate(base_rate: float) -> RateInfo:
    """나무증권의 실제 우대 환율 계산법을 적용합니다."""
    # 스프레드 비용 = 고정 스프레드(원) * (1 - 우대율)
    # 예: 10원 * (1 - 0.95) = 0.5원 수수료 발생
    spread_cost = config.NAMUH_SPREAD * (1 - config.PREFER_RATE / 100)
    spread_cost = round(spread_cost, 2)  # 국내 금융 고시 기준 소수점 둘째 자리 반올림
    
    # 최종 적용 환율 = 매매기준율 + 스프레드 비용
    applied_rate = base_rate + spread_cost
    
    # 역산한 백분율 수수료율 (참고용)
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
    # 이진 탐색(bisect)을 활용하여 정확하고 빠른 하위 백분위 계산
    idx = bisect.bisect_right(sorted_rates, current)
    return round((idx / n) * 100, 1)


def get_median_applied_rate(sorted_base_rates: list[float]) -> float:
    if not sorted_base_rates:
        return 0.0
    median_base = sorted_base_rates[len(sorted_base_rates) // 2]
    return calc_applied_rate(median_base)["applied_rate"]


def get_rating(percentile: float) -> Tuple[str, str]:
    # 🎯 33% 이하면 무조건 초록불! 아주 심플하고 명확해집니다.
    if percentile <= 33:
        return "🟢 매수 적기", "최근 1년 중 상위권 매수 기회입니다."
    elif percentile <= 50:
        return "🟠 보통", "평균 수준입니다."
    elif percentile <= 70:
        return "🔴 비싼 편", "평균보다 불리합니다. 대기를 권장합니다."
    else:
        return "🔴 매우 비씀", "최근 1년 중 고점 구간입니다."
    

# =============================================
# Discord 알림
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
        "username": "📉 나무증권 환전봇",
        "embeds"  : [{
            "title"      : title,
            "description": f"현재 적용환율은 최근 1년 중 **하위 {percentile:.1f}%** 수준입니다.\n{comment}",
            "color"      : color,
            "fields"     : [
                {"name": "💱 시장 기준율",       "value": f"**₩{rate_info['base_rate']:,.2f}**",  "inline": True},
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


# =============================================
# [양방향 동기화 엔진] fx_config.json 업데이트 및 Git Push
# =============================================
def sync_config_to_git(rate_info: dict, percentile: float):
    """기존 config.json의 다른 설정은 그대로 유지하고, exchange_status만 안전하게 업데이트합니다."""
    config_path = Path("config.json") # 💡 통합 설정 파일명으로 변경
    
    # 1. 기존 파일이 있으면 먼저 읽어와서 다른 설정들을 보존합니다.
    if config_path.exists():
        try:
            full_config = json.loads(config_path.read_text(encoding='utf-8'))
        except Exception as e:
            logger.error(f"❌ config.json 읽기 실패 (포맷 오류 가능성): {e}")
            full_config = {}
    else:
        full_config = {}

    # 2. 다른 키값(설정들)은 건드리지 않고, exchange_status 구조만 만들거나 업데이트합니다.
    if "exchange_status" not in full_config:
        full_config["exchange_status"] = {}
        
    full_config["exchange_status"]["LAST_CHECK_DATE"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_config["exchange_status"]["CURRENT_APPLIED_RATE"] = rate_info['applied_rate']
    full_config["exchange_status"]["CURRENT_PERCENTILE"] = percentile

    # 3. 다른 설정들이 포함된 전체 데이터를 다시 안전하게 저장합니다.
    config_path.write_text(json.dumps(full_config, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info("📝 config.json의 환율 정보 업데이트 완료 (기존 설정 보존).")

    # 4. Git Push 작업 (동일)
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
        logger.info("🚀 config.json 깃허브 푸시 완료.")
            
    except Exception as git_err:
        logger.error(f"❌ Git 동기화 중 에러 발생: {git_err}")
        

# =============================================
# 분석
# =============================================
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
# 실행 모드
# =============================================
def run_once():
    logger.info("=== 나무증권 환율 적정성 봇 (1회 실행) ===")
    try:
        is_recommended, rate_info, percentile, median_applied = analyze()

        if not rate_info or not isinstance(rate_info, dict) or len(rate_info) == 0:
            logger.error("환율 조회 실패 - yfinance 응답 없음")
            return

        logger.info(f"적용환율: ₩{rate_info['applied_rate']:,.2f} | 하위 {percentile:.1f}%")
        
        send_discord_alert(rate_info, percentile, median_applied, is_recommended)
        sync_config_to_git(rate_info, percentile)

    except Exception as e:
        logger.error(f"run_once 실행 중 오류 발생: {e}")


def run_monitor():
    logger.info("=== 나무증권 환율 적정성 봇 — 모니터링 모드 ===")
    consecutive_errors = 0

    while True:
        try:
            is_recommended, rate_info, percentile, median_applied = analyze()

            if not rate_info or not isinstance(rate_info, dict) or len(rate_info) == 0:
                consecutive_errors += 1
                logger.warning(f"환율 조회 실패 ({consecutive_errors}회 연속)")
            else:
                consecutive_errors = 0
                logger.info(
                    f"적용환율: ₩{rate_info['applied_rate']:,.2f} | "
                    f"하위 {percentile:.1f}% | 추천: {is_recommended}"
                )

                if is_recommended:
                    logger.info("✅ 매수 적기 감지! Discord 알림 전송")
                    send_discord_alert(rate_info, percentile, median_applied, True)
                    sync_config_to_git(rate_info, percentile)
                    logger.info("매수 적기 알림 전송 후 모니터링 종료")
                    break

            time.sleep(config.CHECK_INTERVAL)

        except KeyboardInterrupt:
            logger.info("👋 사용자에 의해 모니터링이 종료되었습니다.")
            break
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"예기치 못한 오류 발생 ({consecutive_errors}회): {e}")
            time.sleep(60)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "monitor":
        run_monitor()
    else:
        run_once()