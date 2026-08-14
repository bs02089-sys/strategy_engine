#!/usr/bin/env python3
"""AI 에이전트 영어 공부법 — 회화 표현 복습 CLI.

사용법:
    python3 study.py learn [상황]    # 표현 학습 (영어 + 뜻 + 팁) — 역할극 전에 먼저
    python3 study.py review [상황]   # 복습 예정 카드 (간격 반복, Leitner)
    python3 study.py quiz [상황]     # 상황별 퀴즈 (한국어 → 영어)
    python3 study.py list [상황]     # 표현 목록 출력
    python3 study.py stats           # 진척 통계

파일:
    phrases.json   — 회화 표현 덱 (한국어 뜻/팁)
    progress.json  — 개인 복습 진척 (자동 생성 · gitignore 대상)

검증 방식: 키워드 매칭 (기능어 제외). 엄격한 문법 판정은 아님 —
자연스러운 대안 표현 교정은 채팅에서 에이전트 역할극이 담당한다.
"""

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PHRASES_FILE = BASE_DIR / "phrases.json"
PROGRESS_FILE = BASE_DIR / "progress.json"

# Leitner 상자별 복습 간격(일): 상자 1 → 2 → 3 → 4 → 5
BOX_DAYS = [1, 3, 7, 14, 30]

# 키워드 매칭에서 제외할 기능어 (의문사 포함 — 질문 문장에서 답이 아닌 질문 형태를 요구)
STOP_WORDS = {
    # 관사 / 접속사 / 전치사
    "the", "a", "an", "and", "or", "but", "so", "if", "then", "than", "as",
    "to", "of", "in", "on", "at", "by", "for", "with", "from", "about",
    "over", "under", "up", "down", "out", "off",
    # be / 조동사 / 보조 동사
    "is", "am", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "done", "have", "has", "had",
    "can", "could", "would", "will", "shall", "should", "may", "might", "must",
    # 대명사
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us",
    "them", "my", "your", "our", "their", "its", "mine",
    "this", "that", "these", "those",
    # 의문사
    "where", "what", "when", "who", "whom", "whose", "which", "why", "how",
    # 부사 / 양화사 / 기타 기능어
    "not", "no", "yes", "there", "here", "some", "any",
    "more", "most", "much", "many", "very", "really", "just", "only",
    "please", "thanks", "thank", "get", "got",
}


def load_phrases():
    with open(PHRASES_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


PROGRESS = load_progress()


def save_progress():
    PROGRESS_FILE.write_text(
        json.dumps(PROGRESS, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def key_words(text):
    """문장에서 내용어(기능어 제외) 추출 — 정답 판정용."""
    words = re.findall(r"[a-zA-Z']+", text.lower())
    return [w for w in words if w not in STOP_WORDS and len(w) >= 3]


def normalize(text):
    """문자·공백만 남기고 소문자로 — 완전 일치 판정용."""
    return re.sub(r"[^a-zA-Z']", "", text.lower())


def grade(answer, en):
    """correct(모든 키워드) / partial(70% 이상) / wrong."""
    expected = key_words(en)
    if not expected:
        return "correct" if normalize(answer) == normalize(en) else "wrong"
    given = set(key_words(answer))
    ratio = len(given & set(expected)) / len(expected)
    if given.issuperset(expected):
        return "correct"
    if ratio >= 0.7:
        return "partial"
    return "wrong"


def mark(pid, ok):
    """카드 1장의 복습 결과를 Leitner 간격으로 기록."""
    p = PROGRESS.get(pid, {"box": 1, "mistakes": 0, "reviews": 0})
    if ok:
        p["box"] = min(p.get("box", 1) + 1, len(BOX_DAYS))
        interval = BOX_DAYS[p["box"] - 1]
    else:
        p["box"] = 1
        interval = BOX_DAYS[0]
        p["mistakes"] = p.get("mistakes", 0) + 1
    p["reviews"] = p.get("reviews", 0) + 1
    p["due"] = (date.today() + timedelta(days=interval)).isoformat()
    p["last"] = date.today().isoformat()
    PROGRESS[pid] = p


def iter_situations(sit_filter=None):
    """상황 필터(id 또는 한국어 제목) 적용. 필터는 phrases.json의 모든 상황을 순회한다."""
    data = load_phrases()
    for sit in data["situations"]:
        if sit_filter and sit["id"] != sit_filter and sit["title"] != sit_filter:
            continue
        yield sit


def ask_and_mark(ph):
    """카드 1장 퀴즈 + 진척 기록. 정답 여부 반환."""
    print(f"  한국어: {ph['ko']}")
    try:
        answer = input("  영어로: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n저장하고 종료합니다.")
        save_progress()
        sys.exit(0)
    if answer == "":
        print(f"  → 정답: {ph['en']}")
        mark(ph["id"], False)
        return False
    g = grade(answer, ph["en"])
    ok = g != "wrong"
    if g == "correct":
        print(f"  ✅ {ph['en']}")
    elif g == "partial":
        print(f"  🟡 거의 정답! 자연스러운 표현: {ph['en']}")
    else:
        print(f"  ❌ 정답: {ph['en']}")
    mark(ph["id"], ok)
    if "tip" in ph:
        print(f"  💡 {ph['tip']}")
    return ok


def cmd_learn(sit_filter):
    """역할극 전 단계 — 상황별 표현을 영어 + 뜻 + 팁으로 읽기만 한다 (진척 기록 없음)."""
    matched = list(iter_situations(sit_filter))
    if not matched:
        print(f"'{sit_filter}' 상황을 찾지 못했어요. `python3 study.py list` 로 확인하세요.")
        return
    for sit in matched:
        print(f"\n📚 [{sit['title']}] — 역할극 전에 표현 먼저 읽어보기")
        for i, ph in enumerate(sit["phrases"], 1):
            print(f"  {i}. {ph['en']}")
            print(f"     {ph['ko']}")
            if "tip" in ph:
                print(f"     💡 {ph['tip']}")
    print("\n→ 이제 이 표현들을 활용해 채팅에서 역할극을 시작하거나, `python3 study.py quiz <상황>` 으로 확인해 보세요.")


def cmd_quiz(sit_filter):
    matched = list(iter_situations(sit_filter))
    if not matched:
        print(f"'{sit_filter}' 상황을 찾지 못했어요. `python3 study.py list` 로 확인하세요.")
        return
    total = correct = 0
    for sit in matched:
        print(f"\n📚 [{sit['title']}]")
        for ph in sit["phrases"]:
            total += 1
            if ask_and_mark(ph):
                correct += 1
        save_progress()
    pct = correct / total * 100 if total else 0
    print(f"\n=== 퀴즈 완료: {correct}/{total} ({pct:.0f}%) ===")


def cmd_review(sit_filter):
    matched = list(iter_situations(sit_filter))
    if not matched:
        print(f"'{sit_filter}' 상황을 찾지 못했어요. `python3 study.py list` 로 확인하세요.")
        return
    due = []
    for sit in matched:
        for ph in sit["phrases"]:
            p = PROGRESS.get(ph["id"])
            # 학습한 카드만 복습 — 새 카드는 quiz로 진입한다 (첫 실행에 전부 쏟아지는 것 방지)
            if p is not None and p["due"] <= date.today().isoformat():
                due.append((sit, ph))
    if not due:
        print("오늘 복습할 카드가 없어요. `python3 study.py quiz` 로 새 표현을 배워 보세요.")
        return
    print(f"🎯 오늘 복습할 카드: {len(due)}장")
    correct = 0
    for sit, ph in due:
        box = PROGRESS.get(ph["id"], {}).get("box", 1)
        print(f"\n📚 [{sit['title']}] (상자 {box}/{len(BOX_DAYS)})")
        if ask_and_mark(ph):
            correct += 1
        save_progress()
    print(f"\n=== 복습 완료: {correct}/{len(due)} ===")


def cmd_list(sit_filter):
    matched = list(iter_situations(sit_filter))
    if not matched:
        print(f"'{sit_filter}' 상황을 찾지 못했어요.")
        return
    for sit in matched:
        print(f"\n📚 [{sit['title']}]")
        for ph in sit["phrases"]:
            box = PROGRESS.get(ph["id"], {}).get("box", "-")
            print(f"  [{box}] {ph['en']} — {ph['ko']}")


def cmd_stats():
    data = load_phrases()
    all_phrases = [ph for sit in data["situations"] for ph in sit["phrases"]]
    studied = [ph for ph in all_phrases if ph["id"] in PROGRESS]
    mastered = [ph for ph in studied if PROGRESS[ph["id"]]["box"] >= 4]
    due = [ph for ph in studied if PROGRESS[ph["id"]]["due"] <= date.today().isoformat()]
    mistakes = sum(PROGRESS[ph["id"]]["mistakes"] for ph in studied)
    print("📊 영어 공부 진척")
    print(f"  전체 표현 : {len(all_phrases)}")
    print(f"  학습 완료 : {len(studied)} ({len(studied) / len(all_phrases):.0%})")
    print(f"  숙달(상자 4+) : {len(mastered)}")
    print(f"  오늘 복습 : {len(due)}")
    print(f"  누적 오답 : {mistakes}")
    print("\n  상황별:")
    for sit in data["situations"]:
        phs = sit["phrases"]
        learned = sum(1 for ph in phs if ph["id"] in PROGRESS)
        m = sum(1 for ph in phs if PROGRESS.get(ph["id"], {}).get("box", 0) >= 4)
        print(f"    {sit['title']}: {learned}/{len(phs)} 학습 · {m} 숙달")


USAGE = """사용법:
  python3 study.py learn [상황]    # 표현 학습 (영어 + 뜻 + 팁) — 역할극 전에 먼저
  python3 study.py review [상황]   # 복습 예정 카드 (간격 반복)
  python3 study.py quiz [상황]     # 상황별 퀴즈 (한국어 → 영어)
  python3 study.py list [상황]     # 표현 목록 출력
  python3 study.py stats           # 진척 통계
  상황: airport / hotel / restaurant / cafe / shopping / directions / transport / emergency / smalltalk (또는 한국어 제목)
"""


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "help"
    sit_filter = args[1] if len(args) > 1 else None
    if cmd == "learn":
        cmd_learn(sit_filter)
    elif cmd == "quiz":
        cmd_quiz(sit_filter)
    elif cmd == "review":
        cmd_review(sit_filter)
    elif cmd == "list":
        cmd_list(sit_filter)
    elif cmd == "stats":
        cmd_stats()
    else:
        print(USAGE)


if __name__ == "__main__":
    main()
