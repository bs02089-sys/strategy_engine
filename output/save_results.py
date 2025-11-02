# 💾 전략 결과 저장
import pandas as pd
import os

def save_results_to_csv(results: dict, filename: str = "strategy_results.csv", folder: str = "output") -> None:
    """
    전략 결과를 CSV 파일로 저장
    """
    if not results:
        print("⚠️ 저장할 결과 없음")
        return

    # 폴더 없으면 생성
    os.makedirs(folder, exist_ok=True)
    filepath = os.path.join(folder, filename)

    # 결과를 데이터프레임으로 변환
    df = pd.DataFrame(results).T.reset_index()
    df.rename(columns={"index": "ticker"}, inplace=True)

    try:
        df.to_csv(filepath, index=False)
        print(f"💾 전략 결과 저장 완료 → {filepath}")
    except Exception as e:
        print(f"❌ 저장 실패: {e}")