import os
import datetime
import requests
import google.genai as genai
from google.genai import errors

# ====================== 설정 및 환경 변수 ======================
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 분석 키워드 (영문)
KEYWORDS = ["AI Infrastructure", "Semiconductor"]

# 영→한 번역 매핑
TRANSLATIONS = {
    "AI Infrastructure": "AI 인프라스트럭처",
    "Semiconductor": "반도체",
}

MAX_NEWS_PER_KEYWORD = 3

# ====================== 기능 함수 ======================

def send_discord_message(content: str):
    """디스코드 알림 전송"""
    if not DISCORD_WEBHOOK:
        print("⚠️ DISCORD_WEBHOOK이 설정되지 않았습니다.")
        return
        
    data = {"content": f"<@{DISCORD_USER_ID}>\n{content}"}
    try:
        response = requests.post(DISCORD_WEBHOOK, json=data, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"❌ Discord 전송 실패: {e}")

def fetch_news(keyword: str) -> list[dict]:
    """뉴스 데이터 수집 (예시 데이터 - 실제 API 연결 시 이 부분을 수정하세요)"""
    # 실제 구현 시에는 Google News RSS 등을 파싱하는 로직이 들어갑니다.
    return [
        {"title": f"{keyword} 시장 공급망 리포트", "source": "Reuters", "published": "2026-05-12", "summary": "AI 수요 폭증으로 인한 반도체 제조사의 CapEx 투자 확대가 지속되고 있습니다."},
        {"title": f"{keyword} 투자 심리 분석", "source": "Bloomberg", "published": "2026-05-12", "summary": "금리 변동에 따른 대규모 인프라 투자 지연 우려가 제기되었습니다."}
    ]

def analyze_with_gemini_batch(news_context: str) -> str:
    """Gemini 1.5 Flash를 이용한 배치 분석 (400 에러 해결 버전)"""
    if not news_context or not GEMINI_API_KEY:
        return "데이터 또는 API 키가 부족합니다."

    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
당신은 반도체 및 AI 산업 분석가입니다. 아래 뉴스 데이터를 바탕으로 'CapEx 투자 둔화' 징후가 있는지 중점적으로 분석하여 한국어로 요약해 주세요.

[뉴스 데이터]
{news_context}
"""

    try:
        # 모델명을 'models/gemini-1.5-flash'로 명시하여 400 NOT_FOUND 방지
        response = client.models.generate_content(
            model="models/gemini-1.5-flash", 
            contents=prompt
        )
        return response.text
    except errors.ClientError as e:
        # 사용량 초과(429/Resource Exhausted)나 다른 API 오류 처리
        if "RESOURCE_EXHAUSTED" in str(e):
            return "❌ API 사용량 초과 (무료 티어 제한). 나중에 다시 시도해 주세요."
        return f"❌ Gemini API 오류 발생: {e}"

def run_agent():
    """뉴스 에이전트 메인 실행 로직"""
    print(f"🚀 뉴스 에이전트 실행 시작: {datetime.datetime.now()}")
    
    combined_text = ""
    for keyword in KEYWORDS:
        news_list = fetch_news(keyword)
        if news_list:
            translated = TRANSLATIONS.get(keyword, keyword)
            combined_text += f"\n### 섹터: {translated}\n"
            for n in news_list[:MAX_NEWS_PER_KEYWORD]:
                combined_text += f"- {n['title']}\n  (요약: {n['summary']})\n"

    if combined_text:
        print("📝 Gemini 분석 중...")
        analysis = analyze_with_gemini_batch(combined_text)
        
        # 결과 전송
        final_msg = f"🔎 **오늘의 AI/반도체 뉴스 분석 보고서**\n\n{analysis}"
        send_discord_message(final_msg)
        print("✅ 분석 완료 및 전송 성공")
    else:
        print("⚠️ 수집된 뉴스가 없습니다.")

# ====================== 실행부 (격리 완료) ======================
if __name__ == "__main__":
    # 이 파일이 직접 실행될 때만 run_agent()가 호출됩니다.
    # trade_alert.py에서 import할 때는 실행되지 않습니다.
    run_agent()