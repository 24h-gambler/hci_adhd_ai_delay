#!/usr/bin/env python3
"""AI 응답이 시스템 프롬프트 규칙을 지켰는지 검사한다.

두 곳에서 쓴다.
  1) 세션 전 — 테스트 케이스 6개의 응답을 붙여넣어 확인 (prompts/test_cases.md)
  2) 데이터 수집 후 — 로그의 모든 응답을 훑어 "금지어 출현 0건"을 확인
     (analysis/manipulation_check.py가 이 모듈을 불러 쓴다)

사용:
    python3 prompts/response_rules.py --text "그러셨군요. 어떤 점이 걸리셨나요?"
    python3 prompts/response_rules.py --text "..." --context b
    python3 prompts/response_rules.py --jsonl logs/*.jsonl
    echo "응답" | python3 prompts/response_rules.py --stdin

종료 코드: 위반 없음 0 / 위반 있음 1
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import unicodedata

SAFETY_SENTENCE = "이 부분은 연구자와 이야기하시는 것이 좋겠습니다."

# [금지] — 처리 과정·속도·대기
FORBIDDEN_PROCESS = [
    "생각 중", "생각중", "잠시만", "잠깐만", "정리하고", "정리 중", "정리중",
    "오래 걸렸", "오래걸렸", "기다리게", "기다려", "기다리셨",
    "로딩", "처리 중", "처리중", "응답 속도", "느려", "느렸", "느린",
    "빨라", "빨랐", "빠르게 답", "대기 시간", "지연",
]

# [지칭] — 자신을 감정을 가진 존재로 묘사
FORBIDDEN_SELF_EMOTION = [
    "저도 슬프", "저도 그래", "저도 그랬", "저도 마음", "저도 힘들",
    "궁금해요", "궁금하네요", "궁금합니다", "안타깝", "재미있네요", "재밌네요",
    "기뻐요", "기쁘네요", "슬프네요", "제 마음", "저는 느낍", "저도 공감",
]

# 맥락 B 공감 표현 — B안에서 허용되는 목록
EMPATHY_ALLOWED_B = ["그러셨군요", "그런 상황이셨네요", "쉽지 않으셨겠어요"]

# 감정을 대신 규정하는 단정 (B안 금지)
FORBIDDEN_EMOTION_ASCRIPTION = [
    "많이 힘드시겠", "많이 힘드셨겠", "정말 힘드셨겠", "많이 지치셨",
    "충분히 이해합니다", "얼마나 힘드셨", "마음이 아프시겠",
]

MARKDOWN_PAT = re.compile(r"(\*\*|^\s*[-*•]\s|^\s*\d+[.)]\s|^#{1,6}\s)", re.M)
SENT_SPLIT = re.compile(r"[.!?…]+[\s]*")


def has_emoji(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        if (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF
                or 0xFE0F == cp or 0x2190 <= cp <= 0x21FF and unicodedata.category(ch) == "So"):
            return True
    return False


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT.split(text.strip()) if s.strip()]


def check(text: str, context: str = "a", empathy_variant: str = "B",
          user_name: str | None = None, expect_safety: bool = False) -> list[dict]:
    """규칙 위반 목록을 돌려준다. 빈 리스트면 통과."""
    v: list[dict] = []
    t = (text or "").strip()

    def bad(rule, detail):
        v.append({"rule": rule, "detail": detail})

    if not t:
        bad("빈 응답", "응답 텍스트가 없다")
        return v

    # 안전 경로: 지정 문장만 출력해야 한다
    if expect_safety:
        if t != SAFETY_SENTENCE:
            bad("안전 문장 불일치",
                f"지정 문장만 출력해야 한다. 실제: {t[:60]!r}")
        return v
    if SAFETY_SENTENCE in t and t != SAFETY_SENTENCE:
        bad("안전 문장에 덧붙임", "안전 문장 외의 내용이 함께 출력되었다")

    # [분량]
    n = len(sentences(t))
    if not (2 <= n <= 4):
        bad("문장 수", f"{n}문장 (2~4문장이어야 함)")
    if MARKDOWN_PAT.search(t):
        bad("서식 사용", "목록·번호·굵은 글씨가 있다")
    if has_emoji(t):
        bad("이모지", "이모지가 있다")

    # [지칭]
    if user_name and user_name in t:
        bad("이름 호명", f"{user_name!r}이 응답에 있다")
    for w in FORBIDDEN_SELF_EMOTION:
        if w in t:
            bad("자기 감정 묘사", f"{w!r}")

    # [금지]
    for w in FORBIDDEN_PROCESS:
        if w in t:
            bad("처리·속도 언급", f"{w!r}")

    # [구조]
    if not t.endswith("?"):
        bad("마지막 질문 없음", f"끝: {t[-20:]!r}")
    q = t.count("?")
    if q > 1:
        bad("질문 개수", f"물음표 {q}개 (하나여야 함)")

    # 맥락 B 공감 표현
    if context.lower() == "b":
        for w in FORBIDDEN_EMOTION_ASCRIPTION:
            if w in t:
                bad("감정 단정", f"{w!r}")
        if empathy_variant.upper() == "A":
            for w in EMPATHY_ALLOWED_B:
                if w in t:
                    bad("A안 정서 표현", f"A안은 정서를 언급하지 않는다: {w!r}")
        elif empathy_variant.upper() == "B":
            hits = sum(t.count(w) for w in EMPATHY_ALLOWED_B)
            if hits > 1:
                bad("공감 표현 빈도", f"허용 표현이 {hits}회 (한 응답에 1회까지)")
    return v


# ─────────────────────────────── CLI ───────────────────────────────

def scan_jsonl(paths, empathy_variant="B"):
    rows = []
    for p in paths:
        for lineno, line in enumerate(open(p, encoding="utf-8"), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("practice"):
                continue
            v = check(rec.get("ai_response_text", ""),
                      context=str(rec.get("context", "a")),
                      empathy_variant=empathy_variant,
                      expect_safety=bool(rec.get("safety_flag")))
            if v:
                rows.append({
                    "source": f"{p}:{lineno}",
                    "participant_id": rec.get("participant_id"),
                    "condition": rec.get("condition"),
                    "context": rec.get("context"),
                    "turn_index": rec.get("turn_index"),
                    "violations": v,
                    "text": rec.get("ai_response_text", "")[:120],
                })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text", help="검사할 응답 한 건")
    ap.add_argument("--stdin", action="store_true", help="표준입력에서 응답을 읽는다")
    ap.add_argument("--jsonl", nargs="+", help="로그를 훑어 위반을 모은다")
    ap.add_argument("--context", default="a", choices=["a", "b"], help="대화 맥락")
    ap.add_argument("--variant", default="B", choices=["A", "B", "C"], help="맥락 B 공감 변형")
    ap.add_argument("--name", help="참가자 이름 (호명 검사용)")
    ap.add_argument("--expect-safety", action="store_true", help="안전 문장만 나와야 하는 턴")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.jsonl:
        paths = sorted({p for pat in args.jsonl for p in glob.glob(pat)})
        if not paths:
            print("로그 파일을 찾지 못했습니다.", file=sys.stderr)
            return 2
        rows = scan_jsonl(paths, args.variant)
        if args.json:
            json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        elif not rows:
            print(f"✓ {len(paths)}개 파일 — 규칙 위반 0건")
        else:
            print(f"✗ 규칙 위반 {len(rows)}턴")
            for r in rows[:40]:
                who = f"{r['participant_id']} {r['condition']}/{r['context']} t{r['turn_index']}"
                print(f"\n  [{who}]  {r['source']}")
                for x in r["violations"]:
                    print(f"    - {x['rule']}: {x['detail']}")
                print(f"    > {r['text']}")
            if len(rows) > 40:
                print(f"\n  … 외 {len(rows) - 40}턴")
        return 1 if rows else 0

    text = args.text if args.text else (sys.stdin.read() if args.stdin else None)
    if text is None:
        ap.print_help()
        return 2
    v = check(text, args.context, args.variant, args.name, args.expect_safety)
    if args.json:
        json.dump({"pass": not v, "violations": v}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    elif not v:
        print("✓ 통과")
    else:
        print(f"✗ 위반 {len(v)}건")
        for x in v:
            print(f"  - {x['rule']}: {x['detail']}")
    return 1 if v else 0


if __name__ == "__main__":
    raise SystemExit(main())
