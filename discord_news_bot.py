import os
import requests
from groq import Groq

# 환경변수 불러오기
API_KEY = os.environ.get("GROQ_API_KEY")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")

def get_market_analysis(symbols):
    """그록 API를 통해 한자/일본어 없이 순수 한국어로만 분석 요청"""
    client = Groq(api_key=API_KEY)
    
    # 시스템 프롬프트에 엄격한 언어 제한 추가
    system_prompt = (
        "당신은 월가 애널리스트입니다. "
        "모든 답변은 오직 한국어로만 작성하세요. "
        "한자, 히라가나, 가타카나 등 어떤 외국어 문자도 절대 사용하지 마세요. "
        "가독성을 위해 각 종목별로 명확하게 번호를 매기고 문단을 나누어 작성하세요."
    )
    
    symbols_str = ", ".join(symbols)
    user_content = f"{symbols_str} 종목들을 포함하는 뉴스를 요약하고, 월가 애널리스트 관점에서 분석해줘."

    response = client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        model="llama-3.3-70b-versatile"
    )
    
    # 텍스트가 정상적으로 생성되었는지 확인 후 반환
    return response.choices[0].message.content

def send_to_discord(message, webhook_url):
    """디스코드 웹훅 전송"""
    # 메시지가 비어있지 않도록 처리
    if not message:
        message = "분석 결과를 불러올 수 없습니다."
        
    payload = {"content": f"### 💡 월가 애널리스트 분석 리포트\n\n{message}"}
    response = requests.post(webhook_url, json=payload)
    response.raise_for_status()

if __name__ == "__main__":
    if not API_KEY or not WEBHOOK_URL:
        print("에러: 환경변수가 설정되지 않았습니다.")
    else:
        try:
            target_symbols = ["SOXX", "SOXL", "IONQ", "TSLA"]
            
            # 분석 수행
            analysis_text = get_market_analysis(target_symbols)
            
            # 결과 전송
            send_to_discord(analysis_text, WEBHOOK_URL)
            print("분석 완료 및 디스코드 전송 성공.")
            
        except Exception as e:
            print(f"오류 발생: {e}")