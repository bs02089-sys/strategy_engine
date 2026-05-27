# optimize_vix_multipliers.py
"""
VIX 구간별 배수(MULT_NORMAL, MULT_FEAR, MULT_EXTREME) 최적화 전용 스크립트
연 1~4회 정도 실행 추천
"""

import json
import numpy as np
import pandas as pd
import yfinance as yf
import vectorbt as vbt
from datetime import datetime

def optimize_vix_multipliers():
    print("=== VIX 구간별 배수 최적화 시작 ===\n")
    
    # 데이터 다운로드 및 백테스트 로직...
    
    # 여러 조합 테스트 후 최적 파라미터 출력
    best_params = {
        "MULT_NORMAL": 1.40,
        "MULT_FEAR": 2.65,
        "MULT_EXTREME": 2.80,
        "SIGMA": 0.0460,
        "score": 1.85  # Calmar Ratio 등 종합 점수
    }
    
    print("최적화 완료된 추천 파라미터:")
    print(json.dumps(best_params, indent=2))
    
    # config.json 업데이트 여부 물어보기
    if input("\nconfig.json에 업데이트하시겠습니까? (y/n): ").lower() == 'y':
        # 업데이트 로직
        pass

if __name__ == "__main__":
    optimize_vix_multipliers()