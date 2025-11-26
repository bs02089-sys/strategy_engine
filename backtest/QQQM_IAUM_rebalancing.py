import yfinance as yf
import json
import os
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

# 뉴욕 시간 기준
ny_time = datetime.now(ZoneInfo("America/New_York"))
today_str = ny_time.strftime("%Y-%m-%d")

print(f"\n{'='*65}")
print(f" LazyRich QQQM/IAUM 자동 리밸런싱 엔진 - {today_str}")
print(f"{'='*65}")

# 데이터 다운로드 (최근 2년)
tickers = ["QQQM", "IAUM"]
data = yf.download(tickers, period="730d", auto_adjust=True, progress=False)["Close"]
returns = data.pct_change().dropna()

# 실시간 가격
def get_live_price(symbol):
    info = yf.Ticker(symbol).info
    return info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose") or data[symbol].iloc[-1]

qqqm_price = get_live_price("QQQM")
iaum_price = get_live_price("IAUM")

# 체제 판단 로직 (자유롭게 수정 가능)
# 예시: QQQM이 최근 3개월 수익률 12% 이상 + 변동성 28% 이하면 공격적으로
vol_6m = returns.tail(126).std() * (252 ** 0.5)
mom_3m = (data.tail(63).iloc[-1] / data.tail(63).iloc[0] - 1) * (252 / 63)

if mom_3m['QQQM'] >= 0.12 and vol_6m['QQQM'] <= 0.28:
    regime = "Strong Bull"
    qqqm_weight = 0.90
elif mom_3m['QQQM'] >= 0.05:
    regime = "Moderate Bull"
    qqqm_weight = 0.80
elif mom_3m['QQQM'] >= -0.08:
    regime = "Neutral"
    qqqm_weight = 0.70
else:
    regime = "Bear"
    qqqm_weight = 0.55

iaum_weight = round(1 - qqqm_weight, 3)

# 구글 시트가 딱 먹는 완벽한 JSON 구조
final_json = {
    "date": today_str,
    "regime": regime,
    "target_weights": {
        "QQQM": qqqm_weight,
        "IAUM": iaum_weight
    },
    "prices": {
        "QQQM": round(qqqm_price, 2),
        "IAUM": round(iaum_price, 2)
    },
    "note": "LazyRich 자동 리밸런싱 완료 - 구글 시트 전용"
}

# 로컬 저장
with open("target_weights.json", "w", encoding="utf-8") as f:
    json.dump(final_json, f, indent=2, ensure_ascii=False)

print(f"\n체제 판단 → {regime}")
print(f"QQQM 3개월 모멘텀: {mom_3m['QQQM']:.1%} | 6개월 변동성: {vol_6m['QQQM']:.1%}")
print(f"실시간 가격 → QQQM: ${qqqm_price:,.2f} | IAUM: ${iaum_price:,.2f}")
print(f"목표 비중 → QQQM {qqqm_weight*100:.0f}% : IAUM {iaum_weight*100:.0f}%")

# Public 레포 자동 푸시
def push_to_public():
    token = os.getenv("DATA_REPO_TOKEN")  
    if not token:
        print("\nDATA_REPO_TOKEN 없음 → 수동 업로드 필요")
        return
    try:
        repo_url = f"https://x-access-token:{token}@github.com/bs02089-sys/lazyrich-data.git"
        temp_dir = "/tmp/lazyrich-data"
        subprocess.run(["rm", "-rf", temp_dir], check=False)
        subprocess.run(["git", "clone", "--depth", "1", repo_url, temp_dir], check=True)
        with open(f"{temp_dir}/target_weights.json", "w", encoding="utf-8") as f:
            json.dump(final_json, f, indent=2, ensure_ascii=False)
        subprocess.run(["git", "-C", temp_dir, "config", "user.name", "LazyRich Bot"], check=True)
        subprocess.run(["git", "-C", temp_dir, "config", "user.email", "lazy@rich.com"], check=True)
        subprocess.run(["git", "-C", temp_dir, "add", "target_weights.json"], check=True)
        commit_result = subprocess.run(["git", "-C", temp_dir, "commit", "-m", f"Auto {today_str} {regime}"], 
                                     capture_output=True, text=True)
        if "nothing to commit" in commit_result.stdout:
            print("변경사항 없음")
        else:
            subprocess.run(["git", "-C", temp_dir, "push", "--quiet"], check=True)
            print(f"\nPublic 레포 업데이트 성공 → {regime} 비중 반영!")
    except Exception as e:
        print("푸시 실패:", e)

push_to_public()

print(f"\n{'='*65}")
print("LazyRich 완전 자동화 완료")
print("이제 진짜 손 안 대고 영원히 굴러갑니다")
print(f"{'='*65}")