import os
import datetime
import requests
import google.genai as genai
from google.genai import errors

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 키워드는 영문으로 입력
KEYWORDS = ["AI Infrastructure", "Semiconductor"]

# 영→한 번역 매핑 확장 가능
TRANSLATIONS = {
    "AI Infrastructure": "AI 인프라스트럭처",
    "Semiconductor": "반도체",
    # 필요하면 여기에 계속 추가 가능
}

MAX_NEWS_PER_KEYWORD = 3

def send_discord_message(content: str):
    data = {"content": f"<@{DISCORD_USER_ID}>\n{content}"}
    try:
        requests.post(DISCORD_WEBHOOK, json=data)
    except Exception as e:
        print(f"❌ Discord 전송 실패: {e}")

def analyze_with_gemini(keyword: str, news_list: list[dict]) -> str:
    if not news_list:
        return None

    client = genai.Client(api_key=GEMINI_API_KEY)
    news_text = "\n\n".join([
        f"[{i+1}] {n['title']}\n출처: {n['source']} | {n['published']}\n내용: {n['summary'][:200]}"
        for i, n in enumerate(news_list[:MAX_NEWS_PER_KEYWORD])
    ])

    prompt = f"""Keyword: {keyword}
News summary:
{news_text}

Check if there are signs of CapEx slowdown or investment reduction.
"""

    try:
        response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return response.text
    except errors.ClientError as e:
        if "RESOURCE_EXHAUSTED" in str(e):
            titles = "\n".join([f"- {n['title']}" for n in news_list[:MAX_NEWS_PER_KEYWORD]])
            return f"❌ Gemini API 무료 티어 사용량 초과 – 분석 생략\n대신 최근 뉴스 제목:\n{titles}"
        return f"❌ Gemini API 오류 발생: {e}"

def fetch_news(keyword: str) -> list[dict]:
    return [
        {"title": f"{keyword} related news 1", "source": "NewsSite", "published": "2026-05-11", "summary": "Example summary text."},
        {"title": f"{keyword} related news 2", "source": "NewsSite", "published": "2026-05-11", "summary": "Example summary text."}
    ]

def run_agent():
    print(f"🚀 실행 시작: {datetime.datetime.now()}")
    for keyword in KEYWORDS:
        news = fetch_news(keyword)
        analysis = analyze_with_gemini(keyword, news)
        if analysis:
            translated_keyword = TRANSLATIONS.get(keyword, keyword)
            send_discord_message(f"🔎 {translated_keyword} 분석 결과:\n{analysis}")

if __name__ == "__main__":
    run_agent()