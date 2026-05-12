import os
import datetime
import requests
import xml.etree.ElementTree as ET
import google.genai as genai
from google.genai import errors

# ==========================================================
# 1. 환경 설정 (GitHub Secrets & Variables에서 로드)
# ==========================================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")

# GitHub Variables에서 키워드 로드 (없으면 기본값 사용)
raw_keywords = os.getenv("NEWS_KEYWORDS", "Semiconductor, AI Infrastructure, NVIDIA, CapEx")
KEYWORDS = [k.strip() for k in raw_keywords.split(",")]
MAX_NEWS_PER_KEYWORD = 3

# ==========================================================
# 2. 뉴스 수집 함수 (Google News RSS)
# ==========================================================
def fetch_latest_news():
    """구글 뉴스에서 설정된 키워드별 최신 뉴스를 수집합니다."""
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
                items = root.findall(".//item")
                if not items:
                    all_news_text += "- 최근 24시간 내 뉴스 없음\n"
                for item in items[:MAX_NEWS_PER_KEYWORD]:
                    title = item.find("title").text
                    all_news_text += f"- {title}\n"
        except Exception as e:
            print(f"⚠️ {kw} 뉴스 수집 중 오류: {e}")
            
    return all_news_text

# ==========================================================
# 3. Gemini AI 분석 함수
# ==========================================================
def analyze_market_with_gemini(context: str):
    """Gemini 1.5 Flash 모델을 사용하여 시장 동향 분석"""
    if not GEMINI_API_KEY:
        return "❌ Gemini API 키가 설정되지 않았습니다."

    client = genai.Client(api_key=GEMINI_API_KEY)
    model_id = "models/gemini-1.5-flash"

    prompt = f"""
당신은 IT/반도체 시장 전문 분석가입니다. 
다음 뉴스 헤드라인들을 분석하여 반도체 및 AI 인프라 투자 동향을 한국어로 요약해 주세요.

[요약 가이드라인]
1. 불렛 포인트를 사용하여 가독성 있게 작성할 것.
2. 투자 확대나 둔화에 대한 핵심 신호가 있다면 강조할 것.
3. 분석할 뉴스가 없다면 '현재 특이사항 없음'이라고 답변할 것.

[뉴스 데이터]
{context}
"""

    try:
        response = client.models.generate_content(model=model_id, contents=prompt)
        return response.text
    except errors.ClientError as e:
        return f"❌ Gemini API 에러: {e}"
    except Exception as e:
        return f"❌ 분석 중 오류 발생: {e}"

# ==========================================================
# 4. 메인 실행 함수
# ==========================================================
def main():
    print(f"🚀 뉴스 에이전트 시작: {datetime.datetime.now()}")
    
    # 1. 뉴스 수집
    news_content = fetch_latest_news()
    
    if not news_content.strip():
        print("⚠️ 수집된 뉴스 데이터가 없습니다.")
        return

    # 2. AI 분석
    print("📝 Gemini 분석 중...")
    analysis_report = analyze_market_with_gemini(news_content)
    
    # 3. 디스코드 전송
    if analysis_report:
        mention = f"<@{DISCORD_USER_ID}>\n" if DISCORD_USER_ID else ""
        payload = {
            "content": f"{mention}🔎 **오늘의 AI & 반도체 시장 리포트**\n\n{analysis_report}"
        }
        
        try:
            r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)
            if r.status_code in [200, 204]:
                print("✅ 디스코드 전송 성공!")
            else:
                print(f"❌ 전송 실패 (코드: {r.status_code})")
        except Exception as e:
            print(f"❌ 디스코드 통신 오류: {e}")

# ==========================================================
# 5. 실행 보호막 (Import 시 실행 방지)
# ==========================================================
if __name__ == "__main__":
    main()