# 📋 매매 엔진 운영 루틴

## 📁 프로젝트 파일 구성

| 파일명 | 역할 | 실행 주기 |
|--------|------|----------|
| `sigma_position_manager.py` | 실전 매매 알림 엔진 | 매일 밤 11시 30분 이후 |
| `SOXL_VIX_SIGMA_BACKTEST.py` | 배수 최적화 백테스트 | 매년 1월 초 |
| `optimize_strategy.py` | AI 월간 복기 엔진 | 매달 말일 |
| `config.json` | 전략 설정 파일 | 매수 집행 후 수동 업데이트 |
| `ledger.json` | 매매 장부 | 매수 집행 후 수동 기록 |
| `ledger_template_jason.py` | 매매 기록 템플릿 | 참고용 |

---

## 📅 매일 (정규장 개시 후)

```
1. 밤 11시 30분 이후 sigma_position_manager.py 실행
2. Discord 알림 확인
3. 타점 도달 시 증권사 앱에서 직접 매수 체결
```

> ⚠️ 봇은 알림만 제공하며 실제 매수는 반드시 사람이 직접 판단합니다.

---

## 🛒 매수 집행 후 (수동 필수)

```
1. ledger_template_jason.py에서 해당 템플릿 복사
2. ledger.json에 붙여넣기 후 실제 체결 정보 입력
3. config.json 업데이트
   - LAST_CAST_DATE → 오늘 날짜 (예: "2026-05-25")
   - CURRENT_CASTS  → +1
```

### ledger.json 기록 예시 (LONG 매수)
```json
{
  "date": "2026-05-25",
  "ticker": "SOXL",
  "mode": "LONG",
  "action": "BUY",
  "target_price": 158.23,
  "qty": 2,
  "current_casts": 5,
  "vix_status": "✨ 안정",
  "time_guard": "🟢 해제",
  "note": "평시 안정 장세 진입"
}
```

---

## 📅 매달 말일

```
1. optimize_strategy.py 실행
2. Hermes AI 월간 복기 리포트 확인
3. 변경된 파라미터 및 이유 검토
4. y 입력 → config.json 자동 저장
   n 입력 → config.json 변경 없이 유지
```

> 💡 AI가 한 달간 매매 기록을 분석하여 VIX 배수 등 파라미터를 미세 조정합니다.

---

## 📅 매년 1월 초

```
1. SOXL_VIX_SIGMA_BACKTEST.py 실행
2. 현재 config.json 설정값 성과 확인
3. 최적 배수 조합 확인 및 검토
4. y 입력 → config.json 자동 저장
   n 입력 → config.json 변경 없이 유지
```

> 💡 4년치 데이터 기반으로 VIX 3구간 배수를 통계적으로 재최적화합니다.

---

## ⚙️ config.json 주요 설정 항목

| 항목 | 위치 | 설명 |
|------|------|------|
| `LAST_CAST_DATE` | `POSITIONS.SOXL` | 가장 최근 매수 집행일 |
| `CURRENT_CASTS` | `POSITIONS.SOXL` | 올해 누적 매수 횟수 |
| `ANNUAL_QUOTA` | `POSITIONS.SOXL` | 연간 목표 매수 횟수 (기본 24회) |
| `MULT_NORMAL` | `VIX_CONFIG.LONG` | VIX 20 미만 평시 배수 |
| `MULT_FEAR` | `VIX_CONFIG.LONG` | VIX 20~30 공포 배수 |
| `MULT_EXTREME` | `VIX_CONFIG.LONG` | VIX 30 이상 극단 배수 |

---

## 🔒 보안 주의사항

- `config.json`은 `.gitignore`에 등록되어 깃허브에 올라가지 않습니다.
- Discord Webhook URL, User ID는 로컬 PC의 `config.json`에만 보관합니다.
- GitHub Actions 사용 시 `Settings → Secrets`에 별도 등록하세요.