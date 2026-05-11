import os
import datetime
import requests
import google.genai as genai
from google.genai import errors

# 환경 변수에서 API 키 불러오기
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 키워드와 뉴스 개수 제한
KEYWORDS = ["AI", "Semiconductor", "ETF", "Stock"]
MAX_NEWS_PER_KEYWORD = 3   # 무료 티어 고려 → 입력량 최소화

# Discord 메시지 전송 함수
def send_discord_message(content: str):
    data = {
        "content": f"<@{DISCORD_USER_ID}>\n{content}"
    }
    try:
        requests.post(DISCORD_WEBHOOK, json=data)
    except Exception as e:
        print(f"❌ Discord 전송 실패: {e}")

# Gemini 분석 함수 (에러 핸들링 + fallback)
def analyze_with_gemini(keyword: str, news_list: list[dict]) -> str:
    if not news_list:
        return None

    client = genai.Client(api_key=GEMINI_API_KEY)

    # 뉴스 요약 텍스트 (200자 제한 → 토큰 절약)
    news_text = "\n\n".join([
        f"[{i+1}] {n['title']}\n출처: {n['source']} | {n['published']}\n내용: {n['summary'][:200]}"
        for i, n in enumerate(news_list[:MAX_NEWS_PER_KEYWORD])
    ])

    prompt = f"""키워드: {keyword}
뉴스 요약:
{news_text}

CapEx 둔화, 투자 감소 여부만 간단히 체크하세요.
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        return response.text
    except errors.ClientError as e:
        if "RESOURCE_EXHAUSTED" in str(e):
            # Fallback: 뉴스 제목만 전달
            titles = "\n".join([f"- {n['title']}" for n in news_list[:MAX_NEWS_PER_KEYWORD]])
            return f"❌ Gemini API 무료 티어 사용량 초과 – 분석 생략\n대신 최근 뉴스 제목:\n{titles}"
        return f"❌ Gemini API 오류 발생: {e}"

# 뉴스 수집 (예시용 더미 함수)
def fetch_news(keyword: str) -> list[dict]:
    # 실제 구현에서는 API 호출 또는 RSS 파싱
    return [
        {"title": f"{keyword} 관련 뉴스 1", "source": "NewsSite", "published": "2026-05-11", "summary": "내용 요약 예시입니다."},
        {"title": f"{keyword} 관련 뉴스 2", "source": "NewsSite", "published": "2026-05-11", "summary": "내용 요약 예시입니다."}
    ]

# 전체 실행 함수
def run_agent():
    print(f"🚀 실행 시작: {datetime.datetime.now()}")
    for keyword in KEYWORDS:
        news = fetch_news(keyword)
        analysis = analyze_with_gemini(keyword, news)
        if analysis:
            send_discord_message(f"🔎 {keyword} 분석 결과:\n{analysis}")

if __name__ == "__main__":
    run_agent()