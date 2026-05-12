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
                all_news_text += f"\n[분석 섹터: {kw}]\n" # 섹터 명칭도 한글 힌트를 섞음
                items = root.findall(".//item")
                for item in items[:3]:
                    all_news_text += f"- {item.find('title').text}\n"
        except: continue
    return all_news_text

def analyze_market_with_gemini(context: str):
    if not GEMINI_API_KEY: return None
    
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        # [핵심] 명령어를 마지막에 배치하고, 출력 형식을 강제로 지정함
        prompt = f"""
다음은 지난 24시간 동안 수집된 영문 뉴스 헤드라인입니다.

{context}

위 내용을 바탕으로 한국 투자자를 위한 '시장 분석 리포트'를 작성하세요.
반드시 아래 규칙을 엄수하세요:
1. 모든 내용은 '한국어'로만 작성할 것. (영문은 번역할 것)
2. 각 섹터별 핵심 내용을 요약할 것.
3. 말투는 "~입니다", "~함" 등으로 통일할 것.
4. 분석 결과에 영어 단어를 그대로 노출하지 말고 최대한 한글로 풀어서 쓸 것.
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
    
    if analysis_report:
        final_message = f"📢 **오늘의 글로벌 시장 분석 리포트**\n\n{analysis_report}"
    else:
        final_message = f"⚠️ **AI 분석 오류 (영문 원문 목록 대체)**\n\n{news_content}"

    mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    payload = {"content": f"{mention}{final_message}"}
    requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)

if __name__ == "__main__":
    main()