"""
나무증권 원화 → 달러 환율 적정성 알림 봇 (개선 버전)
"""

import os
import sys
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass
from typing import TypedDict, Tuple

import requests

# =============================================
# 로깅 설정
# =============================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("fx_alert.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# =============================================
# 설정 관리
# =============================================
@dataclass
class Config:
    NAMUH_SPREAD: float = 1.75
    PREFER_RATE: float = 95.0
    PERCENTILE_THRESHOLD: float = 30.0
    CHECK_INTERVAL: int = 300
    CACHE_HOURS: int = 6

    @classmethod
    def load(cls) -> "Config":
        """환경변수에서 설정 로드"""
        return cls(
            NAMUH_SPREAD=float(os.getenv("NAMUH_SPREAD", "1.75")),
            PREFER_RATE=float(os.getenv("PREFER_RATE", "95")),
            PERCENTILE_THRESHOLD=float(os.getenv("PERCENTILE_THRESHOLD", "30")),
            CHECK_INTERVAL=int(os.getenv("CHECK_INTERVAL", "300")),
        )


config = Config.load()

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK", "")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID", "")

# Discord Embed 색상
COLOR_GREEN = 0x1D9E75
COLOR_AMBER = 0xBA7517
COLOR_RED = 0xA32D2D


# =============================================
# 타입 정의
# =============================================
class RateInfo(TypedDict):
    base_rate: float
    applied_rate: float
    spread_cost: float
    effective_spread: float


class Stats(TypedDict):
    count: int
    min: float
    p20: float
    p30: float
    p50: float
    p70: float
    max: float


# =============================================
# 캐싱 관련
# =============================================
CACHE_FILE = Path("historical_rates_cache.json")


def save_cache(rates: list[float]) -> None:
    """환율 데이터를 캐싱"""
    try:
        data = {
            "rates": rates,
            "timestamp": datetime.now().isoformat(),
        }
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        logger.debug("환율 캐시 저장 완료")
    except Exception as e:
        logger.warning(f"캐시 저장 실패: {e}")


def load_cache() -> list[float] | None:
    """캐시에서 데이터 로드 (유효기간: 6시간)"""
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        cached_time = datetime.fromisoformat(data["timestamp"])
        if datetime.now() - cached_time < timedelta(hours=config.CACHE_HOURS):
            logger.debug("캐시된 환율 데이터 사용")
            return data["rates"]
    except Exception as e:
        logger.warning(f"캐시 로드 실패: {e}")
    return None


# =============================================
# 환율 데이터 조회
# =============================================
def get_current_rate() -> float | None:
    """현재 USD/KRW 환율 조회"""
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") == "success":
            return float(data["rates"]["KRW"])
    except Exception as e:
        logger.error(f"현재 환율 조회 실패: {e}")
    return None


def get_historical_rates(days: int = 365) -> list[float]:
    """최근 N일 환율 데이터 (캐싱 적용)"""
    cached = load_cache()
    if cached:
        return cached

    try:
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        url = f"https://api.frankfurter.app/{start_date}..{end_date}?from=USD&to=KRW"

        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        rates = [float(v["KRW"]) for v in data.get("rates", {}).values()]

        sorted_rates = sorted(rates)
        save_cache(sorted_rates)
        return sorted_rates

    except Exception as e:
        logger.error(f"과거 환율 조회 실패: {e}")
        return []


# =============================================
# 계산 함수들
# =============================================
def calc_applied_rate(base_rate: float) -> RateInfo:
    """나무증권 적용 환율 계산"""
    effective_spread = config.NAMUH_SPREAD * (1 - config.PREFER_RATE / 100)
    applied_rate = base_rate * (1 + effective_spread / 100)
    spread_cost = applied_rate - base_rate

    return {
        "base_rate": round(base_rate, 2),
        "applied_rate": round(applied_rate, 2),
        "spread_cost": round(spread_cost, 2),
        "effective_spread": round(effective_spread, 4),
    }


def calc_percentile(sorted_rates: list[float], current: float) -> float:
    """개선된 퍼센타일 계산"""
    n = len(sorted_rates)
    if n == 0:
        return 50.0

    better = sum(1 for r in sorted_rates if r < current)          # 더 싼 날
    equal_or_better = sum(1 for r in sorted_rates if r <= current)

    # 두 방식의 평균을 사용하여 더 정확하게 계산
    percentile = (better + equal_or_better) / 2 / n * 100
    return round(percentile, 1)


def get_percentile_stats(sorted_rates: list[float]) -> Stats | dict:
    """퍼센타일 통계"""
    n = len(sorted_rates)
    if n == 0:
        return {}

    def pct(p: float) -> float:
        idx = int(n * p / 100)
        return sorted_rates[min(idx, n - 1)]

    return {
        "count": n,
        "min": sorted_rates[0],
        "p20": pct(20),
        "p30": pct(30),
        "p50": pct(50),
        "p70": pct(70),
        "max": sorted_rates[-1],
    }


def get_rating(percentile: float) -> Tuple[str, str]:
    """등급 및 코멘트 반환"""
    if percentile <= 20:
        return "🟢 매우 저렴", "최근 1년 중 상위권 매수 기회입니다."
    elif percentile <= 30:
        return "🟡 저렴한 편", "평소보다 유리한 환율입니다."
    elif percentile <= 50:
        return "🟠 보통", "평균 수준의 환율입니다. 조금 더 기다려볼 만합니다."
    elif percentile <= 70:
        return "🔴 비싼 편", "평균보다 불리한 환율입니다. 대기를 권장합니다."
    else:
        return "🔴 매우 비쌈", "최근 1년 중 고점 구간입니다. 환전을 미루세요."


# =============================================
# Discord 알림
# =============================================
def send_discord_alert(
    rate_info: RateInfo, percentile: float, stats: dict, is_recommended: bool
) -> bool:
    """Discord 알림 전송"""
    if not WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK이 설정되지 않았습니다.")
        return False

    rating, comment = get_rating(percentile)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if is_recommended:
        title = f"✅ 달러 매수 적기! {rating}"
        description = (
            f"현재 환율이 최근 1년 중 **하위 {percentile:.1f}%** 수준입니다.\n"
            f"{comment}\n나무증권 앱에서 환전을 진행하세요!"
        )
        color = COLOR_GREEN
    else:
        title = f"📊 현재 환율 현황 — {rating}"
        description = f"현재 환율은 최근 1년 중 **하위 {percentile:.1f}%** 수준입니다.\n{comment}"
        color = COLOR_AMBER

    filled = max(0, min(10, int((100 - percentile) / 10)))
    bar = "🟩" * filled + "⬜" * (10 - filled)

    fields = [
        {"name": "💱 매매기준율 (시장)", "value": f"**₩{rate_info['base_rate']:,.2f}** / $1", "inline": True},
        {"name": "🏦 나무증권 적용환율", "value": f"**₩{rate_info['applied_rate']:,.2f}** / $1", "inline": True},
        {"name": "💸 1달러당 스프레드 비용", "value": f"₩{rate_info['spread_cost']:,.2f}", "inline": True},
        {
            "name": "📉 적정성 (낮을수록 유리)",
            "value": f"하위 **{percentile:.1f}%** — {rating}\n{bar}",
            "inline": False,
        },
    ]

    if stats:
        fields.append({
            "name": "📆 최근 1년 매매기준율 구간",
            "value": (
                f"최저(최유리) ₩{stats['min']:,.0f}\n"
                f"하위 20%    ₩{stats['p20']:,.0f}\n"
                f"하위 30%    ₩{stats['p30']:,.0f}  ←  알림 기준\n"
                f"중간값      ₩{stats['p50']:,.0f}\n"
                f"상위 30%    ₩{stats['p70']:,.0f}\n"
                f"최고(최불리) ₩{stats['max']:,.0f}\n"
                f"거래일 기준 {stats['count']}개 데이터"
            ),
            "inline": False,
        })

    payload = {
        "username": "📉 나무증권 환전봇",
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "fields": fields,
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": f"우대율 {config.PREFER_RATE}% • 스프레드 {config.NAMUH_SPREAD}% • 알림기준 하위 {config.PERCENTILE_THRESHOLD:.0f}%"},
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
        logger.error(f"Discord 알림 전송 실패: {e}")
        return False


def send_error_alert(message: str):
    """오류 알림"""
    if not WEBHOOK_URL:
        return
    # ... (기존과 동일, 생략)
    pass


# =============================================
# 핵심 분석
# =============================================
def analyze() -> Tuple[bool, RateInfo, float, dict]:
    current = get_current_rate()
    if current is None:
        return False, {}, 0.0, {}

    historical = get_historical_rates(365)
    percentile = calc_percentile(historical, current)
    stats = get_percentile_stats(historical)
    rate_info = calc_applied_rate(current)
    is_recommended = percentile <= config.PERCENTILE_THRESHOLD

    return is_recommended, rate_info, percentile, stats


# =============================================
# 실행 함수
# =============================================
def run_once():
    logger.info("=== 나무증권 환율 적정성 봇 (1회 실행) ===")
    is_recommended, rate_info, percentile, stats = analyze()

    if not rate_info:
        logger.error("환율 조회 실패")
        send_error_alert("환율 데이터 조회에 실패했습니다.")
        sys.exit(1)

    rating, comment = get_rating(percentile)
    logger.info(f"매매기준율: ₩{rate_info['base_rate']:,.2f} | "
                f"적용환율: ₩{rate_info['applied_rate']:,.2f} | "
                f"하위 {percentile:.1f}% {rating}")

    send_discord_alert(rate_info, percentile, stats, is_recommended)


def run_monitor():
    logger.info("=== 나무증권 환율 적정성 봇 — 모니터링 모드 ===")
    consecutive_errors = 0

    while True:
        try:
            is_recommended, rate_info, percentile, stats = analyze()

            if not rate_info:
                consecutive_errors += 1
                logger.warning(f"환율 조회 실패 ({consecutive_errors}회 연속)")
                if consecutive_errors >= 3:
                    send_error_alert(f"환율 조회 {consecutive_errors}회 연속 실패")
                    consecutive_errors = 0
            else:
                consecutive_errors = 0
                rating, _ = get_rating(percentile)
                logger.info(f"기준 ₩{rate_info['base_rate']:,.2f} | 적용 ₩{rate_info['applied_rate']:,.2f} | "
                            f"하위 {percentile:.1f}% {rating}")

                if is_recommended:
                    send_discord_alert(rate_info, percentile, stats, True)
                    logger.info("매수 적기 알림 전송 후 모니터링 종료")
                    break

            time.sleep(config.CHECK_INTERVAL)

        except KeyboardInterrupt:
            logger.info("모니터링 종료")
            break
        except Exception as e:
            logger.error(f"예기치 못한 오류: {e}")
            time.sleep(60)


# =============================================
# 메인
# =============================================
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "monitor":
        run_monitor()
    else:
        run_once()