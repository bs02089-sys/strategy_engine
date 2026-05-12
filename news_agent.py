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
                all_news_text += f"\n[섹터: {kw}]\n"
                items = root.findall(".//item")
                for item in items[:3]:
                    all_news_text += f"- {item.find('title').text}\n"
        except: continue
    return all_news_text

def analyze_market_with_gemini(context: str):
    if not GEMINI_API_KEY: return None
    
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        # [핵심 변경] 모델명에서 'models/'를 제거하고, 가장 안정적인 최신 명칭 사용
        # 만약 1.5-flash가 계속 404가 나면 'gemini-1.5-flash-002'로 시도해볼 수 있습니다.
        target_model = "gemini-1.5-flash" 
        
        response = client.models.generate_content(
            model=target_model,
            contents=[
                {
                    "role": "user",
                    "parts": [{"text": f"지시: 아래 영문 뉴스 리스트를 한국인 투자자를 위해 한국어로 요약해줘. 절대로 영어를 그대로 출력하지 말고 번역해서 출력해.\n\n내용:\n{context}"}]
                }
            ]
        )
        return response.text
    except Exception as e:
        print(f"AI 분석 실패 상세: {e}")
        return None

def main():
    news_content = fetch_latest_news()
    if not news_content.strip(): return

    analysis_report = analyze_market_with_gemini(news_content)
    
    if analysis_report:
        final_message = f"📢 **오늘의 글로벌 시장 분석 리포트**\n\n{analysis_report}"
    else:
        # 에러 시에는 원문이라도 확실히 나오게 함
        final_message = f"⚠️ **AI 요약 실패 (원문 목록)**\n\n{news_content}"

    mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    payload = {"content": f"{mention}{final_message}"}
    requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)

if __name__ == "__main__":
    main()