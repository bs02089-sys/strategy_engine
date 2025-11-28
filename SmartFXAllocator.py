# SmartFXAllocator
# 환율 변동성 기반 정기 적립식 (전일 기준환율 자동 반영, 알림/출력 단일화)
# 작성자: Copilot

import os
import requests
import datetime
import calendar
import yfinance as yf
import numpy as np
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import subprocess

# ==============================
# 환경 설정
# ==============================
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
regular_invest = 500000  # 정기 적립식 기본 금액

# ==============================
# 유틸 함수
# ==============================
def send_discord(msg: str):
    """Discord 웹훅으로 메시지 전송"""
    if not WEBHOOK_URL:
        print("웹훅 없음 → 로컬 테스트:", msg)
        return
    try:
        resp = requests.post(WEBHOOK_URL, json={"content": msg}, timeout=10)
        if resp.status_code == 204:
            print("✅ Discord 알림 전송 성공")
        else:
            print(f"⚠️ Discord 응답 오류: {resp.status_code}, {resp.text}")
    except Exception as e:
        print(f"전송 실패: {e}")

def get_third_thursday(year: int, month: int) -> datetime.date:
    """해당 연월의 셋째 주 목요일 날짜 반환"""
    last_day = calendar.monthrange(year, month)[1]
    thursdays = [d for d in range(1, last_day + 1)
                 if datetime.date(year, month, d).weekday() == 3]
    thursdays.sort()
    return datetime.date(year, month, thursdays[2])

def is_third_thursday(date: datetime.date) -> bool:
    """오늘이 셋째 주 목요일인지 확인"""
    return date == get_third_thursday(date.year, date.month)

def get_rates():
    """전일 기준환율과 최근 6개월 변동성 기준점 계산"""
    current_data = yf.download("USDKRW=X", period="2d", interval="1d")
    current_rate = round(current_data['Close'].iloc[-1].item(), 2)

    hist_data = yf.download("USDKRW=X", period="6mo", interval="1d", auto_adjust=False)
    hist_data['log_return'] = np.log(hist_data['Close'] / hist_data['Close'].shift(1))
    sigma = hist_data['log_return'].std()

    thresholds = [
        round(current_rate * (1 - 0.5 * sigma), 2),
        round(current_rate * (1 - 1.0 * sigma), 2),
        round(current_rate * (1 - 1.5 * sigma), 2)
    ]
    return current_rate, thresholds

def investment_plan(date: datetime.date, rate: float, thresholds: list):
    """투자 계획 계산"""
    plan = {}
    if is_third_thursday(date):
        plan["regular"] = regular_invest
        plan["note"] = "정기 적립식 매수일"

        extra_amount = 0
        extra_notes = []
        for i, t in enumerate(thresholds, start=1):
            if rate <= t:
                extra_amount += 100000
                extra_notes.append(f"{i}차 기준 충족 ({t}원 이하)")
        plan["extra"] = extra_amount
        plan["extra_notes"] = extra_notes
        plan["total"] = plan["regular"] + plan["extra"]
    else:
        plan["regular"] = 0
        plan["extra"] = 0
        plan["extra_notes"] = []
        plan["total"] = 0
        plan["note"] = "정기 적립식 아님"
    return plan

def write_log(message: str):
    """로그 파일 기록"""
    with open("log.txt", "a", encoding="utf-8") as f:
        f.write(message + "\n")

def git_push(commit_message="SmartFXAllocator update"):
    """GitHub 자동 푸시"""
    try:
        subprocess.run(["git", "add", "log.txt"], check=True)
        subprocess.run(["git", "commit", "-m", commit_message], check=True)
        subprocess.run(["git", "push"], check=True)
        print("✅ GitHub 자동 푸시 완료")
    except subprocess.CalledProcessError as e:
        print("❌ GitHub 푸시 실패:", e)

# ==============================
# 메인 로직
# ==============================
def main():
    kr_time = datetime.datetime.now(ZoneInfo("Asia/Seoul"))
    today = kr_time.date()
    now_str = kr_time.strftime("%Y-%m-%d %H:%M")

    current_rate, thresholds = get_rates()
    plan_today = investment_plan(today, current_rate, thresholds)

    next_month = today.month + 1 if today.month < 12 else 1
    next_year = today.year if today.month < 12 else today.year + 1
    next_third_thursday = get_third_thursday(next_year, next_month)

    # 콘솔 출력
    print("========================================")
    print("💱 [SmartFXAllocator]")
    print(f"📅 오늘 날짜: {today}")
    print(f"💵 전일 기준환율: {current_rate}")
    print(f"📝 오늘 매수 여부: {plan_today['note']}")
    print(f"💰 오늘 매수 금액: {plan_today['regular']}원")
    print(f"➕ 추가 증액: {plan_today['extra']}원")
    if plan_today["extra_notes"]:
        print("⚡ 충족된 기준:")
        for note in plan_today["extra_notes"]:
            print(f"- {note}")
    else:
        print("⚠️ 오늘 추가 증액 없음")
    print(f"💳 총 매수 금액: {plan_today['total']}원")
    print(f"📅 다음 매수일: {next_third_thursday}")
    print("========================================\n")

    # Discord 알림
    if plan_today["total"] > 0:
        alert_message = (
            f"📢 SmartFXAllocator 알림\n"
            f"📅 {now_str} (KST)\n"
            f"💵 전일 기준환율: {current_rate}\n"
            f"💰 총 매수 금액: {plan_today['total']}원\n"
            f"📝 {plan_today['note']}"
        )
    else:
        alert_message = (
            f"📢 SmartFXAllocator 알림\n"
            f"📅 {now_str} (KST)\n"
            f"💵 전일 기준환율: {current_rate}\n"
            f"⚠️ 오늘은 매수 신호 없음"
        )

    send_discord(alert_message)
    write_log(alert_message)
    git_push(f"SmartFXAllocator update {today}")  # ✅ 자동 푸시 실행

    # 월간 Ping
    if today.day == 1:
        send_discord(f"✅ Monthly Ping: 시스템 정상 작동 중 ({now_str})")

# ==============================
# 실행
# ==============================
if __name__ == "__main__":
    main()
