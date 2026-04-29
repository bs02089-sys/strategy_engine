# SmartFXAllocator
# 환율 변동성 기반 정기 적립식 + 추가 매수 전략 코드
# 작성자: Copilot

import datetime
import calendar
import pandas as pd
import yfinance as yf
import numpy as np
import requests
import subprocess

# ===== 기본 설정 =====
regular_invest = 330000        # 정기 적립식 금액
extra_invest_unit = 167000     # 추가 매수 단위 금액
current_rate = 1469.07         # 오늘 환율 (예시)
discord_webhook_url = "YOUR_DISCORD_WEBHOOK_URL"  # 디스코드 웹훅 URL

# ===== 날짜 관련 함수 =====
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

# ===== 변동성 계산 =====
def get_volatility_thresholds(current_rate: float):
    """최근 6개월 환율 데이터를 기반으로 변동성 기준점 계산"""
    data = yf.download("USDKRW=X", period="6mo", interval="1d", auto_adjust=False)
    data['log_return'] = np.log(data['Close'] / data['Close'].shift(1))
    sigma = data['log_return'].std()

    thresholds = [
        round(current_rate * (1 - 0.5 * sigma), 2),
        round(current_rate * (1 - 1.0 * sigma), 2),
        round(current_rate * (1 - 1.5 * sigma), 2)
    ]
    return thresholds

# ===== 투자 계획 =====
def investment_plan(date: datetime.date, rate: float, thresholds: list):
    plan = {}
    if is_third_thursday(date):
        plan["regular"] = regular_invest
        plan["note"] = "정기 적립식 매수일"
    else:
        plan["regular"] = 0
        plan["note"] = "정기 적립식 아님"

    extra = 0
    extra_notes = []
    for i, t in enumerate(thresholds, start=1):
        if rate <= t:
            extra += extra_invest_unit
            extra_notes.append(f"{i}차 기준 충족 ({t}원 이하)")
    plan["extra"] = extra
    plan["extra_notes"] = extra_notes
    plan["total"] = plan["regular"] + plan["extra"]
    return plan

# ===== 디스코드 알림 =====
def send_discord_alert(message: str):
    if discord_webhook_url.startswith("http"):
        data = {"content": message}
        requests.post(discord_webhook_url, json=data)

# ===== 깃허브 자동 푸시 =====
def git_push(commit_message="Auto update SmartFXAllocator log"):
    try:
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-m", commit_message], check=True)
        subprocess.run(["git", "push"], check=True)
        print("✅ GitHub 자동 푸시 완료")
    except subprocess.CalledProcessError as e:
        print("❌ GitHub 푸시 실패:", e)

# ===== 실행 =====
today = datetime.date.today()
thresholds = get_volatility_thresholds(current_rate)
plan_today = investment_plan(today, current_rate, thresholds)

next_month = today.month + 1 if today.month < 12 else 1
next_year = today.year if today.month < 12 else today.year + 1
next_third_thursday = get_third_thursday(next_year, next_month)

# 📋 체크리스트 출력
print("\n=== SmartFXAllocator 체크리스트 ===")
print(f"오늘 날짜: {today}")
print(f"현재 환율: {current_rate}")

print("\n[정기 적립식]")
print(f"- 매월 셋째 주 목요일: {regular_invest}원 매수")
print(f"- 오늘 매수 여부: {plan_today['note']}")

print("\n[추가 매수 기준점]")
for i, t in enumerate(thresholds, start=1):
    print(f"- {i}차 기준: {t}원 이하 → {extra_invest_unit}원 매수")

if plan_today["extra_notes"]:
    print("\n오늘 충족된 추가 매수 기준:")
    for note in plan_today["extra_notes"]:
        print(f"- {note}")
else:
    print("\n오늘 추가 매수 기준 충족 없음")

print(f"\n총 매수 금액: {plan_today['total']}원")

print("\n[다음 매수일]")
print(f"- 다음 달 셋째 주 목요일: {next_third_thursday} (정기 적립식 매수일)")
print("===================================")

# 디스코드 알림 전송
alert_message = f"📢 SmartFXAllocator 알림: {today} | 총 매수 금액 {plan_today['total']}원"
send_discord_alert(alert_message)

# 깃허브 자동 푸시
git_push(f"SmartFXAllocator update {today}")