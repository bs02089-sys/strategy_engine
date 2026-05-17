"""
나무증권 원화 → 달러 환율 적정성 알림 봇 (fx_config.json 전용 파일 분리 버전)
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
from typing import TypedDict, Tuple

import requests

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
# 설정
# =============================================
@dataclass
class Config:
    NAMUH_SPREAD: float = 1.75
    PREFER_RATE: float = 95.0
    PERCENTILE_THRESHOLD: float = 25.0
    CHECK_INTERVAL: int = 300
    CACHE_HOURS: int = 6

    @classmethod
    def load(cls) -> "Config":
        return cls(
            NAMUH_SPREAD=float(os.getenv("NAMUH_SPREAD", "1.75")),
            PREFER_RATE=float(os.getenv("PREFER_RATE", "95")),
            PERCENTILE_THRESHOLD=float(os.getenv("PERCENTILE_THRESHOLD", "25")),
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
# 환율 조회
# =============================================
def get_current_rate() -> float | None:
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
    cached = load_cache()
    if cached:
        return cached

    try:
        end_date   = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        url = f"https://api.frankfurter.app/{start_date}..{end_date}?from=USD&to=KRW"

        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data  = resp.json()
        rates = [float(v["KRW"]) for v in data.get("rates", {}).values()]
        sorted_rates = sorted(rates)
        save_cache(sorted_rates)
        return sorted_rates
    except Exception as e:
        logger.error(f"과거 환율 조회 실패: {e}")
        return []


# =============================================
# 계산 함수
# =============================================
def calc_applied_rate(base_rate: float) -> RateInfo:
    effective_spread = config.NAMUH_SPREAD * (1 - config.PREFER_RATE / 100)
    applied_rate     = base_rate * (1 + effective_spread / 100)
    spread_cost      = applied_rate - base_rate

    return {
        "base_rate"       : round(base_rate, 2),
        "applied_rate"    : round(applied_rate, 2),
        "spread_cost"     : round(spread_cost, 2),
        "effective_spread": round(effective_spread, 4),
    }


def calc_percentile(sorted_rates: list[float], current: float) -> float:
    n = len(sorted_rates)
    if n == 0:
        return 50.0
    better          = sum(1 for r in sorted_rates if r < current)
    equal_or_better = sum(1 for r in sorted_rates if r <= current)
    return round((better + equal_or_better) / 2 / n * 100, 1)


def get_median_applied_rate(sorted_base_rates: list[float]) -> float:
    if not sorted_base_rates:
        return 0.0
    median_base = sorted_base_rates[len(sorted_base_rates) // 2]
    return calc_applied_rate(median_base)["applied_rate"]


def get_rating(percentile: float) -> Tuple[str, str]:
    if percentile <= 20:
        return "🟢 매우 저렴", "최근 1년 중 상위권 매수 기회입니다."
    elif percentile <= 25:
        return "🟡 저렴한 편", "평소보다 유리한 환율입니다."
    elif percentile <= 50:
        return "🟠 보통", "평균 수준입니다."
    elif percentile <= 70:
        return "🔴 비싼 편", "평균보다 불리합니다. 대기를 권장합니다."
    else:
        return "🔴 매우 비쌈", "최근 1년 중 고점 구간입니다."


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
                {"name": "💸 스프레드 비용",     "value": f"₩{rate_info['spread_cost']:,.2f}",    "inline": True},
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
    """환율 전용 설정 파일(fx_config.json)을 생성/수정하고 깃허브로 Push합니다."""
    config_path = Path("fx_config.json") # 💡 독립된 환율 전용 파일명 지정
    
    # 파일이 없으면 빈 제이슨 구조를 메모리에 생성
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                full_config = json.load(f)
        except Exception:
            full_config = {}
    else:
        full_config = {}

    try:
        if "exchange_status" not in full_config:
            full_config["exchange_status"] = {}
            
        full_config["exchange_status"]["LAST_CHECK_DATE"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        full_config["exchange_status"]["CURRENT_APPLIED_RATE"] = rate_info['applied_rate']
        full_config["exchange_status"]["CURRENT_PERCENTILE"] = percentile

        # 전용 파일에 데이터 저장
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(full_config, f, ensure_ascii=False, indent=2)
        logger.info("📝 fx_config.json에 최신 환율 정보 기록 완료.")

        is_github_action = os.getenv("GITHUB_ACTIONS") == "true"
        
        subprocess.run(["git", "config", "user.name", "Automated Bot"], check=True)
        subprocess.run(["git", "config", "user.email", "bot@example.com"], check=True)
        subprocess.run(["git", "add", "fx_config.json"], check=True) # 💡 fx_config.json만 명시하여 add
        subprocess.run(["git", "commit", "-m", f"🤖 Auto-update fx_config.json [Rate: {rate_info['applied_rate']}]"], check=True)
        
        if is_github_action:
            subprocess.run(["git", "push"], check=True)
        else:
            subprocess.run(["git", "push", "origin", "main"], check=True)
            
    except Exception as git_err:
        logger.error(f"❌ FX 전용 Git 동기화 중 에러 발생: {git_err}")


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
# 실행
# =============================================
def run_once():
    logger.info("=== 나무증권 환율 적정성 봇 (1회 실행) ===")

    try:
        is_recommended, rate_info, percentile, median_applied = analyze()

        if not rate_info or not isinstance(rate_info, dict) or len(rate_info) == 0:
            logger.error("환율 조회 실패 - API 응답 없음")
            return

        logger.info(f"적용환율: ₩{rate_info['applied_rate']:,.2f} | 하위 {percentile:.1f}%")
        
        send_discord_alert(rate_info, percentile, median_applied, is_recommended)
        
        # 🚀 전용 제이슨 저장 및 자동 푸시 엔진 작동
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
                    
                    # 🚀 추천 조건 도달 시 전용 제이슨 저장 및 자동 푸시
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


# =============================================
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "monitor":
        run_monitor()
    else:
        run_once()