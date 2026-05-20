import numpy as np
import pandas as pd
import yfinance as yf
import warnings

# 🛡️ 시스템 경고 메시지 제어
warnings.filterwarnings('ignore')

def run_vix_multiplier_final_optimizer():
    print("📡 [vix_multiplier_optimizer_v2.py] 2027년 새해 새 출발 영점 조절 엔진 가동...")
    print("🎬 최근 3개년 최신 시장 데이터를 수집하여 통계적 황금 배수를 역산합니다.\n")
    
    # 1. 최근 3개년 데이터 확보 (실전 엔진과 데이터 동기화)
    soxl = yf.download("SOXL", period="3y", interval="1d", progress=False)
    vix = yf.download("^VIX", period="3y", interval="1d", progress=False)
    
    if soxl.empty or vix.empty:
        print("❌ 야후 파이낸스 데이터 수집에 실패했습니다. 네트워크를 확인하세요.")
        return

    if isinstance(soxl.columns, pd.MultiIndex): soxl.columns = soxl.columns.droplevel(1)
    if isinstance(vix.columns, pd.MultiIndex): vix.columns = vix.columns.droplevel(1)

    df = pd.DataFrame({
        'Open': soxl['Open'].astype(float),
        'High': soxl['High'].astype(float),
        'Low': soxl['Low'].astype(float),
        'Close': soxl['Close'].astype(float),
        'VIX': vix['Close'].astype(float)
    }).dropna()

    # 📐 [실전 완벽 동기화] 90일 로그수익률 기반 순수 일간 표준편차(Daily Sigma) 산출
    df['Prev_Close'] = df['Close'].shift(1)
    df['Log_Ret'] = np.log(df['Close'] / df['Prev_Close'])
    df['Daily_Sigma'] = df['Log_Ret'].rolling(window=90).std(ddof=1)
    df = df.dropna().copy()

    # 🎯 선장님 확정 사상: VIX 3대 축 눈금 동기화 (30 / 20)
    vix_zones = [
        {
            "name": "🔴 극단적 공포 장세 (VIX 30.0 이상)", 
            "cond": df['VIX'] >= 30.0, 
            "search_range": np.arange(1.0, 2.5, 0.05) # 더 넓은 심해 탐색을 위해 범위 확장
        },
        {
            "name": "⚠️ 공포 상승 장세 (VIX 20.0 이상 ~ 30.0 미만)", 
            "cond": (df['VIX'] >= 20.0) & (df['VIX'] < 30.0), 
            "search_range": np.arange(0.5, 2.0, 0.05)
        },
        {
            "name": "✨ 평시 안정 장세 (VIX 20.0 미만)", 
            "cond": df['VIX'] < 20.0, 
            "search_range": np.arange(0.3, 1.2, 0.05)
        }
    ]

    print(f"📊 총 분석 거래일수 : {len(df)}일")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    for zone in vix_zones:
        zone_df = df[zone["cond"]].copy()
        zone_indices = np.where(zone["cond"])[0]
        total_zone_days = len(zone_df)
        
        if total_zone_days == 0:
            print(f"{zone['name']} 구간에 해당하는 거래일이 없습니다.\n")
            continue
            
        best_multiplier = None
        best_score = -1
        best_metrics = {}

        # 그리드 탐색 루프
        for mult in zone["search_range"]:
            mult = round(mult, 2)
            triggered_count = 0
            wins = 0
            losses = 0
            total_profit = 0.0
            total_loss = 0.0
            
            # 🔥 [실전 완벽 복사] 오리지널 기하학적 로그 복리 공식을 백테스트 타점에 직결
            target_prices = zone_df['Open'] * np.exp(-zone_df['Daily_Sigma'] * mult)
            is_triggered = zone_df['Low'] <= target_prices
            
            triggered_positions = np.where(is_triggered)[0]
            
            for pos in triggered_positions:
                idx = zone_indices[pos]
                if idx + 5 < len(df): # 5일 보유 후 청산 패러다임
                    triggered_count += 1
                    # 진입가 역시 로그 복리 타점으로 정밀 산출
                    buy_price = df['Open'].iloc[idx] * np.exp(-df['Daily_Sigma'].iloc[idx] * mult)
                    sell_price = df['Close'].iloc[idx + 5]
                    ret = (sell_price - buy_price) / buy_price
                    
                    if ret > 0:
                        wins += 1
                        total_profit += ret
                    else:
                        losses += 1
                        total_loss += abs(ret)
            
            # 통계적 최소 유의성 필터 (최소 3회 이상 체결된 배수만 인정)
            if triggered_count < 3: 
                continue
                
            win_rate = (wins / triggered_count) * 100 if triggered_count > 0 else 0
            profit_factor = total_profit / total_loss if total_loss > 0 else (total_profit if total_profit > 0 else 1.0)
            
            # 성과 지표 점수화 (승률과 PF의 기하학적 균형점)
            score = win_rate * profit_factor
            
            if score > best_score:
                best_score = score
                best_multiplier = mult
                best_metrics = {
                    "total_days": total_zone_days,
                    "hits": triggered_count,
                    "hit_ratio": (triggered_count / total_zone_days) * 100,
                    "win_rate": win_rate,
                    "pf": profit_factor
                }
        
        print(f"{zone['name']}")
        if best_multiplier is not None:
            print(f"  🏆 최적 황금 배수 : -{best_multiplier:.2f}σ")
            print(f"  └─ 장세 발생일수 : {best_metrics['total_days']}일 중 {best_metrics['hits']}회 체결")
            print(f"  └─ 타점 체결 확률 : {best_metrics['hit_ratio']:.1f}%")
            print(f"  └─ 5일 청산 승률  : {best_metrics['win_rate']:.2f}%")
            print(f"  └─ 프로핏 팩터(PF) : {best_metrics['pf']:.2f}")
        else:
            print("  ⚠️ 통계적 조건(샘플 하한선)을 충족하는 최적 배수를 찾지 못했습니다.")
        print("------------------------------------------------------------")

if __name__ == "__main__":
    run_vix_multiplier_final_optimizer()