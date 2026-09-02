"""조건 배정(상쇄)과 목표 지연 D 추출.

★ 이 모듈이 CONTRACT P1을 구조적으로 보장한다.

D는 sha256(f"{session_id}|{conversation_index}|{turn_index}")를 시드로
뽑는다. 사용자 입력 텍스트가 시드에 들어가지 않으므로 D는 텍스트의
함수일 수 없다. draw_delay_ms는 텍스트를 인자로 받지도 않는다.
"""

from __future__ import annotations

import hashlib
import itertools
import re

CONDITIONS = ["immediate", "medium", "long"]
PERMS = [list(p) for p in sorted(itertools.permutations(CONDITIONS))]
PRACTICE_CONVERSATION_INDEX = 0
CONVERSATIONS_PER_BLOCK = 3
BLOCKS = 2


def participant_number(participant_id: str) -> int:
    """'P07' -> 7. 숫자가 없으면 ValueError."""
    m = re.search(r"(\d+)", str(participant_id))
    if not m:
        raise ValueError(f"참가자 ID에서 번호를 찾을 수 없습니다: {participant_id!r}")
    return int(m.group(1))


def make_session_id(participant_id: str, start_ts_ms: int) -> str:
    return f"{participant_id}-{int(start_ts_ms)}"


def block_order(n: int) -> list[str]:
    """홀수 참가자는 일상 대화(a) 먼저, 짝수는 고민 상담(b) 먼저."""
    return ["a", "b"] if n % 2 == 1 else ["b", "a"]


def condition_orders(n: int) -> list[list[str]]:
    """블록별 조건 순서. 두 블록의 순서는 어떤 n에서도 서로 다르다.

    인덱스가 6을 법으로 항상 3만큼 차이나므로 같은 순열이 될 수 없다.
    """
    i1 = (n - 1) % len(PERMS)
    i2 = (n - 1 + 3) % len(PERMS)
    return [list(PERMS[i1]), list(PERMS[i2])]


def session_plan(participant_id: str) -> dict:
    n = participant_number(participant_id)
    blocks = block_order(n)
    orders = condition_orders(n)
    conversations = []
    idx = 0
    for b in range(BLOCKS):
        for c in range(CONVERSATIONS_PER_BLOCK):
            idx += 1
            conversations.append({
                "index": idx,
                "block": b + 1,
                "context": blocks[b],
                "condition": orders[b][c],
            })
    return {
        "participant_number": n,
        "block_order": blocks,
        "conversations": conversations,
    }


def _unit(session_id: str, conversation_index: int, turn_index: int) -> float:
    """[0, 1) 균등 난수. 입력 텍스트가 아니라 (세션, 대화, 턴)에만 의존한다."""
    seed = f"{session_id}|{int(conversation_index)}|{int(turn_index)}".encode("utf-8")
    digest = hashlib.sha256(seed).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def draw_delay_ms(session_id: str, conversation_index: int, turn_index: int,
                  condition: str, ranges: dict) -> int:
    """목표 지연 D(밀리초).

    ★ 텍스트를 인자로 받지 않는다. 이것이 P1의 구조적 보장이다.
    연습 턴(conversation_index == 0)은 조건과 무관하게 고정값을 쓴다.
    """
    if int(conversation_index) == PRACTICE_CONVERSATION_INDEX:
        return int(ranges["practice"]["fixed_ms"])
    if condition not in CONDITIONS:
        raise ValueError(f"알 수 없는 조건: {condition!r}")
    rng = ranges[condition]
    lo, hi = int(rng["min_ms"]), int(rng["max_ms"])
    return lo + int(_unit(session_id, conversation_index, turn_index) * (hi - lo))


def coverage_report(n_participants: int = 12) -> dict:
    """상쇄가 실제로 균형을 이루는지 확인하는 진단용."""
    counts = {c: [0] * CONVERSATIONS_PER_BLOCK for c in CONDITIONS}
    ctx_first = {"a": 0, "b": 0}
    for n in range(1, n_participants + 1):
        plan = session_plan(f"P{n:02d}")
        ctx_first[plan["block_order"][0]] += 1
        for conv in plan["conversations"]:
            pos = (conv["index"] - 1) % CONVERSATIONS_PER_BLOCK
            counts[conv["condition"]][pos] += 1
    return {"position_counts": counts, "first_context_counts": ctx_first}


if __name__ == "__main__":
    import json
    for pid in ("P01", "P02", "P07"):
        print(pid, json.dumps(session_plan(pid), ensure_ascii=False))
    print("coverage:", json.dumps(coverage_report(), ensure_ascii=False))
