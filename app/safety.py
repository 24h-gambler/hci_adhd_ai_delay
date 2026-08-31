"""배제 주제 입력 검사 (CONTRACT §5).

모델의 판단에만 의존하지 않기 위해 앱이 LLM 호출 **전에** 검사한다.

두 가지 오류를 모두 피해야 한다.
  · 놓침(false negative) — 참가자 보호 실패
  · 오탐(false positive) — 대화 하나와 데이터 한 점이 통째로 날아간다

한국어 관용구가 문제다. "배고파 죽겠어요", "피곤해 죽겠네요",
"죽을 맛이에요"는 위기 신호가 아니다. 그래서 '죽'이 아니라
'죽고 싶다' 계열만 잡는다.

띄어쓰기 흔들림("죽고싶어" / "죽고 싶어")을 흡수하기 위해 공백을 모두
제거한 문자열에 대해 검사한다.
"""

from __future__ import annotations

import re
import unicodedata

SAFETY_REPLY = "이 부분은 연구자와 이야기하시는 것이 좋겠습니다."

# 공백을 제거한 텍스트에 대해 검사한다.
PATTERNS: dict[str, list[str]] = {
    "self_harm": [
        r"자살", r"자해",
        r"죽고싶", r"죽고파", r"죽어버리고싶", r"죽어야겠",
        r"목숨을?끊", r"극단적선택",
        r"살고싶지않", r"살기싫", r"살아갈이유",
        r"사라지고싶", r"없어지고싶", r"세상을뜨",
        r"손목을?긋", r"칼로긋", r"스스로해치", r"나를해치",
        r"유서",
    ],
    "harm_others": [
        r"죽이고싶", r"죽여버리", r"때려죽", r"해치고싶",
        r"없애버리고싶", r"복수하고죽",
    ],
    "crime_legal": [
        r"마약", r"전과", r"절도", r"폭행", r"성폭",
        r"고소당", r"고소를", r"고소했", r"고발당",
        r"소송중", r"소송을", r"소송이",
        r"처벌받", r"경찰조사", r"구속",
    ],
    "acute_crisis": [
        r"지금위험", r"위급한상황", r"119에신고", r"응급실에가야",
        r"지금당장도움", r"버틸수가없",
    ],
}

_COMPILED = {cat: [re.compile(p) for p in pats] for cat, pats in PATTERNS.items()}


def _normalize(text: str) -> str:
    """NFC 정규화 후 모든 공백류를 제거한다."""
    t = unicodedata.normalize("NFC", str(text or ""))
    return re.sub(r"\s+", "", t)


def match_category(text: str) -> str | None:
    """걸린 범주 이름. 걸리지 않으면 None.

    ⚠️ 어떤 키워드에 걸렸는지는 돌려주지 않는다. 로그에 남기지 않기 위해서다
    (CONTRACT §5 — 참가자 보호).
    """
    t = _normalize(text)
    if not t:
        return None
    for cat, pats in _COMPILED.items():
        for p in pats:
            if p.search(t):
                return cat
    return None


def is_excluded(text: str) -> bool:
    return match_category(text) is not None


if __name__ == "__main__":
    hits = ["요즘 죽고 싶어요", "자해를 한 적이 있어요", "다 죽여버리고 싶어요",
            "소송이 진행 중이에요", "지금 위험한 것 같아요"]
    misses = ["배고파 죽겠어요", "피곤해 죽겠네요", "숙제 때문에 죽을 맛이에요",
              "고소공포증이 있어요", "요즘 일이 너무 많아서 지쳐요"]
    for t in hits:
        print(f"{'OK ' if is_excluded(t) else 'MISS'}  {t}")
    for t in misses:
        print(f"{'OK ' if not is_excluded(t) else 'FALSE POSITIVE'}  {t}")
