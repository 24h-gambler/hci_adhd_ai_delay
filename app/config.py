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
    for key in ("version", "empathy_variant", "model", "conversation", "delay_conditions"):
        if key not in cfg:
            raise ValueError(f"prompts.yaml에 '{key}'가 없습니다")
    if cfg["empathy_variant"] not in ("A", "B", "C"):
        raise ValueError(f"empathy_variant는 A/B/C 중 하나여야 합니다: {cfg['empathy_variant']!r}")
    if cfg["model"].get("stream") is not False:
        raise ValueError("model.stream은 반드시 false여야 합니다 (CONTRACT P4)")
    dc = cfg["delay_conditions"]
    for cond in ("immediate", "medium", "long"):
        if cond not in dc:
            raise ValueError(f"delay_conditions에 '{cond}'가 없습니다")
        lo, hi = dc[cond]["min_ms"], dc[cond]["max_ms"]
        if not (isinstance(lo, int) and isinstance(hi, int) and lo < hi):
            raise ValueError(f"{cond} 범위가 잘못되었습니다: {lo}~{hi}")
    order = ["immediate", "medium", "long"]
    for a, b in zip(order, order[1:]):
        if dc[a]["max_ms"] >= dc[b]["min_ms"]:
            raise ValueError(f"{a}와 {b}의 지연 범위가 겹칩니다")
    if "practice" not in dc or "fixed_ms" not in dc["practice"]:
        raise ValueError("delay_conditions.practice.fixed_ms가 없습니다")


def scaled_delay_conditions(cfg: dict, scale: float) -> dict:
    """E2E 고속 모드용. 세 조건과 연습 지연에 같은 배율을 적용한다."""
    if scale == 1.0:
        return cfg["delay_conditions"]
    dc = {}
    for cond, rng in cfg["delay_conditions"].items():
        if cond == "practice":
            dc[cond] = {"fixed_ms": max(1, round(rng["fixed_ms"] * scale))}
        else:
            dc[cond] = {"min_ms": max(1, round(rng["min_ms"] * scale)),
                        "max_ms": max(2, round(rng["max_ms"] * scale))}
    return dc


if __name__ == "__main__":
    import json
    print(json.dumps(load_config(), ensure_ascii=False, indent=2))
