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

# 규칙(prompts/system_common.txt)을 지키는 응답만 넣는다.
# 2~4문장 · 마지막이 질문 하나 · 이모지/서식/금지어 없음 · 자기 감정 진술 없음.
_MOCK_A = [
    "말씀하신 그 장면이 인상적이셨군요. 어떤 부분이 가장 기억에 남으셨나요?",
    "그 작품을 끝까지 보셨군요. 보시는 동안 특히 눈에 들어온 인물이 있으셨을 것 같습니다. 누구였나요?",
    "그렇게 보셨군요. 처음 볼 때와 다시 볼 때의 인상이 달랐을 수도 있겠습니다. 어떤 점에서 그랬나요?",
    "그 부분을 자세히 말씀해 주셨습니다. 그 장면에서 어떤 생각이 드셨나요?",
]
_MOCK_B = [
    "그러셨군요. 그 일이 언제부터 신경 쓰이기 시작했나요?",
    "그런 상황이셨네요. 말씀을 들어보니 정리가 잘 안 되는 상태로 지내신 것 같습니다. 어떤 부분이 제일 걸리셨나요?",
    "쉽지 않으셨겠어요. 그 상황에서 지금까지 해보신 것이 있다면 무엇이었나요?",
    "상황을 조금 더 여쭙고 싶습니다. 그때 주변에서는 어떤 반응이었나요?",
]


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
            base = 2500 + jitter
        else:
            base = 280 + 3 * len(text) + jitter    # 'length' — 입력 길이 비례
        return max(1, round(base * self.latency_scale))

    def complete(self, system: str, messages: list[dict]) -> dict:
        request_ts = now_ms()
        self.calls.append({"system": system, "messages": [dict(m) for m in messages]})
        latency = self._latency_ms(system, messages)
        time.sleep(latency / 1000.0)
        pool = _MOCK_B if "정서 표현" in system or "신경 쓰이는 일" in system else _MOCK_A
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

    def complete(self, system: str, messages: list[dict]) -> dict:
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
