"""prompts.yaml 로더 — 표준 라이브러리만 사용한다.

PyYAML을 쓰지 않으므로 완전한 YAML 파서가 아니다. prompts.yaml이 쓰는
부분집합만 다룬다: 들여쓰기 맵, 리스트, 인라인 플로우 맵, 주석, 스칼라.
파일 구조가 바뀌면 여기도 바꾼다.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "prompts" / "prompts.yaml"


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _scalar(tok: str):
    tok = tok.strip()
    if not tok:
        return None
    if tok[0] == tok[-1] and tok[0] in "\"'" and len(tok) >= 2:
        return tok[1:-1]
    low = tok.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"[+-]?\d+", tok):
        return int(tok)
    if re.fullmatch(r"[+-]?\d*\.\d+([eE][+-]?\d+)?", tok):
        return float(tok)
    return tok


def _flow_map(body: str) -> dict:
    out = {}
    for part in re.split(r",(?![^{]*\})", body):
        if not part.strip():
            continue
        k, _, v = part.partition(":")
        out[k.strip()] = _scalar(v)
    return out


def _parse_block(lines, i: int, indent: int):
    """lines[i:] 를 indent 수준의 맵/리스트로 파싱한다. (값, 다음 인덱스)를 돌려준다."""
    # 리스트인지 먼저 본다
    if i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
        items = []
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            items.append(_scalar(lines[i][1][2:]))
            i += 1
        return items, i

    out = {}
    while i < len(lines):
        col, text = lines[i]
        if col < indent:
            break
        if col > indent:                      # 방어적 — 정상 입력에서는 오지 않는다
            i += 1
            continue
        key, _, rest = text.partition(":")
        key, rest = key.strip(), rest.strip()
        i += 1
        if rest.startswith("{") and rest.endswith("}"):
            out[key] = _flow_map(rest[1:-1])
        elif rest:
            out[key] = _scalar(rest)
        elif i < len(lines) and lines[i][0] > indent:
            out[key], i = _parse_block(lines, i, lines[i][0])
        else:
            out[key] = None
    return out, i


def load_config(path=DEFAULT_CONFIG) -> dict:
    raw = Path(path).read_text(encoding="utf-8")
    lines = []
    for line in raw.splitlines():
        stripped = _strip_comment(line)
        if not stripped.strip():
            continue
        lines.append((len(stripped) - len(stripped.lstrip()), stripped.strip()))
    cfg, _ = _parse_block(lines, 0, 0)
    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    for key in ("version", "model", "conversation", "delay_placement"):
        if key not in cfg:
            raise ValueError(f"prompts.yaml에 '{key}'가 없습니다")
    if cfg["model"].get("stream") is not False:
        raise ValueError("model.stream은 반드시 false여야 합니다 (CONTRACT P4)")
    if cfg.get("indicator") != "none":
        raise ValueError("indicator는 'none'이어야 합니다 — 화면 표시를 넣으면 해석 단서가 된다")

    dp = cfg["delay_placement"]
    for key in ("deep_ms", "medium_ms", "shallow_ms", "total_per_conversation_ms", "practice_ms"):
        if not isinstance(dp.get(key), int) or dp[key] <= 0:
            raise ValueError(f"delay_placement.{key}가 잘못되었습니다: {dp.get(key)!r}")
    if not (dp["shallow_ms"] < dp["medium_ms"] < dp["deep_ms"]):
        raise ValueError("delay_placement: 얕음 < 보통 < 깊음 이어야 합니다")

    conv = cfg["conversation"]
    turns = conv.get("turns_per_conversation")
    if turns != 9:
        raise ValueError(f"대화당 9턴이어야 합니다 (깊음3·보통3·얕음3): {turns!r}")
    if conv.get("conversations") != 3:
        raise ValueError("대화는 3개여야 합니다 — 3블록이다")

    # ★ 세 조건의 총 지연이 같다는 것이 이 설계의 전제다. 산술로 확인한다.
    expected = 3 * (dp["deep_ms"] + dp["medium_ms"] + dp["shallow_ms"])
    if expected != dp["total_per_conversation_ms"]:
        raise ValueError(
            f"총 지연이 맞지 않습니다: 3×(깊음+보통+얕음)={expected}ms "
            f"vs total_per_conversation_ms={dp['total_per_conversation_ms']}ms")


def scaled_delay_placement(cfg: dict, scale: float) -> dict:
    """E2E 고속 모드용. 세 조건과 연습 지연에 같은 배율을 적용한다.

    ★ min_ms와 max_ms의 하한 clamp가 서로 달라서(1 / 2), 배율이 너무 작으면
      조건들이 같은 구간으로 뭉개진다. 예를 들어 scale=1e-4면 즉시와 중간이
      모두 1~2ms가 되고 긺은 폭이 0이 된다 — 조건 간 대비가 사라진 채로
      축소 실행이 "통과"한다. 뭉개진 범위를 조용히 돌려주지 않고 터뜨린다.
    """
    dp = dict(cfg["delay_placement"])
    if scale == 1.0:
        return dp
    out = dict(dp)
    for key in ("deep_ms", "medium_ms", "shallow_ms", "practice_ms"):
        out[key] = max(1, round(dp[key] * scale))
    out["total_per_conversation_ms"] = 3 * (out["deep_ms"] + out["medium_ms"] + out["shallow_ms"])
    _validate_scaled(out, scale)
    return out


def _validate_scaled(dp: dict, scale: float) -> None:
    """축소 후에도 세 깊이가 구별되는지 확인한다."""
    if not (dp["shallow_ms"] < dp["medium_ms"] < dp["deep_ms"]):
        raise ValueError(
            f"delay_scale={scale}에서 세 깊이의 지연이 뭉개집니다: "
            f"{dp['shallow_ms']}/{dp['medium_ms']}/{dp['deep_ms']}ms — 조작이 사라집니다")


if __name__ == "__main__":
    import json
    print(json.dumps(load_config(), ensure_ascii=False, indent=2))
