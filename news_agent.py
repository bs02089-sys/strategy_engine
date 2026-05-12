import os
import datetime
import requests
import google.genai as genai
from google.genai import errors

# --- 설정 및 환경 변수 ---
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 키워드 설정 (영문)
KEYWORDS = ["AI Infrastructure", "Semiconductor"]

# 영→한 번역 매핑
TRANSLATIONS = {
    "AI Infrastructure": "AI 인프라스트럭처",
    "Semiconductor": "반도체",
}

MAX_NEWS_PER_KEYWORD = 3

def send_discord_message(content: str):
    """디스코드 채널로 메시지를 전송합니다."""
    if not DISCORD_WEBHOOK:
        print("⚠️ DISCORD_WEBHOOK 환경변수가 설정되지 않았습니다.")
        return
        
    data = {"content": f"<@{DISCORD_USER_ID}>\n{content}"}
    try:
        response = requests.post(DISCORD_WEBHOOK, json=data)
        response.raise_for_status()
    except Exception as e:
        print(f"❌ Discord 전송 실패: {e}")

def fetch_news(keyword: str) -> list[dict]:
    """
    뉴스 데이터를 가져오는 함수 (현재는 예시 데이터)
    사용자님의 기존 뉴스 수집 로직이 있다면 이 부분을 교체하세요.
    """
    return [
        {"title": f"{keyword} 관련 시장 동향 뉴스 1", "source": "TechNews", "published": "2026-05-12", "summary": "최근 기업들의 설비 투자 추이에 대한 분석 보고서가 발표되었습니다."},
        {"title": f"{keyword} 글로벌 수요 변화 2", "source": "EconomyLog", "published": "2026-05-12", "summary": "공급망 병목 현상 완화와 함께 하반기 투자 계획이 수정되고 있습니다."}
    ]

def analyze_all_news_with_gemini_batch(all_news_context: str) -> str:
    """
    모든 뉴스 데이터를 한 번에 Gemini 1.5 Flash로 분석 (배치 방식)
    """
    if not all_news_context:
        return "분석할 뉴스 데이터가 없습니다."

    if not GEMINI_API_KEY:
        return "❌ GEMINI_API_KEY 환경변수가 설정되지 않았습니다."

    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
당신은 글로벌 IT 및 반도체 시장 분석가입니다. 
아래 제공된 뉴스 리스트를 바탕으로 'AI 인프라' 및 '반도체' 분야의 CapEx(설비투자) 흐름을 종합 분석해 주세요.

[분석 요청 사항]
1. 각 섹터별로 투자 확대 혹은 둔화(Slowdown)의 징후가 있는지 파악할 것.
2. 중요한 수치나 기업의 결정이 있다면 강조할 것.
3. 전체적인 시장 심리를 요약할 것.
4. 반드시 한국어로 답변할 것.

[뉴스 데이터]
{all_news_context}
"""

    try:
        # 모델을 1.5 Flash로 설정하여 무료 티어 안정성 확보
        response = client.models.generate_content(
            model="gemini-1.5-flash", 
            contents=prompt
        )
        return response.text
    except errors.ClientError as e:
        if "RESOURCE_EXHAUSTED" in str(e):
            return "❌ Gemini API 무료 티어 사용량 초과 (RPM/RPD 제한). 잠시 후 다시 시도해 주세요."
        return f"❌ Gemini API 오류 발생: {e}"
    except Exception as e:
        return f"❌ 예상치 못한 오류 발생: {e}"

def run_agent():
    print(f"🚀 실행 시작: {datetime.datetime.now()}")
    
    combined_news_text = ""
    
    # 1. 모든 키워드 뉴스를 먼저 수집하여 하나의 텍스트로 병합
    for keyword in KEYWORDS:
        news_list = fetch_news(keyword)
        if news_list:
            translated_name = TRANSLATIONS.get(keyword, keyword)
            combined_news_text += f"\n### 섹터: {translated_name}\n"
            for i, n in enumerate(news_list[:MAX_NEWS_PER_KEYWORD]):
                combined_news_text += f"[{i+1}] {n['title']}\n- 요약: {n['summary'][:150]}\n"
    
    # 2. 수집된 텍스트가 있으면 '단 한 번' API 호출
    if combined_news_text:
        print("📝 뉴스 분석 중 (Gemini 1.5 Flash 배치 모드)...")
        analysis_result = analyze_all_news_with_gemini_batch(combined_news_text)
        
        # 3. 최종 결과 전송
        final_message = f"🔎 **오늘의 시장 분석 보고서**\n{analysis_result}"
        send_discord_message(final_message)
        print("✅ 분석 완료 및 Discord 전송 시도 성공")
    else:
        print("⚠️ 수집된 뉴스가 없어 분석을 건너뜁니다.")

if __name__ == "__main__":
    run_agent()