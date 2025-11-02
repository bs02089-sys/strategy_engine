import os
import json
import logging
import datetime
from typing import Any, Dict

# 🧾 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 📁 전략 파일 경로
STRATEGY_JSON_PATH = os.path.join(os.path.dirname(__file__), "..", "optimal_strategy.json")

# 📄 전략 JSON 로딩 및 기본 구조 보장
def ensure_strategy_json() -> Dict[str, Any]:
    if not os.path.exists(STRATEGY_JSON_PATH):
        empty_data = {"params": {}, "best": {}}
        try:
            with open(STRATEGY_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(empty_data, f, indent=2)
            logger.info(f"📄 빈 전략 파일 생성: {STRATEGY_JSON_PATH}")
        except Exception as e:
            logger.error(f"❌ 전략 파일 생성 실패: {e}")
        return empty_data

    try:
        with open(STRATEGY_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("params", {})
            data.setdefault("best", {})
            return data
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"⚠️ JSON 로드 실패: {e}. 빈 데이터 사용.")
        return {"params": {}, "best": {}}

# 🔐 환경 변수 로딩 (.env 직접 파싱)
def parse_env_file(env_file: str = ".env") -> None:
    env_path = os.path.join(os.path.dirname(__file__), "..", env_file)
    if not os.path.exists(env_path):
        logger.warning(f"⚠️ .env 파일 없음: {env_path}. 환경 변수 직접 설정하세요.")
        return

    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ[key.strip()] = value.strip()
        logger.info("✅ .env 파일 파싱 완료")
    except Exception as e:
        logger.error(f"❌ .env 파싱 실패: {e}")

# 🔑 API 키 로딩 함수
def load_api_key(service: str, path: str = os.path.join(os.path.dirname(__file__), "api_keys.json")) -> str:
    # 파일이 존재하지 않으면 조용히 경고 후 빈 문자열 반환
    if not os.path.exists(path):
        logger.warning(f"⚠️ API 키 파일 없음: {path}")
        return ""

    try:
        with open(path, "r", encoding="utf-8") as f:
            keys = json.load(f)
        return keys.get(service, "")
    except json.JSONDecodeError as e:
        logger.error(f"❌ API 키 JSON 파싱 실패: {e}")
        return ""
    except Exception as e:
        logger.error(f"❌ API 키 로딩 실패: {e}")
        return ""

def load_api_key_twelve_data():
    return load_api_key("twelve_data")

# 📦 전략 JSON 로딩
strategy_data = ensure_strategy_json()

# ⚙️ 전략 CONFIG
CONFIG: Dict[str, Any] = {
    "strategy_tickers": ["BLOK", "QQQM", "IAU"],
    "watchlist_tickers": ["TQQQ", "IONQ", "TSLA"],
    "short_window": 10,
    "long_window": 20,
    "lookback": 60,
    "start_date": datetime.datetime(2020, 10, 13),
    "end_date": datetime.datetime.today(),
    "initial_capital": 17788.31,
    "current_holdings": {
        "BLOK": 5,
        "QQQM": 57,
        "IAU": 36
    },
    "entry_info": {
        "BLOK": {
            "buy_date": "2020-10-13",
            "avg_price": 67.83
        },
        "QQQM": {
            "buy_date": "2020-10-13",
            "avg_price": 259.75
        },
        "IAU": {
            "buy_date": "2020-10-13",
            "avg_price": 75.35
        }
    },    
    "risk_free_rate": 0.04,
    "fees": 0.00165,
    "target_weights": {
        ticker: strategy_data["params"].get(ticker, {}).get("target_weight", None)
        for ticker in ["BLOK", "QQQM", "IAU"]
    },
    "sl_tp_params": {
        ticker: {
            "SL": strategy_data["params"].get(ticker, {}).get("SL", None),
            "TP": strategy_data["params"].get(ticker, {}).get("TP", None)
        }
        for ticker in ["BLOK", "QQQM", "IAU"]
    },
    "sp500_ticker": "^GSPC",
    "hedge_ticker": "SGOV",
    "hedge_threshold": -0.2,
    "reentry_threshold": 0.2,
    "mdd_threshold": -0.2,
    # optimizer iterations (default lowered for faster local testing)
    # - Use environment variable OPTIMIZER_N_ITER to override for batch/precise runs (e.g. >=1000)
    "optimizer_n_iter": int(os.getenv("OPTIMIZER_N_ITER", "100")),
    "API_KEY_PATH": os.path.join(os.path.dirname(__file__), "api_keys.json")
}