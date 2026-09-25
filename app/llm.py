"""LLM 제공자.

mock       — API 키 없이 전체 흐름과 검증 파이프라인을 돌린다. 표준 라이브러리만.
anthropic  — 실제 모델. 공식 SDK(`pip install anthropic`)를 **지연 임포트**한다.
             mock 경로에는 외부 의존성이 전혀 없다.

★ 스트리밍은 어느 경로에서도 쓰지 않는다 (CONTRACT P4).
  응답 전체를 받아 돌려주고, 표시 시점은 프런트엔드가 목표 시각에 결정한다.
"""

from __future__ import annotations

import hashlib
import os
import time

# ── 현재 세대 Claude 모델은 temperature/top_p를 거부한다 (400) ──────────────
# 참고: claude-api 스킬의 Thinking & Effort 표.
_NO_SAMPLING_PREFIXES = (
    "claude-fable-5", "claude-mythos-5",
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-sonnet-5",
)
# effort를 받는 모델 (haiku 계열은 거부한다)
_EFFORT_PREFIXES = _NO_SAMPLING_PREFIXES + ("claude-opus-4-6", "claude-sonnet-4-6")


def supports_temperature(model_id: str) -> bool:
    return not str(model_id).startswith(_NO_SAMPLING_PREFIXES)


def supports_effort(model_id: str) -> bool:
    return str(model_id).startswith(_EFFORT_PREFIXES)


def now_ms() -> int:
    return int(time.time() * 1000)


# ────────────────────────────── mock ──────────────────────────────

# 규칙(prompts/system_common.txt + 깊이 지시문)을 지키는 응답만 넣는다.
# 깊이별 문장 수(깊음 3~4 · 보통 2~3 · 얕음 1~2) · 마지막이 질문 하나 ·
# 이모지/서식/금지어 없음 · 자기 감정 진술 없음.
# 지시된 깊이대로 나오는 mock 응답. 사후 TES 평정 연습에도 쓸 수 있다.
_MOCK = {
    "deep": [
        "말씀하신 '자꾸 미루게 된다'는 부분에서 답답함이 느껴집니다. 하려는 마음이 없어서가 아니라 시작하는 지점에서 막히는 상태처럼 들립니다. 그런 마음이 드는 게 당연합니다. 그 막히는 순간에는 주로 무엇이 떠오르시나요?",
        "'정리가 안 된다'고 하신 게 걸립니다. 할 일이 많아서라기보다 어디부터인지가 안 잡히는 막막함처럼 들립니다. 그런 상태에서는 누구나 지칠 수 있습니다. 그럴 때 가장 먼저 손이 가는 것은 무엇인가요?",
        "말씀 중에 '괜찮은 척했다'는 대목이 있었습니다. 그 자리에서는 서운함을 드러내기 어려우셨던 것 같습니다. 그렇게 참는 게 익숙하신 것 같습니다. 그때 하고 싶었던 말은 무엇이었나요?",
    ],
    "medium": [
        "그 상황에서는 그렇게 느끼실 수 있습니다. 마감보다 착수 기준으로 일을 나눠 보세요. 요즘 가장 먼저 손이 가는 일은 무엇인가요?",
        "혼자 감당하고 계시는 것 같습니다. 주변에 한 사람에게만 먼저 이야기해 보세요. 이야기해 본 적이 있으신가요?",
        "그 상황이 반복되고 있군요. 반복될 때는 기록으로 남기면 패턴이 보입니다. 마지막으로 그랬던 건 언제인가요?",
    ],
    "shallow": [
        "그러시군요. 그게 언제부터였나요?",
        "네. 얼마나 자주 그러신가요?",
        "알겠습니다. 그때는 어디에 계셨나요?",
    ],
}

class MockProvider:
    """검증용 제공자.

    ★ latency_mode='length'는 **생성 시간을 입력 길이에 비례**하게 만든다.
      지연 주입이 잘못 구현되어 있으면(예: 응답 도착 후부터 D초를 세면)
      부과 지연이 입력 길이를 따라가고, manipulation_check.py가 그것을 잡는다.
      검증용 함정이므로 끄지 않는다.
    """

    def __init__(self, config: dict, latency_mode: str = "length", latency_scale: float = 1.0):
        self.model = f"mock-{latency_mode}"
        self.latency_mode = latency_mode
        # E2E 고속 모드에서 지연 조건을 배율로 줄이면 mock의 생성 시간도 같은
        # 배율로 줄여야 한다. 그러지 않으면 축소된 즉시 조건에서 항상 초과가
        # 나서 조작 실패가 아닌 '테스트 설정 실패'가 된다.
        self.latency_scale = float(latency_scale)
        self.max_tokens = int(config["model"]["max_tokens"])
        self.calls: list[dict] = []          # 테스트가 들여다본다

    def _latency_ms(self, system: str, messages: list[dict]) -> int:
        text = messages[-1]["content"] if messages else ""
        h = hashlib.sha256((text + self.latency_mode).encode("utf-8")).digest()
        jitter = int.from_bytes(h[:2], "big") % 200
        if self.latency_mode == "fixed":
            base = 600 + jitter
        elif self.latency_mode == "slow":
            base = 4000 + jitter   # 얕음(3000) 초과 · 깊음(15000) 미만
        else:
            base = 280 + 3 * len(text) + jitter    # 'length' — 입력 길이 비례
        return max(1, round(base * self.latency_scale))

    def complete(self, system: str, messages: list[dict], depth: str | None = None) -> dict:
        request_ts = now_ms()
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        latency = self._latency_ms(system, messages)
        time.sleep(latency / 1000.0)
        # ★ 깊이는 문자열 매칭으로 추측하지 않는다. 서버가 turn_plan에서 정한
        #   값을 그대로 받는다 (공통 프롬프트에 "깊음" 단어가 있어 오판한 사고).
        if depth in _MOCK:
            key = depth
        else:
            key = ("deep" if "깊음" in system else
                   "shallow" if "얕음" in system else "medium")
        pool = _MOCK[key]
        turn = sum(1 for m in messages if m["role"] == "user")
        text = pool[(turn - 1) % len(pool)]
        return {
            "text": text,
            "finish_reason": "stop",
            "request_ts": request_ts,
            "response_ts": now_ms(),
            "model": self.model,
        }


# ──────────────────────────── anthropic ────────────────────────────

class AnthropicProvider:
    """공식 SDK 경유. 스트리밍을 쓰지 않는다."""

    def __init__(self, config: dict, latency_mode: str = "length"):
        try:
            import anthropic  # noqa: F401  지연 임포트
        except ImportError as e:
            raise RuntimeError(
                "anthropic SDK가 없습니다. `pip install anthropic` 후 다시 실행하세요. "
                "(mock 제공자는 의존성 없이 동작합니다)"
            ) from e
        import anthropic
        self._sdk = anthropic
        self._client = anthropic.Anthropic()
        m = config["model"]
        self.model = str(m["id"])
        if self.model in ("", "TBD"):
            raise RuntimeError(
                "prompts.yaml의 model.id가 아직 'TBD'입니다. "
                "실제 모델 ID를 박아 넣고 본 실험을 시작하세요."
            )
        self.max_tokens = int(m["max_tokens"])
        self.temperature = m.get("temperature")
        self.effort = m.get("effort", "low")
        self.omitted_params: list[str] = []
        if self.temperature is not None and not supports_temperature(self.model):
            # 현재 세대 모델은 temperature를 400으로 거부한다. 조용히 빼고 기록한다.
            self.omitted_params.append("temperature")

    def complete(self, system: str, messages: list[dict], depth: str | None = None) -> dict:
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        }
        if self.temperature is not None and supports_temperature(self.model):
            kwargs["temperature"] = float(self.temperature)
        if supports_effort(self.model):
            # 생성 시간을 줄이기 위해 낮은 effort를 쓴다. 즉시 조건의 실행
            # 가능성이 여기에 달려 있다 (materials/04 §6).
            kwargs["output_config"] = {"effort": self.effort}

        request_ts = now_ms()
        resp = self._client.messages.create(**kwargs)
        response_ts = now_ms()

        if getattr(resp, "stop_reason", None) == "refusal":
            text = ""
            finish = "refusal"
        else:
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            finish = "length" if resp.stop_reason == "max_tokens" else "stop"
        return {
            "text": text.strip(),
            "finish_reason": finish,
            "request_ts": request_ts,
            "response_ts": response_ts,
            "model": self.model,
        }


def make_provider(name: str, config: dict, latency_mode: str = "length",
                  latency_scale: float = 1.0):
    if name == "mock":
        return MockProvider(config, latency_mode, latency_scale)
    if name == "anthropic":
        return AnthropicProvider(config, latency_mode)
    raise ValueError(f"알 수 없는 제공자: {name!r}")


if __name__ == "__main__":
    from config import load_config
    cfg = load_config()
    p = make_provider("mock", cfg)
    out = p.complete("정서 표현은 다음 범위 안에서만", [{"role": "user", "content": "가" * 100}])
    print(out["model"], out["response_ts"] - out["request_ts"], "ms")
    print(out["text"])
    for mid in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-sonnet-4-6"):
        print(f"{mid:<22} temperature={supports_temperature(mid)}  effort={supports_effort(mid)}")
