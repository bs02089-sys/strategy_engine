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
                all_news_text += f"\n[섹터: {kw}]\n" # 요청하신 대로 '섹터'로 수정
                items = root.findall(".//item")
                for item in items[:3]:
                    all_news_text += f"- {item.find('title').text}\n"
        except: continue
    return all_news_text

def analyze_market_with_gemini(context: str):
    if not GEMINI_API_KEY: return None
    
    try:
        # 404 에러 해결을 위한 클라이언트 설정
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        # 모델 명칭에서 'models/'를 빼고 'gemini-1.5-flash'만 입력
        # 만약 또 404가 나면 'gemini-1.5-flash-latest'로 바꿔보세요.
        response = client.models.generate_content(
            model="gemini-1.5-flash", 
            contents=[
                "지시사항: 당신은 외신 기사를 한국어로 번역하고 요약하는 전문가입니다. 아래 뉴스를 반드시 한국어로 요약하세요.",
                f"대상 뉴스:\n{context}",
                "결과물 형태: 각 섹터별 핵심 내용을 한국어로 1~2문장으로 요약하여 출력하세요. 영어 원문을 그대로 출력하면 절대 안 됩니다."
            ]
        )
        return response.text
    except Exception as e:
        # 에러 메시지를 콘솔에 찍어 확인하기 위함
        print(f"AI 분석 실패 상세: {e}")
        return None

def main():
    news_content = fetch_latest_news()
    if not news_content.strip(): return

    analysis_report = analyze_market_with_gemini(news_content)
    
    # 분석 성공 시 결과 전송, 실패 시 뉴스 원문 전송
    if analysis_report:
        final_message = f"📢 **오늘의 글로벌 시장 분석 리포트**\n\n{analysis_report}"
    else:
        final_message = f"⚠️ **AI 요약 실패 (원문 목록)**\n\n{news_content}"

    mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
    payload = {"content": f"{mention}{final_message}"}
    requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)

if __name__ == "__main__":
    main()