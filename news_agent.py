"""
글로벌 시장 뉴스 수집 및 AI 번역 에러 핸들링 봇 (올라마-Gemini 하이브리드 교차 검증 버전)
"""

import os
import sys
import json
import requests
import xml.etree.ElementTree as ET
import google.genai as genai
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# 🛡️ [올라마 라이브러리 하이브리드 방어막 설정]
# 로컬 PC 환경(Ollama 존재)과 깃허브 액션즈 환경(Ollama 부재)을 스스로 판별합니다.
try:
    import ollama
    HAS_OLLAMA = True
except ImportError:
    HAS_OLLAMA = False

# 1. 환경 변수 로드 (로컬 PC 구동 시 .env 파일 스캔용)
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 2. 통합 설정 파일(config.json) 로드
CONFIG_PATH = Path("config.json")

if not CONFIG_PATH.exists():
    print("❌ 에러: config.json 파일이 존재하지 않습니다. 파일을 먼저 생성해 주세요.")
    sys.exit(1)

try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config_data = json.load(f)
except Exception as e:
    print(f"❌ 에러: config.json 파일을 읽는 도중 오류가 발생했습니다: {e}")
    sys.exit(1)

# 3. config.json에서 실시간 설정 추출 및 안전화
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip() or config_data.get("DISCORD_WEBHOOK", "").strip()
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "").strip() or config_data.get("DISCORD_USER_ID", "").strip()

# 뉴스 수집 검색 키워드 및 개수 설정 가져오기
NEWS_SETTINGS = config_data.get("news_settings", {})
KEYWORDS = NEWS_SETTINGS.get("KEYWORDS", [
    "AI Infrastructure",
    "semiconductor stock",
    "NVDA",
    "SOXL ETF",
    "TSLA stock",
    "IONQ stock"
])
MAX_NEWS_PER_KEYWORD = NEWS_SETTINGS.get("MAX_NEWS_PER_KEYWORD", 5)


def fetch_latest_news():
    """Google News RSS를 통해 키워드별 최신 뉴스를 수집합니다."""
    all_news_text = ""
    headers = {"User-Agent": "Mozilla/5.0"}
    
    for kw in KEYWORDS:
        rss_url = f"https://news.google.com/rss/search?q={kw}+when:1d&hl=en-US&gl=US&ceid=US:en"
        try:
            resp = requests.get(rss_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                all_news_text += f"\n### 📂 검색 키워드: {kw}\n"
                items = root.findall(".//item")
                
                for item in items[:MAX_NEWS_PER_KEYWORD]:
                    title = item.find('title').text or ""
                    all_news_text += f"- {title}\n"
        except Exception as e:
            print(f"❌ 뉴스 수집 에러 ({kw}): {e}")
            continue
    return all_news_text


def send_to_discord(webhook_url, user_id, message_body, ping_prefix=""):
    """디스코드 2000자 제한을 우회하여 안전하게 분할 전송합니다."""
    mention = f"<@{user_id}>\n" if user_id else ""
    header = f"{mention}{ping_prefix}"
    
    MAX_LEN = 1900
    
    if len(header + message_body) <= 2000:
        chunks = [message_body]
    else:
        lines = message_body.split('\n')
        chunks = []
        current_chunk = ""
        
        for line in lines:
            if len(current_chunk + line + '\n') > MAX_LEN:
                chunks.append(current_chunk)
                current_chunk = line + '\n'
            else:
                current_chunk += line + '\n'
        if current_chunk:
            chunks.append(current_chunk)

    for i, chunk in enumerate(chunks):
        if i == 0:
            payload = {"content": f"{header}{chunk}"}
        else:
            payload = {"content": f"🔄 **이어서 계속...**\n\n{chunk}"}
            
        try:
            resp = requests.post(webhook_url, json=payload, timeout=15)
            if resp.status_code in [200, 204]:
                print(f"✅ 디스코드 메시지 전송 완료 (파트 {i+1}/{len(chunks)})")
            else:
                print(f"❌ 디스코드 전송 실패 (상태 코드: {resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"❌ 디스코드 전송 중 네트워크 예외 발생: {e}")


def main():
    global GEMINI_API_KEY
    if not GEMINI_API_KEY:
        GEMINI_API_KEY = config_data.get("GEMINI_API_KEY")

    # 1. 뉴스 수집 단계
    news_content = fetch_latest_news()
    if not news_content.strip():
        print("ℹ️ 수집된 뉴스가 없습니다.")
        return

    report = None
    ai_mode_notice = ""

    # 2. ✨ AI 엔진 분기 처리 (Ollama vs Gemini)
    if HAS_OLLAMA:
        # 💻 로컬 PC 환경: 오프라인 무료 Ollama 엔진 구동
        ai_mode_notice = "🤖 **[Ollama AI 뉴스 브리핑]**\n\n"
        print("ℹ️ 로컬 AI 엔진(Ollama) 감지: Llama3 모델로 뉴스 요약 및 번역을 시작합니다.")
        try:
            prompt = (
                f"너는 금융 전문 번역가이자 퀀트 애널리스트야. 아래 뉴스 제목들을 한국어로 직관적이고 깔끔하게 번역해줘.\n"
                f"단, 회사명, 주식 티커(NVDA, SOXL 등), 고유명사는 영문 그대로 유지해줘. 섹터별 분류 구조도 그대로 유지해줘:\n\n{news_content}"
            )
            response = ollama.chat(
                model='llama3',
                messages=[{'role': 'user', 'content': prompt}]
            )
            report = response['message']['content']
        except Exception as e:
            print(f"❌ 로컬 Ollama 구동 실패: {e}. Gemini 백업 엔진 전환을 시도합니다.")

    # 🐙 깃허브 서버 환경이거나 로컬 Ollama가 실패한 경우 -> Gemini API 백업 작동
    if not report:
        if not GEMINI_API_KEY:
            print("❌ 에러: AI 처리를 위한 자격 증명(Ollama 또는 GEMINI_API_KEY)이 없습니다.")
            return
            
        ai_mode_notice = "✨ **[Gemini AI 뉴스 브리핑]**\n\n"
        print("ℹ️ 클라우드 AI 엔진(Gemini) 구동: 뉴스 번역을 시작합니다.")
        try:
            client = genai.Client(
                api_key=GEMINI_API_KEY,
                http_options={'api_version': 'v1beta'}
            )
            
            prompt = (
                f"너는 금융 전문 번역가야. 아래 뉴스 제목들을 한국어로 번역해줘.\n"
                f"단, 회사명, 주식 티커, 고유명사는 영문 그대로 유지해. 섹터 구조는 그대로 유지해:\n\n{news_content}"
            )
            
            response = client.models.generate_content(
                model="models/gemini-2.5-flash",
                contents=prompt
            )
            report = response.text
        except Exception as e:
            print(f"❌ Gemini 클라우드 AI 분석 실패: {e}")

    # 3. 메시지 타이틀 및 본문 결합
    if report:
        final_message = f"{ai_mode_notice}{report}"
    else:
        final_message = f"⚠️ **AI 번역 전원 실패 (뉴스 원문 출력)**\n\n{news_content}"

    # 4. 매월 1일 하트비트/핑 기능 (계정 만료 방지)
    ping_prefix = ""
    if datetime.now().day == 1:
        ping_prefix = "📡 **[System Ping]** 디스코드 계정 활성화 유지 신호 송신 중 (정상 작동)\n\n"
        
    # 5. 환경 맞춤형 최종 전송 및 화면 출력 제어
    if DISCORD_WEBHOOK:
        send_to_discord(DISCORD_WEBHOOK, DISCORD_USER_ID, final_message, ping_prefix)
    else:
        print("\n" + "="*60)
        print("ℹ️  로컬 편집기 환경 감지: 디스코드 웹훅이 없어 콘솔창에 결과를 출력합니다.")
        print("="*60)
        print(final_message)
        print("="*60 + "\n")


if __name__ == "__main__":
    main()