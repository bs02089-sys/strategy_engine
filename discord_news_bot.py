import os
import requests
from groq import Groq

# 1. 환경변수 불러오기 (깃허브 시크릿에 설정한 이름 그대로 사용)
API_KEY = os.environ.get("GROQ_API_KEY")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")

def get_market_analysis(symbols):
    """그록 API를 호출하여 시장 분석을 받아옵니다."""
    client = Groq(api_key=API_KEY)
    
    system_prompt = (
        "당신은 월가 애널리스트입니다. 모든 답변은 한국어로만 작성하세요. "
        "한자, 히라가나 등 외국어 문자를 철저히 제외하고, "
        "가독성을 위해 각 종목별로 명확하게 문단을 나누고 띄어쓰기를 적용하세요."
    )
    
    symbols_str = ", ".join(symbols)
    user_content = f"{symbols_str} 종목들을 포함하는 뉴스를 요약하고, 월가 애널리스트의 관점에서 분석해줘."

    response = client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        model="llama-3.3-70b-versatile"
    )
    return response.choices[0].message.content

def send_to_discord(message, webhook_url):
    """디스코드 웹훅으로 분석 결과를 전송합니다."""
    payload = {"content": f"### 💡 시장 분석 리포트\n\n{message}"}
    response = requests.post(webhook_url, json=payload)
    response.raise_for_status()

if __name__ == "__main__":
    # 필수 환경변수 체크
    if not API_KEY or not WEBHOOK_URL:
        print("에러: 환경변수(GROQ_API_KEY, DISCORD_WEBHOOK)가 설정되지 않았습니다.")
    else:
        try:
            target_symbols = ["SOXX", "AIPO", "QNDX", "NVDX", "SOXL", "IONQ", "TSLA"]
            
            # 1. 분석 수행
            analysis_text = get_market_analysis(target_symbols)
            
            # 2. 결과 전송
            send_to_discord(analysis_text, WEBHOOK_URL)
            print("분석 완료 및 디스코드 전송 성공.")
            
        except Exception as e:
            print(f"프로세스 실행 중 오류 발생: {e}")