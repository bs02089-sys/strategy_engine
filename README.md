# Strategy Engine

간단한 투자/백테스트 도구입니다. 이 저장소는 가격 데이터 로드, 시그널 생성, VectorBT 기반 전략 최적화 및 리밸런싱 로직을 포함합니다.

## 필수사항
- Python 3.10+ (프로젝트에서 사용 중인 가상환경: `vbt_312`)
- 주요 의존성: pandas, numpy, yfinance, vectorbt 등. (가상환경에 이미 설치되어 있어야 합니다.)
- API 키: TWELVEDATA 같은 외부 가격 API를 사용할 경우 `.env` 또는 환경변수에 `TWELVEDATA_API_KEY`를 설정하세요.

## 환경 변수
- `.env` 파일 또는 시스템 환경변수로 설정 가능합니다.
- 주요 환경 변수
  - `TWELVEDATA_API_KEY` — TwelveData API 키
  - `OPTIMIZER_N_ITER` — 최적화 반복 수(기본값: 100). 배치/정밀 실행 시 1000 이상 권장.

예시 `.env` (프로젝트 루트):

```
TWELVEDATA_API_KEY=YOUR_API_KEY_HERE
OPTIMIZER_N_ITER=100
```

## 빠른 시작 (PowerShell)

1) 로컬 가상환경의 Python을 사용해 `main.py` 실행 — 실시간(또는 최근) 가격을 합쳐 매수 판단을 출력합니다:

```powershell
C:\Users\bs020\strategy_engine\vbt_312\Scripts\python.exe .\main.py
```

2) 전략 러너(벡터백테스트/최적화 포함)를 실행하려면:

```powershell
C:\Users\bs020\strategy_engine\vbt_312\Scripts\python.exe .\engine\strategy_runner.py
```

3) 배치/정밀 실행 예 (OPTIMIZER_N_ITER을 1000으로 설정):

```powershell
$env:OPTIMIZER_N_ITER = '1000'
C:\Users\bs020\strategy_engine\vbt_312\Scripts\python.exe .\engine\strategy_runner.py
```

## 로깅
- 기본적으로 INFO 레벨로 동작합니다. 디버그 로그를 보고 싶으면 `config/config.py`의 로깅 설정을 `logging.basicConfig(level=logging.DEBUG, ...)`로 임시 변경하거나, 실행 시 전역 로거 레벨을 변경하세요.

## 팁
- `OPTIMIZER_N_ITER` 값을 낮추면(예: 50~200) 디버깅이 빨라집니다. 실제 실험은 500~5000 권장.
- `pf.stats()` 반환 형식은 vectorbt 버전 따라 달라질 수 있으니, 안정화를 위해 `backtest/opt_vectorbt.py`에서 다양한 반환 형식을 처리하도록 구현되어 있습니다.

---

필요하시면 README에 더 자세한 설정, 예제 출력, 또는 CLI 사용법(예: `--n-iter`)을 추가해 드리겠습니다.
