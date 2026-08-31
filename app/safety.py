"""배제 주제 입력 검사 (CONTRACT §5).

모델의 판단에만 의존하지 않기 위해 앱이 LLM 호출 **전에** 검사한다.

두 가지 오류를 모두 피해야 한다.
  · 놓침(false negative)   — IRB 요건 위반. 위기 상태의 참가자를 AI가 상담하게 된다.
  · 헛발동(false positive) — 그 대화가 통째로 깨지고 데이터 한 점이 사라진다.

★ 공백을 제거하고 매칭하면 안 된다.
  "혼자 해결하고" → "혼자해결하고" 에는 '자해'가 들어 있다.
  "각자 살길" → "각자살길" 에는 '자살'이 들어 있다.
  그래서 원문에 대해 매칭하고, 띄어쓰기가 흔들릴 수 있는 **구(phrase)**
  에만 패턴 안에서 \\s* 를 허용한다. 한 낱말짜리 어휘(자해·자살·마약)는
  공백을 허용하지 않고, 알려진 오탐에는 전후 문맥 조건을 건다.

한국어 관용구도 문제다. "배고파 죽겠어요", "죽을 맛이에요"는 위기 신호가
아니다. 그래서 '죽'이 아니라 '죽고 싶다' 계열만 잡는다.
"""

from __future__ import annotations

import re
import unicodedata

SAFETY_REPLY = "이 부분은 연구자와 이야기하시는 것이 좋겠습니다."

PATTERNS: dict[str, list[str]] = {
    "self_harm": [
        # 자살 사고 — '죽' 관용구를 피해 '죽고 싶다' 계열만
        r"죽고\s*싶", r"죽고\s*파", r"죽어\s*버리고\s*싶", r"죽어야겠",
        # '혼자 살아요' / '각자 살길' 이 붙어 쓰이는 경우를 배제
        r"(?<![혼각])자살(?![아어])",
        r"목숨을?\s*끊", r"극단적\s*선택",
        r"살고\s*싶지\s*않", r"살\s*이유가\s*없", r"살기\s*싫",
        r"사라지고\s*싶", r"없어지고\s*싶", r"세상을\s*뜨",
        # 자해 — '혼자 해결' / '각자 해보' 를 배제
        r"(?<![혼각])자해",
        r"손목(을|에)?\s*(긋|그어|그었|그은)", r"칼로\s*(긋|그어|그었)",
        r"유서",
        # 과량 복용
        r"과다\s*복용", r"약[^.!?]{0,8}(한꺼번에|왕창|전부|모두)[^.!?]{0,6}먹",
        r"수면제[^.!?]{0,8}(먹|삼키)",
    ],
    "harm_others": [
        r"죽이고\s*싶", r"죽여\s*버리", r"때려\s*죽",
        r"때리고\s*싶", r"없애\s*버리고\s*싶",
        # '일을 해치우다'(끝내다)를 배제
        r"해치(?!우)(고|는|려|겠|기)",
    ],
    "crime_legal": [
        # '마약김밥'은 광장시장 음식 이름이다
        r"마약(?!김밥|떡볶이|옥수수|계란|토스트|베개|방석|같)",
        r"훔치|훔친|훔쳐|훔쳤",
        r"절도", r"폭행(을|당|했|한|이)", r"성폭",
        r"전과(가|는|를|자|\s)", r"고소(당|를|했|장)", r"고발당",
        r"소송(중|을|이|에)", r"구속(되|됐|당)",
        r"경찰\s*조사", r"처벌받",
    ],
    "acute_crisis": [
        r"공황\s*발작", r"숨이\s*안\s*쉬", r"숨을?\s*못\s*쉬",
        r"응급실", r"(?<!\d)119(?!\d)",
        r"지금\s*위험", r"버틸\s*수(가)?\s*없",
    ],
}

_COMPILED = {cat: [re.compile(p) for p in pats] for cat, pats in PATTERNS.items()}


def _normalize(text: str) -> str:
    """NFC 정규화만 한다. ★ 공백은 제거하지 않는다 (모듈 설명 참조)."""
    return unicodedata.normalize("NFC", str(text or ""))


def match_category(text: str) -> str | None:
    """걸린 범주 이름. 걸리지 않으면 None.

    ⚠️ 어떤 키워드에 걸렸는지는 돌려주지 않는다. 로그에 남기지 않기 위해서다
    (CONTRACT §5 — 참가자 보호).
    """
    t = _normalize(text)
    if not t.strip():
        return None
    for cat, pats in _COMPILED.items():
        for p in pats:
            if p.search(t):
                return cat
    return None


def is_excluded(text: str) -> bool:
    return match_category(text) is not None
