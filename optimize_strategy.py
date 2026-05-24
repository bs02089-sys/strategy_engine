# fileName: optimize_strategy.py
import json
import os
import logging
from datetime import datetime
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

LEDGER_FILE = "ledger.json"
CONFIG_FILE = "config.json"

def load_json_file(file_path):
    if not os.path.exists(file_path):
        return [] if "ledger" in file_path else {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        logger.error(f"❌ {file_path} 파일의 JSON 형식이 올바르지 않습니다.")
        return [] if "ledger" in file_path else {}

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    logger.info("💾 새로운 한 달 기준 config.json 설정이 안전하게 저장되었습니다.")

def sync_to_git_via_manager():
    try:
        import sigma_position_manager as manager
        logger.info("🔄 GitHub 레포지토리에 월간 최적화 설정을 동기화합니다...")
        manager.sync_config_to_git(datetime.now().date())
    except ImportError:
        logger.warning("⚠️ sigma_position_manager.py 파일을 찾을 수 없어 Git 동기화를 건너뜁니다.")
    except Exception as e:
        logger.error(f"❌ Git 동기화 중 오류 발생: {e}")

def optimize_strategy():
    ledger = load_json_file(LEDGER_FILE)
    config = load_json_file(CONFIG_FILE)
    
    if not ledger:
        logger.info("📭 ledger.json에 누적된 매매 기록이 없어 월간 복기를 진행하지 않습니다.")
        return

    logger.info("🧠 월간 AI 복기 엔진 가동 중... 한 달간의 호흡을 분석합니다.")

    # [BUG #5 FIX] API 키 환경변수 사전 검증
    if not os.getenv("GEMINI_API_KEY"):
        logger.error("❌ GEMINI_API_KEY 환경변수가 설정되지 않았습니다. 실행을 중단합니다.")
        return

    try:
        client = genai.Client()
    except Exception as e:
        logger.error(f"❌ Gemini API 클라이언트 초기화 실패: {e}")
        return

    # 📌 월간 호흡에 맞춰 튜닝된 퀀트 에이전트 프롬프트
    prompt = f"""
너는 자가 개선(Self-Improving) 능력을 가진 최고의 퀀트 트레이딩 에이전트 'Hermes'다.
사용자는 내년 5월까지 약 1년 동안 SOXL 종목을 총 24회(월 평균 2회) 진입하는 '장기 쿼터 분할 매수 롱 모드'를 운영 중이다.
한 달간 쌓인 매매 일지(ledger.json)와 현재의 매매 엔진 설정(config.json)을 거시적으로 분석하여 최적의 파라미터 조정을 수행하라.

[월간 분석 지침 - 묵직한 자가 개선]
1. 사용자는 월 2회 안팎의 느린 템포로 자산을 매집한다. 매매 일지의 'date' 갭과 'current_casts'(누적 실탄 수)를 보고, 이번 달 실탄 소모 속도가 계획 대비 너무 빠르거나 느리지 않았는지 체크하라.
2. 만약 하락 강도가 세서 한 달 동안 실탄 소모가 너무 빨랐다면, 다음 달에는 조급한 진입을 막기 위해 `MULT_FEAR` 설정을 높이거나 타임 가드 설정을 보수적으로 유도해야 한다.
3. 과학적 방법론에 따라, 한 달에 단 '1개의 핵심 변수'만 미세 조정(Fine-tuning)하여 다음 한 달간의 타점을 제어하라. 시장 트렌드가 유지된다면 변경 없이 기존 설정을 유지해도 좋다.

[사용자의 매매 장부 (ledger.json)]
{json.dumps(ledger, indent=2, ensure_ascii=False)} 

[현재 엔진 설정 (config.json)]
{json.dumps(config, indent=2, ensure_ascii=False)}

[출력 형식]
반드시 아래의 정확한 JSON 구조로만 답변해야 하며, 주석이나 추가 텍스트는 절대 포함하지 말라.
{{
  "analysis_report": "이번 달(한 달간) 매수 속도 평가 및 거시 시장 분석 리포트 (인간 트레이더를 위한 상세 브리핑)",
  "modified_parameter": "이번 달에 수정한 딱 1개의 파라미터 변수명 (예: VIX_CONFIG.LONG.MULT_FEAR 또는 POSITIONS.SOXL.ANNUAL_QUOTA)",
  "reason_for_change": "이 변수를 다음 한 달 동안 이렇게 유지해야 하는 거시적/통계적 이유",
  "updated_config": {{
     ... 여기에 업데이트가 완료된 전체 config.json 구조를 그대로 채워서 출력할 것 ...
  }}
}}
"""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1 # 한 달 주기 분석이므로 훨씬 더 보수적이고 일관된 수치 제어를 위해 온도를 낮춤
            )
        )
        
        # [BUG #4 FIX] Gemini가 ```json 마크다운을 붙여 반환할 경우 파싱 실패 방지
        raw = response.text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        
        print("\n" + "="*50)
        print("📊 [Hermes AI 월간 트레이딩 복기 리포트]")
        print("="*50)
        print(f"📝 분석 결과:\n{result.get('analysis_report')}\n")
        print(f"⚙️ 변경된 파라미터: {result.get('modified_parameter')}")
        print(f"💡 변경 이유: {result.get('reason_for_change')}")
        print("="*50 + "\n")
        
        new_config = result.get("updated_config")
        # [BUG #1 FIX] config.json 실제 구조에 맞게 키 검증 (tickers → TICKERS)
        if new_config and isinstance(new_config, dict) and "TICKERS" in new_config:
            # 저장 여부 사용자 확인 후 반영
            answer = input("\n💾 AI 분석 결과를 config.json에 저장하시겠습니까? (y/n): ").strip().lower()
            if answer == "y":
                # [BUG #6 FIX] 기존 config를 베이스로 AI 결과를 병합 → 키 누락으로 인한 덮어쓰기 방지
                merged_config = {**config, **new_config}
                save_config(merged_config)
                sync_to_git_via_manager()
            else:
                logger.info("⏭️ 저장을 건너뜁니다. config.json은 변경되지 않았습니다.")
        else:
            logger.error("❌ AI가 리턴한 새로운 config 구조가 올바르지 않아 반영을 취소합니다.")

    except Exception as e:
        logger.error(f"❌ AI 월간 복기 진행 중 에러 발생: {e}")

if __name__ == "__main__":
    optimize_strategy()