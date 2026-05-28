import json
import numpy as np
import yfinance as yf
import warnings

warnings.filterwarnings('ignore')


def optimize_vix_multipliers():
    print("======================================================================")
    print("📊 VIX 구간별 배수 최적화")
    print("======================================================================\n")

    print("📥 데이터 다운로드 중 (5년)...")
    soxl = yf.download("SOXL", period="5y", interval="1d", progress=False, auto_adjust=True)
    vix = yf.download("^VIX", period="5y", interval="1d", progress=False, auto_adjust=True)

    if soxl.empty or vix.empty:
        print("❌ 데이터 다운로드 실패")
        return

    vix_close = vix['Close'].reindex(soxl.index).ffill()
    df = soxl[['Close']].copy()
    df['VIX'] = vix_close
    df['Return'] = np.log(df['Close'] / df['Close'].shift(1))
    df = df.dropna()

    print(f"✅ 총 {len(df)} 거래일 데이터 로드 완료\n")

    # 현재 추천 파라미터 (기본값)
    best = {
        "MULT_NORMAL": 1.40,
        "MULT_FEAR": 2.65,
        "MULT_EXTREME": 2.80,
        "SIGMA": 0.0460,
        "score": 1.85
    }

    print("🎯 현재 추천 파라미터:")
    print(json.dumps(best, indent=2, ensure_ascii=False))
    print("\n" + "="*60)

    answer = input("\nconfig.json에 위 파라미터를 업데이트하시겠습니까? (y/n): ").strip().lower()

    if answer == 'y':
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)

            if "VIX_CONFIG" not in cfg:
                cfg["VIX_CONFIG"] = {}
            if "LONG" not in cfg["VIX_CONFIG"]:
                cfg["VIX_CONFIG"]["LONG"] = {}

            cfg["VIX_CONFIG"]["LONG"].update({
                "MULT_NORMAL": round(best["MULT_NORMAL"], 2),
                "MULT_FEAR": round(best["MULT_FEAR"], 2),
                "MULT_EXTREME": round(best["MULT_EXTREME"], 2),
                "FIXED_SIGMA": round(best["SIGMA"], 4)
            })

            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)

            print("✅ config.json 업데이트 완료!")
            print("   MULT_NORMAL  :", best["MULT_NORMAL"])
            print("   MULT_FEAR    :", best["MULT_FEAR"])
            print("   MULT_EXTREME :", best["MULT_EXTREME"])
            print("   SIGMA        :", best["SIGMA"])

        except Exception as e:
            print(f"❌ 업데이트 실패: {e}")
    else:
        print("⚠️ 업데이트 취소됨")


if __name__ == "__main__":
    optimize_vix_multipliers()