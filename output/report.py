# 📋 전략 성과 요약 리포트
def generate_report(results: dict) -> None:
    """
    전략 결과를 요약해서 콘솔에 출력 (간단 버전)
    """
    if not results:
        print("⚠️ 리포트 생성할 결과 없음")
        return

    print("\n📋 전략 성과 요약 리포트")
    print("-" * 40)

    for ticker, metrics in results.items():
        ret = metrics.get("return", 0.0)
        mdd = metrics.get("mdd", 0.0)
        print(f"📈 {ticker}: 수익률 {ret:.2f} / MDD {mdd:.2f}")

    print("-" * 40)