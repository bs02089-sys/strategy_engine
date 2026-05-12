import os
import datetime
import requests
import xml.etree.ElementTree as ET
import google.genai as genai
from google.genai import errors

# ====================== 환경 변수 설정 ======================
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 분석 키워드
KEYWORDS = ["AI Infrastructure", "Semiconductor"]
MAX_NEWS_PER_KEYWORD = 3

def fetch_google_news(keyword: str) -> list[dict]:
    """구글 뉴스 RSS를 통해 실제 최신 뉴스를 가져옵니다."""
    news_list = []
    # 구글 뉴스 RSS URL (영문 검색 결과)
    url = f"https://news.google.com/rss/search?q={keyword}+when:1d&hl=en-US&gl=US&ceid=US:en"
    
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            root = ET.fromstring(response.text)
            for item in root.findall(".//item")[:MAX_NEWS_PER_KEYWORD]:
                news_list.append({
                    "title": item.find("title").text,
                    "link": item.find("link").text,
                    "published": item.find("pubDate").text
                })
    except Exception as e:
        print(f"❌ {keyword} 뉴스 수집 중 오류: {e}")
    return news_list

def analyze_with_gemini(combined_text: str) -> str:
    """Gemini 1.5 Flash를 이용한 분석 (404/400 오류 방지 모델명 적용)"""
    if not GEMINI_API_KEY:
        return "❌ GEMINI_API_KEY가 설정되지 않았습니다."

    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
당신은 글로벌 IT 전문 시장 분석가입니다. 아래 제공된 최신 뉴스 헤드라인들을 바탕으로 
'AI 인프라' 및 '반도체' 분야에서 CapEx(설비투자) 둔화 징후나 중요한 시장 변화가 있는지 분석해 주세요.

[규칙]
1. 반드시 한국어로 답변할 것.
2. 각 뉴스별 요약보다는 전체적인 흐름을 종합하여 요약할 것.
3. 부정적이거나 긍정적인 투자 신호가 있다면 강조할 것.

[최신 뉴스 데이터]
{combined_text}
"""

    try:
        # 모델 경로를 'models/gemini-1.5-flash'로 명시 (404 방지 핵심)
        response = client.models.generate_content(
            model="models/gemini-1.5-flash", 
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"❌ Gemini 분석 실패: {e}"

def send_discord(content: str):
    """디스코드 메시지 전송"""
    if not DISCORD_WEBHOOK:
        print("⚠️ DISCORD_WEBHOOK 없음")
        return
    
    mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    data = {"content": f"{mention}{content}"}
    requests.post(DISCORD_WEBHOOK, json=data, timeout=15)

def run_agent():
    print(f"🚀 뉴스 분석 에이전트 가동: {datetime.datetime.now()}")
    
    all_context = ""
    for kw in KEYWORDS:
        news = fetch_google_news(kw)
        if news:
            all_context += f"\n[키워드: {kw}]\n"
            for n in news:
                all_context += f"- {n['title']} ({n['published']})\n"
    
    if all_context:
        print("📝 뉴스 분석 중...")
        report = analyze_with_gemini(all_context)
        
        final_report = f"🔎 **오늘의 시장 분석 보고서**\n\n{report}"
        send_discord(final_report)
        print("✅ 보고서 전송 완료")
    else:
        print("⚠️ 수집된 뉴스가 없습니다.")

# ====================== 실행 보호막 ======================
if __name__ == "__main__":
    # 이 조건문 덕분에 다른 파일(trade_alert.py)이 실행될 때 간섭하지 않습니다.
    run_agent()