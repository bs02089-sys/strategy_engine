import os
import datetime
import requests
import xml.etree.ElementTree as ET
import google.genai as genai

# 환경 변수 로드
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")
raw_keywords = os.getenv("NEWS_KEYWORDS", "Semiconductor, AI Infrastructure, NVIDIA, CapEx")
KEYWORDS = [k.strip() for k in raw_keywords.split(",")]

def fetch_latest_news():
    all_news_text = ""
    headers = {"User-Agent": "Mozilla/5.0"}
    for kw in KEYWORDS:
        rss_url = f"https://news.google.com/rss/search?q={kw}+when:1d&hl=en-US&gl=US&ceid=US:en"
        try:
            resp = requests.get(rss_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                all_news_text += f"\n**[{kw}]**\n"
                items = root.findall(".//item")
                for item in items[:3]:
                    all_news_text += f"- {item.find('title').text}\n"
        except: continue
    return all_news_text

def analyze_market_with_gemini(context: str):
    if not GEMINI_API_KEY: return None
    
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        # 한국어 요약 지침을 프롬프트 최상단에 배치
        prompt = f"""
        당신은 글로벌 테크 시장 분석가입니다. 아래 제공된 영문 뉴스 헤드라인들을 읽고, 
        한국 투자자들이 이해하기 쉽게 핵심 내용을 '한국어'로 요약해 주세요.

        [지침]
        1. 반드시 모든 답변은 한국어로 작성할 것.
        2. 전문 용어는 필요한 경우 한글과 병기할 것 (예: 설비투자(CapEx)).
        3. 각 섹터별 핵심 동향을 한 줄로 요약할 것.

        [뉴스 내용]
        {context}
        """
        
        response = client.models.generate_content(
            model="gemini-1.5-flash", 
            contents=prompt
        )
        return response.text
    except Exception as e:
        print(f"AI 분석 실패: {e}")
        return None

def main():
    news_content = fetch_latest_news()
    if not news_content.strip(): return

    analysis_report = analyze_market_with_gemini(news_content)
    
    # 메시지 제목 및 구조 한글화
    if analysis_report:
        final_message = f"📢 **오늘의 글로벌 시장 분석 리포트**\n\n{analysis_report}"
    else:
        # 분석 실패 시에도 한글 안내와 함께 뉴스 목록 전송
        final_message = f"⚠️ **AI 요약 일시 중단 (수집된 원문 목록)**\n\n{news_content}"

    mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    payload = {"content": f"{mention}{final_message}"}
    requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)

if __name__ == "__main__":
    main()