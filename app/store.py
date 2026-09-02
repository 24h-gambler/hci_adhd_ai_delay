"""턴·설문 로그 (JSONL).

`analysis/10-manipulation-check-plan.md §6` 스키마를 그대로 쓴다.
manipulation_check.py가 이 파일을 직접 읽는다.

next_input_start_ts는 **다음 턴이 시작될 때** 채워지므로, 레코드를 메모리에
들고 있다가 변경이 있을 때마다 파일 전체를 다시 쓴다. 세션당 30여 개라
비용이 무시할 수준이고, 파일이 항상 일관된 상태를 유지한다.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

# 로그에 반드시 있어야 하는 필드 (순서 고정 — 사람이 읽기 쉽게)
TURN_FIELDS = [
    "session_id", "participant_id", "group",
    "block", "conversation_index", "condition", "context",
    "turn_index", "practice",
    "user_input_start_ts", "user_input_submit_ts", "user_input_text", "user_input_chars",
    "target_delay_ms",
    "llm_request_ts", "llm_response_ts", "display_ts",
    "ai_response_text", "ai_response_chars",
    "next_input_start_ts",
    "safety_flag", "manipulation_ok",
    "prompt_version", "prompt_sha256", "empathy_variant",
    "model", "temperature", "max_tokens", "finish_reason",
    "delay_scale",
]


class TurnStore:
    def __init__(self, log_dir):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._turns: dict[str, list[dict]] = {}      # session_id -> [record, ...]
        self._index: dict[str, tuple[str, int]] = {}  # turn_id -> (session_id, position)

    # ── 경로 ──
    def turns_path(self, session_id: str) -> Path:
        return self.dir / f"{session_id}.turns.jsonl"

    def surveys_path(self, session_id: str) -> Path:
        return self.dir / f"{session_id}.surveys.jsonl"

    # ── 쓰기 ──
    def _flush(self, session_id: str) -> None:
        """원자적으로 파일 전체를 다시 쓴다."""
        path = self.turns_path(session_id)
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for rec in self._turns.get(session_id, []):
                f.write(json.dumps({k: rec.get(k) for k in TURN_FIELDS},
                                   ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    def begin_turn(self, record: dict) -> str:
        """/api/turn 시점. display_ts는 아직 없다."""
        turn_id = record["turn_id"] = (
            f"{record['session_id']}:{record['conversation_index']}:{record['turn_index']}"
        )
        record.setdefault("display_ts", None)
        record.setdefault("next_input_start_ts", None)
        with self._lock:
            sid = record["session_id"]
            bucket = self._turns.setdefault(sid, [])
            if turn_id in self._index:                 # 같은 턴 재전송 — 덮어쓴다
                _, pos = self._index[turn_id]
                bucket[pos] = record
            else:
                self._index[turn_id] = (sid, len(bucket))
                bucket.append(record)
            self._flush(sid)
        return turn_id

    def _get(self, turn_id: str) -> dict | None:
        loc = self._index.get(turn_id)
        if not loc:
            return None
        sid, pos = loc
        return self._turns[sid][pos]

    def complete_display(self, turn_id: str, display_ts: int) -> dict:
        with self._lock:
            rec = self._get(turn_id)
            if rec is None:
                raise KeyError(f"알 수 없는 turn_id: {turn_id}")
            rec["display_ts"] = int(display_ts)
            deadline = rec["user_input_submit_ts"] + rec["target_delay_ms"]
            rec["manipulation_ok"] = bool(
                not rec["safety_flag"]
                and not rec["practice"]
                and rec["llm_response_ts"] <= deadline
            )
            self._flush(rec["session_id"])
            return {
                "manipulation_ok": rec["manipulation_ok"],
                "display_error_ms": int(display_ts - deadline),
            }

    def set_next_input(self, turn_id: str, ts: int) -> None:
        with self._lock:
            rec = self._get(turn_id)
            if rec is None:
                raise KeyError(f"알 수 없는 turn_id: {turn_id}")
            rec["next_input_start_ts"] = int(ts)
            self._flush(rec["session_id"])

    def write_survey(self, record: dict) -> None:
        with self._lock:
            with self.surveys_path(record["session_id"]).open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def close_session(self, session_id: str) -> dict:
        """미완성 턴을 그대로 두고(display_ts=None) 파일을 확정한다."""
        with self._lock:
            recs = self._turns.get(session_id, [])
            incomplete = sum(1 for r in recs if r.get("display_ts") is None)
            self._flush(session_id)
            return {"turns": len(recs), "incomplete": incomplete,
                    "path": str(self.turns_path(session_id))}
