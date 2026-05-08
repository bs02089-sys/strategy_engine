"""
나무증권 원화 → 달러 환율 적정성 알림 봇
─────────────────────────────────────────────────────────────
"지금 1달러를 사는 게 싼가, 비싼가?"를 판단합니다.
최근 365일 롤링 데이터 기반 퍼센타일로 적정 환율 구간을 표시하고,
하위 30% 이하 진입 시 Discord로 알림을 전송합니다.

실행 방법:
  로컬 1회  : python fx_alert.py
  로컬 반복 : python fx_alert.py monitor
  자동 실행 : .github/workflows/fx_alert.yml 스케줄

환경변수 (로컬 .env 또는 GitHub Secrets):
  DISCORD_WEBHOOK        Discord Webhook URL
  DISCORD_USER_ID        멘션할 유저 ID (선택)
  NAMUH_SPREAD           나무증권 스프레드 % (기본 1.75)
  PREFER_RATE            우대율 % (기본 95)
  PERCENTILE_THRESHOLD   알림 기준 퍼센타일 (기본 30)
  CHECK_INTERVAL         로컬 반복 주기 초 (기본 300)
"""

import os
import sys
import requests
from datetime import datetime, timedelta

# ──────────────────────────────────────────
# 경로 고정 및 환경변수 로드
# ──────────────────────────────────────────
WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(WORKING_DIR)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # GitHub Actions 환경에서는 Secrets가 환경변수로 자동 주입됨

WEBHOOK_URL          = os.getenv("DISCORD_WEBHOOK", "")
DISCORD_USER_ID      = os.getenv("DISCORD_USER_ID", "")

# ──────────────────────────────────────────
# 설정값
# ──────────────────────────────────────────
NAMUH_SPREAD         = float(os.getenv("NAMUH_SPREAD",          "1.75"))
PREFER_RATE          = float(os.getenv("PREFER_RATE",           "95"))
PERCENTILE_THRESHOLD = float(os.getenv("PERCENTILE_THRESHOLD",  "30"))
CHECK_INTERVAL       = int(os.getenv("CHECK_INTERVAL",          "300"))

# Discord Embed 색상
COLOR_GREEN = 0x1D9E75
COLOR_AMBER = 0xBA7517
COLOR_RED   = 0xA32D2D


# ──────────────────────────────────────────
# 환율 데이터 조회
# ──────────────────────────────────────────
def get_current_rate() -> float | None:
    """현재 USD/KRW 환율 조회 (open.er-api.com 무료, API 키 불필요)"""
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") == "success":
            return data["rates"]["KRW"]
    except requests.RequestException as e:
        print(f"[오류] 현재 환율 조회 실패: {e}")
    return None


def get_historical_rates(days: int = 365) -> list[float]:
    """
    최근 N일의 USD/KRW 일별 환율 리스트 반환
    Frankfurter API 사용 (ECB 데이터 기반, 무료·API 키 불필요)
    주말/공휴일 제외 거래일 기준 약 250~260개 반환됨
    """
    end_date   = datetime.now().date()
    start_date = end_date - timedelta(days=days)
    url = f"https://api.frankfurter.app/{start_date}..{end_date}?from=USD&to=KRW"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data  = resp.json()
        rates = [v["KRW"] for v in data.get("rates", {}).values()]
        return sorted(rates)
    except requests.RequestException as e:
        print(f"[오류] 과거 환율 조회 실패: {e}")
    return []


# ──────────────────────────────────────────
# 적용 환율 계산 (1달러 기준)
# ──────────────────────────────────────────
def calc_applied_rate(base_rate: float) -> dict:
    """
    나무증권 우대율 적용 후 실제 1달러 매수 원화 계산
    적용 환율 = 기준율 × (1 + 스프레드 × (1 - 우대율/100) / 100)
    """
    effective_spread = NAMUH_SPREAD * (1 - PREFER_RATE / 100)
    applied_rate     = base_rate * (1 + effective_spread / 100)
    spread_cost      = applied_rate - base_rate   # 1달러당 스프레드 비용 (원)

    return {
        "base_rate"      : round(base_rate, 2),
        "applied_rate"   : round(applied_rate, 2),
        "spread_cost"    : round(spread_cost, 2),
        "effective_spread": round(effective_spread, 4),
    }


# ──────────────────────────────────────────
# 퍼센타일 계산
# ──────────────────────────────────────────
def calc_percentile(sorted_rates: list[float], current: float) -> float:
    """
    현재 환율이 과거 분포에서 몇 퍼센타일인지 반환 (0~100)
    낮을수록 원화 강세 → 달러 매수 유리
    예) 하위 20% → 최근 1년 중 80%의 날보다 저렴
    """
    n = len(sorted_rates)
    if n == 0:
        return 50.0
    below = sum(1 for r in sorted_rates if r > current)
    return round(below / n * 100, 1)


def get_percentile_stats(sorted_rates: list[float]) -> dict:
    """분포 요약 통계 및 구간별 기준 환율 반환"""
    n = len(sorted_rates)
    if n == 0:
        return {}

    def pct(p):
        idx = int(n * p / 100)
        return sorted_rates[min(idx, n - 1)]

    return {
        "count" : n,
        "min"   : sorted_rates[0],    # 1년 최저 (가장 유리)
        "p20"   : pct(20),            # 하위 20% 기준선
        "p30"   : pct(30),            # 하위 30% 기준선 (알림 기준)
        "p50"   : pct(50),            # 중간값
        "p70"   : pct(70),            # 상위 30% 기준선
        "max"   : sorted_rates[-1],   # 1년 최고 (가장 불리)
    }


def get_rating(percentile: float) -> tuple[str, str]:
    """퍼센타일 기반 환율 적정성 등급 및 코멘트 반환"""
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


# ──────────────────────────────────────────
# Discord 알림 전송
# ──────────────────────────────────────────
def send_discord_alert(
    rate_info: dict,
    percentile: float,
    stats: dict,
    is_recommended: bool,
) -> bool:
    """환율 적정성 분석 결과를 Discord Embed로 전송"""
    if not WEBHOOK_URL:
        print("[오류] DISCORD_WEBHOOK 환경변수가 설정되지 않았습니다.")
        return False

    now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    base     = rate_info["base_rate"]
    applied  = rate_info["applied_rate"]
    cost     = rate_info["spread_cost"]
    rating, comment = get_rating(percentile)

    if is_recommended:
        title       = f"✅ 달러 매수 적기! {rating}"
        description = (
            f"현재 환율이 최근 1년 중 **하위 {percentile:.1f}%** 수준입니다.\n"
            f"{comment}\n"
            f"나무증권 앱에서 환전을 진행하세요! (주간 95% 우대)"
        )
        color = COLOR_GREEN
    else:
        title       = f"📊 현재 환율 현황 — {rating}"
        description = (
            f"현재 환율은 최근 1년 중 **하위 {percentile:.1f}%** 수준입니다.\n"
            f"{comment}"
        )
        color = COLOR_AMBER

    # 퍼센타일 시각적 바 (저렴할수록 많이 채워짐)
    filled = max(0, min(10, int((100 - percentile) / 10)))
    bar    = "🟩" * filled + "⬜" * (10 - filled)

    fields = [
        {
            "name" : "💱 매매기준율 (시장)",
            "value": f"**₩{base:,.2f}** / $1",
            "inline": True,
        },
        {
            "name" : "🏦 나무증권 적용환율",
            "value": f"**₩{applied:,.2f}** / $1",
            "inline": True,
        },
        {
            "name" : "💸 1달러당 스프레드 비용",
            "value": f"₩{cost:,.2f}",
            "inline": True,
        },
        {
            "name" : "📉 적정성 (낮을수록 유리)",
            "value": f"하위 **{percentile:.1f}%** — {rating}\n{bar}",
            "inline": False,
        },
    ]

    if stats:
        fields.append({
            "name": "📆 최근 1년 매매기준율 구간",
            "value": (
                f"최저(최유리) ₩{stats['min']:,.0f}\n"
                f"하위 20%    ₩{stats['p20']:,.0f}  ←  매우 저렴 기준\n"
                f"하위 30%    ₩{stats['p30']:,.0f}  ←  알림 기준\n"
                f"중간값      ₩{stats['p50']:,.0f}\n"
                f"상위 30%    ₩{stats['p70']:,.0f}\n"
                f"최고(최불리) ₩{stats['max']:,.0f}\n"
                f"거래일 기준 {stats['count']}개 데이터"
            ),
            "inline": False,
        })

    mention = f"<@{DISCORD_USER_ID}> " if (DISCORD_USER_ID and is_recommended) else ""
    payload = {
        "username": "📉 나무증권 환전봇",
        "embeds"  : [{
            "title"      : title,
            "description": description,
            "color"      : color,
            "fields"     : fields,
            "timestamp"  : datetime.utcnow().isoformat(),
            "footer"     : {
                "text": (
                    f"나무증권 환전봇 • 우대율 {PREFER_RATE}% • "
                    f"스프레드 {NAMUH_SPREAD}% • 알림기준 하위 {PERCENTILE_THRESHOLD:.0f}%"
                )
            },
        }],
    }
    if mention:
        payload["content"] = mention

    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"[{now}] ✅ Discord 알림 전송 성공 (HTTP {resp.status_code})")
        return True
    except requests.RequestException as e:
        print(f"[{now}] ❌ Discord 알림 전송 실패: {e}")
        return False


def send_error_alert(message: str):
    """오류 발생 시 Discord에 오류 메시지 전송"""
    if not WEBHOOK_URL:
        return
    payload = {
        "username": "📉 나무증권 환전봇",
        "embeds"  : [{
            "title"      : "⚠️ 환율 조회 오류",
            "description": message,
            "color"      : COLOR_RED,
            "timestamp"  : datetime.utcnow().isoformat(),
        }],
    }
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10)
    except Exception:
        pass


# ──────────────────────────────────────────
# 핵심 분석 로직
# ──────────────────────────────────────────
def analyze() -> tuple[bool, dict, float, dict]:
    """
    현재 환율 조회 → 365일 롤링 퍼센타일 계산 → 매수 적정 여부 반환
    Returns: (is_recommended, rate_info, percentile, stats)
    """
    current = get_current_rate()
    if current is None:
        return False, {}, 0.0, {}

    historical     = get_historical_rates(365)
    percentile     = calc_percentile(historical, current)
    stats          = get_percentile_stats(historical)
    rate_info      = calc_applied_rate(current)
    is_recommended = percentile <= PERCENTILE_THRESHOLD

    return is_recommended, rate_info, percentile, stats


# ──────────────────────────────────────────
# 단발 실행 (GitHub Actions / 로컬 1회)
# ──────────────────────────────────────────
def run_once():
    """환율 1회 분석 후 결과 출력 및 Discord 알림"""
    print("=" * 55)
    print("  나무증권 환율 적정성 봇")
    print(f"  알림 기준 : 최근 1년 하위 {PERCENTILE_THRESHOLD:.0f}% 이하")
    print(f"  스프레드  : {NAMUH_SPREAD}%  (우대율 {PREFER_RATE}% 적용)")
    print("-" * 55)
    print("  과거 365일 데이터 로딩 중...")

    is_recommended, rate_info, percentile, stats = analyze()

    if not rate_info:
        print("  환율 조회에 실패했습니다.")
        send_error_alert("환율 데이터 조회에 실패했습니다.")
        sys.exit(1)

    rating, comment = get_rating(percentile)
    print(f"  매매기준율    : ₩{rate_info['base_rate']:,.2f} / $1")
    print(f"  나무증권 적용 : ₩{rate_info['applied_rate']:,.2f} / $1")
    print(f"  1달러당 비용  : ₩{rate_info['spread_cost']:,.2f}")
    print(f"  퍼센타일      : 하위 {percentile:.1f}%  {rating}")
    print(f"  코멘트        : {comment}")
    if stats:
        print(f"  1년 범위      : ₩{stats['min']:,.0f} ~ ₩{stats['max']:,.0f}"
              f"  (중간값 ₩{stats['p50']:,.0f})")
    print("-" * 55)

    send_discord_alert(rate_info, percentile, stats, is_recommended)


# ──────────────────────────────────────────
# 로컬 반복 모니터링
# ──────────────────────────────────────────
def run_monitor():
    """
    로컬에서 CHECK_INTERVAL 초마다 환율을 확인하고,
    하위 30% 이하 도달 시 Discord 알림 후 종료.
    GitHub Actions 사용 시 이 모드 대신 cron 스케줄을 권장.
    Ctrl+C 로 언제든 중단 가능.
    """
    import time

    print("=" * 55)
    print("  나무증권 환율 적정성 봇 — 로컬 모니터링")
    print(f"  알림 기준 : 최근 1년 하위 {PERCENTILE_THRESHOLD:.0f}% 이하")
    print(f"  확인 주기 : {CHECK_INTERVAL}초")
    print("  종료      : Ctrl+C")
    print("=" * 55)

    consecutive_errors = 0

    while True:
        now = datetime.now().strftime("%H:%M:%S")
        is_recommended, rate_info, percentile, stats = analyze()

        if not rate_info:
            consecutive_errors += 1
            print(f"[{now}] ⚠️ 환율 조회 실패 ({consecutive_errors}회 연속)")
            if consecutive_errors >= 3:
                send_error_alert(f"환율 조회 {consecutive_errors}회 연속 실패.")
                consecutive_errors = 0
        else:
            consecutive_errors = 0
            rating, _ = get_rating(percentile)
            print(
                f"[{now}] 기준 ₩{rate_info['base_rate']:,.2f} | "
                f"적용 ₩{rate_info['applied_rate']:,.2f} | "
                f"하위 {percentile:.1f}% {rating}"
            )

            if is_recommended:
                send_discord_alert(rate_info, percentile, stats, is_recommended=True)
                print("  매수 적기 알림 전송 완료 — 모니터링 종료.")
                break

        try:
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            print("\n  모니터링을 종료합니다.")
            break


# ──────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────
if __name__ == "__main__":
    # python fx_alert.py          → 1회 실행 (GitHub Actions 기본)
    # python fx_alert.py monitor  → 로컬 반복 모니터링
    if len(sys.argv) > 1 and sys.argv[1] == "monitor":
        run_monitor()
    else:
        run_once()