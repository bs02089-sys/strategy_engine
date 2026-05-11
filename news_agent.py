import feedparser
import google.genai as genai
import requests
from dotenv import load_dotenv
import os
from datetime import datetime, timedelta
from urllib.parse import quote

# config.py 파일에서 설정값 로드
try:
    from config import KEYWORDS, MAX_NEWS_PER_KEYWORD
except ImportError:
    print("❌ 에러: config.py 파일을 찾을 수 없습니다. 설정 파일을 확인해주세요.")
    exit()

load_dotenv()

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not DISCORD_WEBHOOK or not GEMINI_API_KEY:
    print("❌ 에러: DISCORD_WEBHOOK 또는 GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")
    exit()

# ─────────────────────────────────────────
# 1. 뉴스 수집 (투자 둔화 시그널 쿼리 보완)
# ─────────────────────────────────────────

def fetch_news(keyword: str, max_results: int = MAX_NEWS_PER_KEYWORD) -> list[dict]:
    """Google News RSS에서 키워드 기반 뉴스 수집"""
    
    # AI/반도체 관련 키워드일 경우 '둔화' 및 'CapEx' 키워드 조합
    search_query = keyword
    if any(target in keyword.upper() for target in ["AI", "STOCK", "ETF", "SEMICONDUCTOR"]):
        search_query = f'"{keyword}" AND ("CapEx" OR "slowdown" OR "spending cut" OR "peak-out")'
    
    encoded = quote(search_query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"

    feed = feedparser.parse(url)
    news_list = []
    cutoff = datetime.now() - timedelta(hours=24)

    for entry in feed.entries[:max_results]:
        try:
            published = datetime(*entry.published_parsed[:6])
            if published < cutoff: continue
        except Exception: published = datetime.now()

        news_list.append({
            "title": entry.get("title", ""),
            "summary": entry.get("summary", "")[:500],
            "link": entry.get("link", ""),
            "published": published.strftime("%Y-%m-%d %H:%M"),
            "source": entry.get("source", {}).get("title", "Unknown"),
        })
    return news_list

# ─────────────────────────────────────────
# 2. Gemini 분석 (강세장 종료 신호 감지 프롬프트)
# ─────────────────────────────────────────

def analyze_with_gemini(keyword: str, news_list: list[dict]) -> str:
    """Gemini API로 뉴스 분석 및 리스크 탐지"""
    if not news_list: return None

    client = genai.Client(api_key=GEMINI_API_KEY)
    news_text = "\n\n".join([
        f"[{i+1}] {n['title']}\n출처: {n['source']} | {n['published']}\n내용: {n['summary']}"
        for i, n in enumerate(news_list)
    ])

    prompt = f"""당신은 전문 주식 애널리스트입니다. 아래 뉴스 묶음을 한국어로 분석하세요.
키워드: {keyword}

{news_text}

분석 시 '강세장 종료 신호(AI 투자 둔화, CapEx 감소, 반도체 피크아웃)'가 있는지 엄격히 체크하세요.

형식:
**📊 시장 분위기**: (긍정적/부정적/중립적 + 이유)
**🚨 AI 투자 경보**: (CapEx 둔화 징후가 보이면 상세히 기술, 없으면 '특이사항 없음')
**🔑 핵심 내용**: (3줄 요약)
**💹 투자 시사점**: (대응 전략)
**⚠️ 주의사항**: (리스크 요인)"""

    response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
    return response.text

# ─────────────────────────────────────────
# 3. Discord 전송
# ─────────────────────────────────────────

def send_to_discord(keyword: str, news_list: list[dict], analysis: str):
    """Discord Webhook으로 분석 결과 전송"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # 분석 내용에 따라 메시지 색상 변경 (경보 시 붉은색)
    is_alert = "🚨" in (analysis or "") and "특이사항 없음" not in analysis
    color = 0xFF0000 if is_alert else 0x00B4D8

    payload = {
        "username": "📈 Stock News Agent",
        "embeds": [
            {
                "title": f"🔔 {keyword} 브리핑 ({now})",
                "description": f"최신 뉴스 {len(news_list)}건 분석 완료",
                "color": color,
                "footer": {"text": "Powered by Gemini 2.0 Flash"}
            },
            {
                "title": "📋 수집된 뉴스 리스트",
                "color": 0x48CAE4,
                "fields": [
                    {"name": f"{i+1}. {n['title'][:80]}", "value": f"[기사 보기]({n['link']})", "inline": False}
                    for i, n in enumerate(news_list[:5])
                ]
            },
            {
                "title": "🤖 AI 투자 심층 분석",
                "description": analysis[:4000] if analysis else "분석 실패",
                "color": 0x0077B6
            }
        ]
    }
    requests.post(DISCORD_WEBHOOK, json=payload)

# ─────────────────────────────────────────
# 4. 실행 (GitHub Actions가 스케줄 담당)
# ─────────────────────────────────────────

def run_agent():
    print(f"🚀 실행 시작: {datetime.now()}")
    for keyword in KEYWORDS:
        news = fetch_news(keyword)
        if not news:
            print(f"⚠️ {keyword}: 수집된 뉴스 없음")
            continue
        analysis = analyze_with_gemini(keyword, news)
        send_to_discord(keyword, news, analysis)
        print(f"✅ {keyword} 전송 완료")
    print("🎯 전체 완료")

if __name__ == "__main__":
    run_agent()