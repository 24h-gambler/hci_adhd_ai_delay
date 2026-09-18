"""조건 배정과 지연 배치 — 지연 배치 3조건 설계 (R1 / R2 / R3).

재설계 근거: `docs/spec/발표대본_7분_v1.pdf` SLIDE 9·10
  · 대화 3개(블록). "6개"가 아니다.
  · 대화 하나당 9턴 = 깊은 답 3 · 보통 3 · 얕은 답 3
  · 세 조건의 지연 다중집합이 같다 {15,15,15,8,8,8,3,3,3} → 총 78초
  · 다른 것은 "깊은 답에 무엇이 붙느냐"뿐이다

      R1  깊음←15s  보통←8s  얕음←3s    (이루다·Jiang 방식)
      R2  깊음←3s   보통←8s  얕음←15s   (반대 조건)
      R3  같은 9개 지연을 깊이와 무관하게 무작위 배치 (규칙 없음)

★ CONTRACT P1은 그대로다. 지연은 (세션, 대화, 턴)의 함수이고
  사용자 입력 텍스트의 함수가 아니다. draw_delay_ms는 텍스트를 받지 않는다.
  달라진 점: 이제 지연은 그 턴에 **지시된 공감 깊이**의 함수이기도 하다.
  깊이는 입력 내용이 아니라 미리 정해진 순열에서 나오므로 독립성은 유지된다.
"""

from __future__ import annotations

import hashlib
import itertools
import re

CONDITIONS = ["R1", "R2", "R3"]
PERMS = [list(p) for p in sorted(itertools.permutations(CONDITIONS))]

DEPTHS = ["deep", "medium", "shallow"]
DEPTH_KO = {"deep": "깊음", "medium": "보통", "shallow": "얕음"}
TURNS_PER_CONVERSATION = 9
CONVERSATIONS = 3
PRACTICE_CONVERSATION_INDEX = 0

# 한 대화의 지연 다중집합. 세 조건에서 동일하다.
DELAY_MS = {"deep": 15000, "medium": 8000, "shallow": 3000}
TOTAL_DELAY_MS = 3 * (DELAY_MS["deep"] + DELAY_MS["medium"] + DELAY_MS["shallow"])  # 78000

# 깊이 → 지연 (R3은 매핑이 아니라 무작위 배치라 여기 없다)
PLACEMENT = {
    "R1": {"deep": 15000, "medium": 8000, "shallow": 3000},
    "R2": {"deep": 3000, "medium": 8000, "shallow": 15000},
}


def participant_number(participant_id: str) -> int:
    m = re.search(r"(\d+)", str(participant_id))
    if not m:
        raise ValueError(f"참가자 ID에서 번호를 찾을 수 없습니다: {participant_id!r}")
    return int(m.group(1))


def make_session_id(participant_id: str, start_ts_ms: int) -> str:
    return f"{participant_id}-{int(start_ts_ms)}"


def _rand(session_id: str, *parts) -> float:
    """[0,1) 균등 난수. (세션, …)에만 의존하며 입력 텍스트와 무관하다."""
    seed = "|".join([str(session_id)] + [str(x) for x in parts]).encode("utf-8")
    return int.from_bytes(hashlib.sha256(seed).digest()[:8], "big") / 2 ** 64


def _shuffled(items: list, session_id: str, *parts) -> list:
    """시드 결정론 Fisher-Yates. 같은 시드면 항상 같은 순서."""
    out = list(items)
    for i in range(len(out) - 1, 0, -1):
        j = int(_rand(session_id, *parts, "swap", i) * (i + 1))
        out[i], out[j] = out[j], out[i]
    return out


def condition_order(n: int) -> list[str]:
    """참가자 번호로 세 조건의 제시 순서를 상쇄한다. 6명이면 6순열이 모두 나온다."""
    return list(PERMS[(n - 1) % len(PERMS)])


def session_plan(participant_id: str) -> dict:
    n = participant_number(participant_id)
    order = condition_order(n)
    return {
        "participant_number": n,
        "condition_order": order,
        "conversations": [
            {"index": i + 1, "condition": order[i], "turns": TURNS_PER_CONVERSATION}
            for i in range(CONVERSATIONS)
        ],
    }


def depth_sequence(session_id: str, conversation_index: int) -> list[str]:
    """그 대화 9턴의 지시 깊이 순서. 깊음 3 · 보통 3 · 얕음 3 고정 구성.

    순서는 (세션, 대화)로 시드된 무작위다. 조건과 무관하게 정해지므로
    깊이 순서가 조건과 교락되지 않는다.
    """
    pool = ["deep"] * 3 + ["medium"] * 3 + ["shallow"] * 3
    return _shuffled(pool, session_id, conversation_index, "depth")


def delay_sequence(session_id: str, conversation_index: int, condition: str,
                   placement: dict | None = None) -> list[int]:
    """그 대화 9턴의 목표 지연(ms). 합은 항상 78000."""
    ms = {"deep": DELAY_MS["deep"], "medium": DELAY_MS["medium"], "shallow": DELAY_MS["shallow"]}
    if placement:
        ms = {"deep": placement["deep_ms"], "medium": placement["medium_ms"],
              "shallow": placement["shallow_ms"]}
    depths = depth_sequence(session_id, conversation_index)
    if condition == "R1":
        return [ms[d] for d in depths]
    if condition == "R2":
        flip = {"deep": ms["shallow"], "medium": ms["medium"], "shallow": ms["deep"]}
        return [flip[d] for d in depths]
    if condition == "R3":
        # 같은 9개 지연을 깊이와 무관하게 섞는다.
        pool = [ms[d] for d in ("deep", "medium", "shallow") for _ in range(3)]
        return _shuffled(pool, session_id, conversation_index, "r3")
    raise ValueError(f"알 수 없는 조건: {condition!r}")


def turn_plan(session_id: str, conversation_index: int, condition: str,
              turn_index: int, placement: dict | None = None) -> dict:
    """한 턴의 지시 깊이와 목표 지연."""
    if int(conversation_index) == PRACTICE_CONVERSATION_INDEX:
        practice = (placement or {}).get("practice_ms", PRACTICE_DELAY_MS)
        return {"depth": "medium", "target_delay_ms": int(practice)}
    i = int(turn_index) - 1
    if not (0 <= i < TURNS_PER_CONVERSATION):
        raise ValueError(f"턴 번호 범위를 벗어났습니다: {turn_index}")
    return {
        "depth": depth_sequence(session_id, conversation_index)[i],
        "target_delay_ms": delay_sequence(session_id, conversation_index, condition, placement)[i],
    }


PRACTICE_DELAY_MS = 8000   # 연습은 보통 깊이·보통 지연. 세 조건의 중앙값이라 앵커가 치우치지 않는다.


def draw_delay_ms(session_id: str, conversation_index: int, turn_index: int,
                  condition: str, ranges=None) -> int:
    """★ 텍스트를 인자로 받지 않는다 (CONTRACT P1)."""
    return turn_plan(session_id, conversation_index, condition, turn_index, ranges)["target_delay_ms"]


def coverage_report(n_participants: int = 12) -> dict:
    pos = {c: [0] * CONVERSATIONS for c in CONDITIONS}
    totals = set()
    for n in range(1, n_participants + 1):
        plan = session_plan(f"P{n:02d}")
        sid = make_session_id(f"P{n:02d}", 0)
        for conv in plan["conversations"]:
            pos[conv["condition"]][conv["index"] - 1] += 1
            totals.add(sum(delay_sequence(sid, conv["index"], conv["condition"])))
    return {"position_counts": pos, "conversation_total_delay_ms": sorted(totals)}


if __name__ == "__main__":
    import json
    sid = make_session_id("P01", 1700000000000)
    print("P01 계획:", json.dumps(session_plan("P01"), ensure_ascii=False))
    print()
    for cond in CONDITIONS:
        depths = depth_sequence(sid, 1)
        delays = delay_sequence(sid, 1, cond)
        print(f"  {cond}  깊이 {[DEPTH_KO[d][0] for d in depths]}")
        print(f"      지연 {[d // 1000 for d in delays]}초   합 {sum(delays)//1000}초")
    print()
    print("상쇄:", json.dumps(coverage_report(), ensure_ascii=False))
