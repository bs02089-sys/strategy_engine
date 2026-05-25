"""
나무증권 원화 → 달러 환율 적정성 알림 봇
(서울외국환중개 실시간 크롤링 및 고정 스프레드 반영 버전 - GitHub Actions 최적화본)
"""

import os
from dataclasses import dataclass
import sys
import json
import logging
import time
import subprocess
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Tuple, TypedDict
import bisect
import statistics

import requests
from bs4 import BeautifulSoup
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


@dataclass
class Config:
    NAMUH_SPREAD: float = 10.0
    PREFER_RATE: float = 95.0
    PERCENTILE_THRESHOLD: float = 50.0
    CHECK_INTERVAL: int = 300
    CACHE_HOURS: int = 6
    ALERT_COOLDOWN_HOURS: int = 4  # 매수 적기 알림 발생 후 재알림 방지 쿨다운 시간

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
            "ALERT_COOLDOWN_HOURS": 4,
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

        # 환경변수 타입 안전 오버라이드 로직
        # GitHub Secrets / 로컬 환경변수 모두 문자열로 들어오므로 원래 타입으로 캐스팅
        for key in config_data.keys():
            env_val = os.getenv(key)
            if env_val is not None:
                orig_type = type(config_data[key])
                try:
                    if orig_type == bool:
                        config_data[key] = env_val.lower() in ("true", "1", "yes")
                    else:
                        config_data[key] = orig_type(env_val)
                except (ValueError, TypeError):
                    config_data[key] = env_val

        return cls(
            NAMUH_SPREAD=float(config_data["NAMUH_SPREAD"]),
            PREFER_RATE=float(config_data["PREFER_RATE"]),
            PERCENTILE_THRESHOLD=float(config_data["PERCENTILE_THRESHOLD"]),
            CHECK_INTERVAL=int(config_data["CHECK_INTERVAL"]),
            CACHE_HOURS=int(config_data["CACHE_HOURS"]),
            ALERT_COOLDOWN_HOURS=int(config_data["ALERT_COOLDOWN_HOURS"]),
            DISCORD_WEBHOOK=str(config_data["DISCORD_WEBHOOK"]),
            DISCORD_USER_ID=str(config_data["DISCORD_USER_ID"]),
        )


config = Config.load()
WEBHOOK_URL = config.DISCORD_WEBHOOK
DISCORD_USER_ID = config.DISCORD_USER_ID

COLOR_GREEN = 0x1D9E75
COLOR_AMBER = 0xBA7517


class RateInfo(TypedDict):
    base_rate: float
    applied_rate: float
    spread_cost: float
    effective_spread: float


CACHE_FILE = Path("historical_rates_cache.json")


def save_cache(rates: list[float]):
    try:
        data = {"rates": rates, "timestamp": datetime.now(tz=timezone.utc).isoformat()}
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"캐시 저장 실패: {e}")


def load_cache() -> list[float] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if datetime.now(tz=timezone.utc) - datetime.fromisoformat(data["timestamp"]) < timedelta(hours=config.CACHE_HOURS):
            return data["rates"]
    except Exception:
        pass
    return None


# =============================================
# 🎯 서울외국환중개 실시간 매매기준율 크롤링 엔진
# =============================================
def get_current_rate() -> float | None:
    """서울외국환중개(SMBS) 실시간 환율을 먼저 조회하고,
    성공 시 yfinance와 교차 검증하여 괴리율을 로깅합니다.
    SMBS 실패 시에만 yfinance를 백업으로 사용합니다.
    """
    smbs_rate = None

    url = "https://www.smbs.biz/ExRate/TodayExRate.jsp"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "euc-kr"

        soup = BeautifulSoup(resp.text, "html.parser")
        tables = soup.find_all("table")
        for table in tables:
            if "미국" not in table.get_text() and "USD" not in table.get_text():
                continue
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                cell_texts = [c.get_text(strip=True) for c in cells]

                # [FIX #2] 위치 기반 파싱: 불필요한 any() 구조 제거 후 단순 조건으로 정리
                if len(cell_texts) > 1 and ("미국" in cell_texts[0] or "USD" in cell_texts[0]):
                    # SMBS 테이블에서 매매기준율은 두 번째 열(Index 1)에 위치
                    target_txt = cell_texts[1].replace(",", "")
                    match = re.search(r"^\d+\.\d+$|^\d+$", target_txt)
                    if match:
                        val = float(match.group())
                        if 900 <= val <= 2000:
                            smbs_rate = val
                            break
            if smbs_rate:
                break

    except Exception as e:
        logger.error(f"❌ 서울외국환중개 실시간 환율 크롤링 실패: {e}")

    # [FIX #6] SMBS 성공 시에만 yfinance 교차 검증 호출 → 불필요한 선호출 제거
    if smbs_rate:
        yf_rate = get_backup_rate()
        if yf_rate:
            diff = smbs_rate - yf_rate
            pct_diff = (diff / smbs_rate) * 100
            logger.info("==================================================")
            logger.info("📊 [환율 데이터 소스 교차 검증 리포트]")
            logger.info(f" - 소스 A (서울외국환중개 당일 기준율): ₩{smbs_rate:,.2f}")
            logger.info(f" - 소스 B (yfinance 글로벌 실시간):   ₩{yf_rate:,.2f}")
            logger.info(f" - 두 소스 간 절대 괴리: ₩{diff:+.2f}원 (괴리율: {pct_diff:+.3f}%)")
            if abs(diff) >= 5.0:
                logger.warning("⚠️ [주의] 서울외국환과 yfinance 간의 괴리가 5원 이상으로 큽니다.")
            else:
                logger.info(" ✅ 두 소스의 괴리가 안정적 범위 안에 있습니다.")
            logger.info("==================================================")
        else:
            logger.warning("⚠️ yfinance 조회 실패로 인해 교차 검증 없이 서울외국환 데이터만 사용합니다.")
        return smbs_rate

    # SMBS 실패 시 yfinance 백업망으로 대체
    logger.warning("⚠️ 서울외국환 크롤링 실패 — 백업망(yfinance) 데이터로 대체합니다.")
    return get_backup_rate()


def get_backup_rate() -> float | None:
    """주말/시장 교대기 공백 방지 및 교차 검증용 실시간 환율 확보 (USDKRW=X)"""
    try:
        ticker = yf.Ticker("USDKRW=X")
        df = ticker.history(period="5d")
        if not df.empty:
            return float(df["Close"].dropna().iloc[-1])
    except Exception as e:
        logger.error(f"❌ 검증용 yfinance 실시간 조회 실패: {e}")
    return None


def get_historical_rates(days: int = 365) -> list[float]:
    """과거 1년치 통계용 환율 데이터를 빌드합니다. 캐시가 유효하면 캐시를 반환합니다."""
    cached = load_cache()
    if cached:
        return cached

    try:
        logger.info(f"통계 분석을 위해 과거 {days}일간의 환율 데이터를 빌드합니다...")
        ticker = yf.Ticker("USDKRW=X")
        df = ticker.history(period="1y")
        if df.empty:
            raise ValueError("통계 데이터가 비어 있습니다.")
        rates = [float(val) for val in df["Close"].dropna().tolist()]
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
    """나무증권 우대 환율 계산: 기준율 + (스프레드 × (1 - 우대율))"""
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
    """현재 환율이 과거 1년 데이터 중 하위 몇 %에 해당하는지 반환합니다.
    [FIX #1] 현재값을 통계 모집단에 혼입하지 않아 백분위 왜곡을 방지합니다.
    """
    if not sorted_rates:
        return 50.0
    idx = bisect.bisect_right(sorted_rates, current)
    return round((idx / len(sorted_rates)) * 100, 1)


def get_median_applied_rate(sorted_base_rates: list[float]) -> float:
    if not sorted_base_rates:
        return 0.0
    median_base = statistics.median(sorted_base_rates)
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
# Discord 알림 및 Git 파일 기입 로직
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
                {"name": "💱 서울외국환 기준율", "value": f"**₩{rate_info['base_rate']:,.2f}**", "inline": True},
                {"name": "🏦 나무증권 적용환율", "value": f"**₩{current_applied:,.2f}**",        "inline": True},
                {"name": "💸 스프레드 수수료",   "value": f"₩{rate_info['spread_cost']:,.2f}",   "inline": True},
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
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
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
    # [FIX #3] 들여쓰기 오류 수정 — 함수 본문을 표준 4칸으로 정렬
    config_path = Path("config.json")
    if config_path.exists():
        try:
            full_config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"❌ config.json 읽기 실패: {e}")
            full_config = {}
    else:
        full_config = {}

    if "exchange_status" not in full_config:
        full_config["exchange_status"] = {}

    # [FIX #4] timezone.utc 통일 — naive datetime 혼용 방지
    full_config["exchange_status"]["LAST_CHECK_DATE"] = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    full_config["exchange_status"]["CURRENT_APPLIED_RATE"] = rate_info["applied_rate"]
    full_config["exchange_status"]["CURRENT_PERCENTILE"] = percentile

    config_path.write_text(json.dumps(full_config, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("📝 config.json exchange_status 로컬 기입 완료.")

    # [하이브리드 환경 최적화 제어]
    # GitHub Actions 환경이면 파이썬 내부 푸시를 생략하고 워크플로우(.yml)에 위임
    # → 중복 푸시 충돌 방지
    if os.getenv("GITHUB_ACTIONS") == "true":
        logger.info("ℹ️ GitHub Actions 환경 감지: 파이썬 내부 푸시를 생략하고 워크플로우에 위임합니다.")
        return

    # 로컬 PowerShell에서 수동 실행 시에만 아래 Git 동기화 수행
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", str(config_path)],
            capture_output=True, text=True
        )
        if not status.stdout.strip():
            logger.info("ℹ️ 로컬 변동 사항이 없어 Git 커밋을 생략합니다.")
            return

        subprocess.run(["git", "config", "--local", "user.name",  "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "--local", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", str(config_path)], check=True)
        subprocess.run(["git", "commit", "-m", f"🤖 Local Auto-update config.json [Rate: {rate_info['applied_rate']}]"], check=True)
        subprocess.run(["git", "push", "origin", "main"], check=True)
        logger.info("🚀 로컬 PowerShell 기준 Git Push 성공.")
    except Exception as git_err:
        logger.error(f"❌ 로컬 Git 동기화 중 에러 발생: {git_err}")


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
    """GitHub Actions 스케줄 실행용 — 1회 조회 후 종료합니다.
    Git push는 yml 워크플로우가 전담합니다.
    이 함수는 config.json 로컬 기입(exchange_status 갱신)까지만 수행합니다.
    """
    logger.info("=== 나무증권 환율 봇 (서울외국환 실시간 모드) — 단발 실행 ===")
    try:
        is_recommended, rate_info, percentile, median_applied = analyze()
        if not rate_info:
            logger.error("환율 조회 실패 — 서울외국환중개 및 yfinance 모두 응답 없음")
            return

        logger.info(f"실시간 적용환율: ₩{rate_info['applied_rate']:,.2f} | 하위 {percentile:.1f}%")
        send_discord_alert(rate_info, percentile, median_applied, is_recommended)

        # config.json 로컬 기입만 수행 (Git push는 yml 워크플로우 마지막 스텝이 담당)
        config_path = Path("config.json")
        try:
            full_config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        except Exception:
            full_config = {}
        full_config.setdefault("exchange_status", {})
        full_config["exchange_status"]["LAST_CHECK_DATE"] = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        full_config["exchange_status"]["CURRENT_APPLIED_RATE"] = rate_info["applied_rate"]
        full_config["exchange_status"]["CURRENT_PERCENTILE"] = percentile
        config_path.write_text(json.dumps(full_config, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("📝 config.json exchange_status 로컬 기입 완료. Git push는 yml이 처리합니다.")

    except Exception as e:
        logger.error(f"run_once 오류 발생: {e}")


def run_monitor():
    """로컬 PowerShell 실행용 — 쿨다운 제어식 무한 모니터링 모드입니다."""
    logger.info("=== 나무증권 환율 봇 — 장중 실시간 쿨다운 제어 모드 가동 ===")
    consecutive_errors = 0
    ERROR_THRESHOLD    = 5  # 연속 장애 임계값: 초과 시 Discord SOS 알림 발송

    # [FIX #4] timezone.utc aware datetime으로 통일 — 향후 파일 저장/로드 확장 시 충돌 방지
    last_alert_time: datetime | None = None

    while True:
        try:
            is_recommended, rate_info, percentile, median_applied = analyze()

            # 1. 크롤링 실패 처리 구역
            if not rate_info:
                consecutive_errors += 1
                logger.warning(f"⚠️ 환율 조회 실패 ({consecutive_errors}/{ERROR_THRESHOLD}회 연속)")

                if consecutive_errors == ERROR_THRESHOLD:
                    msg = (f"🚨 [시스템 알림] 환율 크롤링 엔진에 {ERROR_THRESHOLD}회 연속 장애가 "
                           "발생했습니다. 확인이 필요합니다.")
                    logger.error(msg)
                    if WEBHOOK_URL:
                        try:
                            requests.post(
                                WEBHOOK_URL,
                                json={"username": "⚠️ 환율봇 관리자", "content": msg},
                                timeout=5
                            )
                        except Exception:
                            pass

                time.sleep(60)  # 에러 상태일 때는 1분 단위로 재시도
                continue

            # 정상 수신 시 연속 에러 카운터 리셋
            consecutive_errors = 0
            logger.info(f"정상 모니터링 중 | 적용환율: ₩{rate_info['applied_rate']:,.2f} | 하위 {percentile:.1f}%")

            # 2. 매수 적기 알림 및 쿨다운 관리 구역
            if is_recommended:
                now            = datetime.now(tz=timezone.utc)   # [FIX #4] UTC aware
                cooldown_delta = timedelta(hours=config.ALERT_COOLDOWN_HOURS)

                if last_alert_time is None or (now - last_alert_time) >= cooldown_delta:
                    logger.info("🎯 매수 기준선 도달! 알림 발송 및 쿨다운 타임라인을 활성화합니다.")
                    send_discord_alert(rate_info, percentile, median_applied, True)
                    sync_config_to_git(rate_info, percentile)
                    last_alert_time = now
                else:
                    remaining_mins = int((cooldown_delta - (now - last_alert_time)).total_seconds() / 60)
                    logger.info(f"⏳ 매수 적기 구간이나 쿨다운 적용 중입니다. ({remaining_mins}분 후 제한 해제)")

            time.sleep(config.CHECK_INTERVAL)

        except KeyboardInterrupt:
            logger.info("👋 사용자에 의해 모니터링이 안전하게 종료되었습니다.")
            break
        except Exception as e:
            logger.error(f"⚠️ 시스템 런타임 오류 발생: {e}")
            time.sleep(60)


if __name__ == "__main__":
    # [FIX #5] GitHub Actions: 인자 없이 실행 → run_once() (단발 종료)
    #          로컬 PowerShell: python fx_alert.py monitor → run_monitor() (무한 루프)
    if len(sys.argv) > 1 and sys.argv[1] == "monitor":
        run_monitor()
    else:
        run_once()
