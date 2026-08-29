#!/usr/bin/env python3
"""시스템 프롬프트를 조합하고 SHA-256을 출력한다.

앱은 이 스크립트가 만드는 문자열을 그대로 system prompt로 쓰고,
출력된 해시를 세션 로그의 prompt_sha256 필드에 남긴다.

사용:
    python3 prompts/build_prompts.py                 # 해시 표
    python3 prompts/build_prompts.py --emit context_a  # 프롬프트 본문 출력
    python3 prompts/build_prompts.py --json          # 앱이 읽을 JSON
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
JOINER = "\n\n"

# prompts.yaml의 composition 블록을 그대로 옮긴 것.
# (PyYAML 의존을 피하기 위해 여기서 한 번 더 선언한다.
#  yaml을 고치면 이 표도 같이 고친다 — verify_matches_yaml()이 검사한다.)
COMPOSITION: dict[str, list[str]] = {
    "context_a": ["system_common.txt", "system_safety.txt", "system_context_a.txt"],
    "context_b": ["system_common.txt", "system_safety.txt", "system_context_b.txt"],
    "context_b_L2": ["system_common.txt", "system_safety.txt", "system_context_b_L2.txt"],
}


def build(key: str) -> str:
    parts = []
    for name in COMPOSITION[key]:
        path = HERE / name
        if not path.exists():
            raise FileNotFoundError(f"프롬프트 조각이 없습니다: {path}")
        parts.append(path.read_text(encoding="utf-8").strip())
    return JOINER.join(parts) + "\n"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_version() -> str:
    yaml_path = HERE / "prompts.yaml"
    if not yaml_path.exists():
        return "unknown"
    m = re.search(r'^version:\s*"?([^"\n]+)"?', yaml_path.read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else "unknown"


def verify_matches_yaml() -> list[str]:
    """prompts.yaml의 composition과 COMPOSITION 표가 어긋나면 경고를 낸다."""
    yaml_path = HERE / "prompts.yaml"
    if not yaml_path.exists():
        return ["prompts.yaml 없음 — composition 대조 생략"]
    text = yaml_path.read_text(encoding="utf-8")
    warnings = []
    for key, files in COMPOSITION.items():
        block = re.search(
            rf"^\s{{2}}{re.escape(key)}:\s*\n((?:\s{{4}}-\s.*\n)+)", text, re.M
        )
        if not block:
            warnings.append(f"prompts.yaml에 composition.{key}가 없습니다")
            continue
        listed = re.findall(r"-\s*([^\s#]+)", block.group(1))
        if listed != files:
            warnings.append(
                f"composition.{key} 불일치: yaml={listed} / build_prompts.py={files}"
            )
    return warnings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--emit", choices=sorted(COMPOSITION), help="프롬프트 본문을 출력")
    ap.add_argument("--json", action="store_true", help="앱이 읽을 JSON으로 출력")
    args = ap.parse_args()

    if args.emit:
        sys.stdout.write(build(args.emit))
        return 0

    version = read_version()
    built = {k: build(k) for k in COMPOSITION}

    if args.json:
        payload = {
            "prompt_version": version,
            "prompts": {
                k: {"sha256": sha256(v), "chars": len(v), "text": v}
                for k, v in built.items()
            },
        }
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"prompt_version: {version}\n")
    print(f"{'key':<16}{'chars':>7}  sha256")
    print("-" * 80)
    for key, text in built.items():
        print(f"{key:<16}{len(text):>7}  {sha256(text)}")

    hashes = {k: sha256(v) for k, v in built.items()}
    print()
    if hashes["context_a"] == hashes["context_b"]:
        print("✗ 맥락 A와 B의 프롬프트가 동일합니다 — 맥락 조작이 성립하지 않습니다.")
        return 1
    print("✓ 맥락 A / B 프롬프트가 서로 다릅니다.")
    print("  (지연 조건 3수준은 프롬프트를 공유하므로 같은 맥락 안에서 해시가 같아야 합니다.)")

    for w in verify_matches_yaml():
        print(f"⚠ {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
