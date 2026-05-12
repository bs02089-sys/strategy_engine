import os
import datetime
import requests
import xml.etree.ElementTree as ET
import google.genai as genai
from google.genai import errors

# ==========================================================
# 1. 환경 설정 (다른 파일 import 없이 직접 로드)
# ==========================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")

# 분석 키워드 (구글 뉴스 검색용)
KEYWORDS = ["AI Infrastructure", "Semiconductor CapEx"]
MAX_NEWS_PER_KEYWORD = 3

# ==========================================================
# 2. 뉴스 수집 함수 (Google News RSS)
# ==========================================================
def fetch_latest_news():
    """실제 구글 뉴스에서 최신 IT/반도체 뉴스를 수집합니다."""
    all_news_text = ""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    for kw in KEYWORDS:
        # 최근 24시간 이내(when:1d), 영문 뉴스 검색
        rss_url = f"https://news.google.com/rss/search?q={kw}+when:1d&hl=en-US&gl=US&ceid=US:en"
        try:
            resp = requests.get(rss_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                all_news_text += f"\n### Sector: {kw}\n"
                for item in root.findall(".//item")[:MAX_NEWS_PER_KEYWORD]:
                    title = item.find("title").text
                    pub_date = item.find("pubDate").text
                    all_news_text += f"- {title} ({pub_date})\n"
        except Exception as e:
            print(f"⚠️ {kw} 뉴스 수집 중 오류: {e}")
            
    return all_news_text

# ==========================================================
# 3. Gemini AI 분석 함수
# ==========================================================
def analyze_market_with_gemini(context: str):
    """Gemini 1.5 Flash 모델을 사용하여 시장 동향 분석"""
    if not GEMINI_API_KEY:
        return "❌ API 키가 설정되지 않았습니다."

    client = genai.Client(api_key=GEMINI_API_KEY)
    
    # [중요] 404 에러 방지용 공식 모델명 경로
    MODEL_ID = "models/gemini-1.5-flash"

    prompt = f"""
당신은 세계 최고의 IT/반도체 시장 분석가입니다. 
아래 뉴스 헤드라인들을 분석하여 향후 AI 인프라와 반도체 분야의 투자 흐름을 진단해 주세요.

[분석 가이드라인]
1. 반드시 한국어로 답변할 것.
2. 각 뉴스 나열보다는 전체적인 '투자 둔화'나 '확대' 신호를 요약할 것.
3. 읽기 편하게 불렛 포인트와 이모지를 사용할 것.

[데이터]
{context}
"""

    try:
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=prompt
        )
        return response.text
    except errors.ClientError as e:
        return f"❌ Gemini API 오류 (404/400 등): {e}"
    except Exception as e:
        return f"❌ 분석 중 예상치 못한 오류: {e}"

# ==========================================================
# 4. 메인 실행 로직
# ==========================================================
def run_agent():
    print(f"🚀 뉴스 에이전트 가동 시작 ({datetime.datetime.now()})")
    
    # 1단계: 뉴스 수집
    news_context = fetch_latest_news()
    
    if not news_context.strip():
        print("⚠️ 수집된 뉴스가 없어 종료합니다.")
        return

    # 2단계: AI 분석
    print("📝 뉴스 데이터 분석 중...")
    analysis_result = analyze_market_with_gemini(news_context)
    
    # 3단계: 디스코드 전송
    if analysis_result:
        mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
        final_payload = {
            "content": f"{mention}🔎 **오늘의 AI & 반도체 시장 리포트**\n\n{analysis_result}"
        }
        
        try:
            resp = requests.post(DISCORD_WEBHOOK, json=final_payload, timeout=15)
            if resp.status_code in [200, 204]:
                print("✅ 디스코드 전송 성공!")
            else:
                print(f"❌ 전송 실패: {resp.status_code}")
        except Exception as e:
            print(f"❌ 디스코드 통신 오류: {e}")

# ==========================================================
# 5. 실행 보호막 (Import 시 자동 실행 방지)
# ==========================================================
if __name__ == "__main__":
    run_agent()