#!/usr/bin/env python3
"""실험 웹앱 서버 (표준 라이브러리만).

    python3 app/server.py --port 8000 --provider mock

CONTRACT.md §2의 API를 구현한다. 정적 파일은 app/static/에서 서빙한다.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(REPO_ROOT / "prompts"))

import build_prompts                    # noqa: E402  프롬프트 조합 + 해시
import config as cfgmod                 # noqa: E402
import llm as llmmod                    # noqa: E402
import safety                           # noqa: E402
import schedule as sched                # noqa: E402
from store import TurnStore             # noqa: E402

STATIC_DIR = APP_DIR / "static"


def now_ms() -> int:
    return int(time.time() * 1000)


class Experiment:
    """세션 상태. 스레드 안전."""

    def __init__(self, cfg, provider, store, delay_scale=1.0):
        self.cfg = cfg
        self.provider = provider
        self.store = store
        self.delay_scale = float(delay_scale)
        self.ranges = cfgmod.scaled_delay_conditions(cfg, self.delay_scale)
        self.variant = cfg["empathy_variant"]
        self.turns_per_conversation = int(cfg["conversation"]["turns_per_conversation"])
        self.reset_history = bool(
            cfg["conversation"].get("reset_history_between_conversations", True))
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self._prompts = {
            "a": build_prompts.build("context_a"),
            "b": build_prompts.build(f"context_b_{self.variant}"),
        }
        self._hashes = {k: build_prompts.sha256(v) for k, v in self._prompts.items()}

    # ── 세션 ──
    def start_session(self, participant_id: str, group: str) -> dict:
        plan = sched.session_plan(participant_id)
        sid = sched.make_session_id(participant_id, now_ms())
        with self._lock:
            self._sessions[sid] = {
                "session_id": sid,
                "participant_id": participant_id,
                "group": group,
                "plan": plan,
                "history": {},          # conversation_index -> [messages]
                "alerts": [],
            }
        m = self.cfg["model"]
        return {
            "session_id": sid,
            "participant_number": plan["participant_number"],
            "block_order": plan["block_order"],
            "conversations": plan["conversations"],
            "turns_per_conversation": self.turns_per_conversation,
            "empathy_variant": self.variant,
            "prompt_version": self.cfg["version"],
            "prompt_sha256": dict(self._hashes),
            "model": self.provider.model,
            "temperature": m.get("temperature"),
            "max_tokens": m.get("max_tokens"),
            "delay_scale": self.delay_scale,
            "delay_conditions": self.ranges,
        }

    def _conversation_meta(self, sess: dict, conv_index: int) -> dict:
        if int(conv_index) == sched.PRACTICE_CONVERSATION_INDEX:
            first_ctx = sess["plan"]["block_order"][0]
            return {"index": 0, "block": 0, "context": first_ctx, "condition": "practice"}
        for c in sess["plan"]["conversations"]:
            if c["index"] == int(conv_index):
                return c
        raise KeyError(f"알 수 없는 conversation_index: {conv_index}")

    # ── 턴 ──
    def turn(self, body: dict) -> dict:
        sid = body["session_id"]
        conv = int(body["conversation_index"])
        turn_index = int(body["turn_index"])
        text = body.get("text", "")
        submit_ts = int(body["user_input_submit_ts"])
        start_ts = int(body.get("user_input_start_ts") or submit_ts)

        with self._lock:
            sess = self._sessions.get(sid)
        if sess is None:
            raise KeyError(f"알 수 없는 session_id: {sid}")
        meta = self._conversation_meta(sess, conv)
        practice = conv == sched.PRACTICE_CONVERSATION_INDEX

        # ★ D는 텍스트를 보지 않고 (세션, 대화, 턴)만으로 뽑는다 (CONTRACT P1).
        target = sched.draw_delay_ms(sid, conv, turn_index, meta["condition"], self.ranges)
        deadline = submit_ts + target

        # ★ 안전 검사는 LLM 호출 **전에** (CONTRACT §5).
        if safety.is_excluded(text):
            with self._lock:
                sess["alerts"].append({"conversation_index": conv, "turn_index": turn_index,
                                       "ts": now_ms()})
            ts = now_ms()
            result = {"text": safety.SAFETY_REPLY, "finish_reason": "stop",
                      "request_ts": ts, "response_ts": ts, "model": self.provider.model}
            safety_flag, bypass = True, True
        else:
            history = self._history_for(sess, conv)
            messages = history + [{"role": "user", "content": text}]
            result = self.provider.complete(self._prompts[meta["context"]], messages)
            with self._lock:
                sess["history"].setdefault(conv, []).append({"role": "user", "content": text})
                sess["history"][conv].append({"role": "assistant", "content": result["text"]})
            safety_flag, bypass = False, False

        m = self.cfg["model"]
        record = {
            "session_id": sid,
            "participant_id": sess["participant_id"], "group": sess["group"],
            "block": meta["block"], "conversation_index": conv,
            "condition": meta["condition"], "context": meta["context"],
            "turn_index": turn_index, "practice": practice,
            "user_input_start_ts": start_ts, "user_input_submit_ts": submit_ts,
            "user_input_text": text, "user_input_chars": len(text),
            "target_delay_ms": target,
            "llm_request_ts": result["request_ts"], "llm_response_ts": result["response_ts"],
            "ai_response_text": result["text"], "ai_response_chars": len(result["text"]),
            "safety_flag": safety_flag,
            "manipulation_ok": bool(not safety_flag and not practice
                                    and result["response_ts"] <= deadline),
            "prompt_version": self.cfg["version"],
            "prompt_sha256": self._hashes[meta["context"]],
            "empathy_variant": self.variant,
            "model": result["model"],
            "temperature": m.get("temperature"), "max_tokens": m.get("max_tokens"),
            "finish_reason": result["finish_reason"],
            "delay_scale": self.delay_scale,
        }
        turn_id = self.store.begin_turn(record)
        return {
            "turn_id": turn_id,
            "target_delay_ms": target, "deadline_ts": deadline,
            "reply": result["text"],
            "llm_request_ts": result["request_ts"], "llm_response_ts": result["response_ts"],
            "finish_reason": result["finish_reason"],
            "safety_flag": safety_flag, "bypass_delay": bypass,
            "condition": meta["condition"], "context": meta["context"],
            "prompt_sha256": self._hashes[meta["context"]], "model": result["model"],
            "turns_per_conversation": self.turns_per_conversation,
        }

    def _history_for(self, sess: dict, conv: int) -> list[dict]:
        """★ 대화 간 이력 격리 (CONTRACT P6). conv 키가 다르면 서로 섞이지 않는다."""
        with self._lock:
            if not self.reset_history:
                merged = []
                for k in sorted(sess["history"]):
                    merged.extend(sess["history"][k])
                return merged
            return list(sess["history"].get(conv, []))

    def plan(self, sid: str) -> dict:
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                raise KeyError(sid)
            return {"session_id": sid, "participant_id": sess["participant_id"],
                    "group": sess["group"], "plan": sess["plan"],
                    "alerts": list(sess["alerts"]),
                    "delay_scale": self.delay_scale}


# ────────────────────────────── HTTP ──────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "hci-adhd-delay/0.1"
    exp: Experiment = None       # 클래스 속성으로 주입

    def log_message(self, fmt, *a):            # 조용히
        if self.server.verbose:
            super().log_message(fmt, *a)

    # ── 유틸 ──
    def _send(self, code: int, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else \
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # ── 라우팅 ──
    def do_GET(self):
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/session/([^/]+)/plan", path)
        if m:
            try:
                return self._send(200, self.exp.plan(m.group(1)))
            except KeyError as e:
                return self._send(404, {"error": str(e)})
        if path == "/api/health":
            return self._send(200, {"ok": True})
        return self._static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/session/start":
                b = self._body()
                return self._send(200, self.exp.start_session(
                    b.get("participant_id", "P00"), b.get("group", "unspecified")))
            if path == "/api/turn":
                return self._send(200, self.exp.turn(self._body()))
            if path == "/api/turn/display":
                b = self._body()
                out = self.exp.store.complete_display(b["turn_id"], int(b["display_ts"]))
                return self._send(200, {"ok": True, **out})
            if path == "/api/turn/next-input":
                b = self._body()
                self.exp.store.set_next_input(b["turn_id"], int(b["next_input_start_ts"]))
                return self._send(200, {"ok": True})
            if path == "/api/survey":
                b = self._body()
                self.exp.store.write_survey(b)
                return self._send(200, {"ok": True})
            m = re.fullmatch(r"/api/session/([^/]+)/end", path)
            if m:
                return self._send(200, self.exp.store.close_session(m.group(1)))
        except (KeyError, ValueError) as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})
        except Exception as e:                       # noqa: BLE001
            self.exp and None
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        return self._send(404, {"error": "not found"})

    # ── 정적 파일 ──
    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            target = STATIC_DIR / "index.html"          # SPA 폴백
        if not target.is_file():
            return self._send(404, {"error": "static not built"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
            ctype += "; charset=utf-8"
        return self._send(200, target.read_bytes(), ctype)


def build_server(port=0, provider="mock", log_dir=None, latency_mode="length",
                 config_path=None, delay_scale=1.0, verbose=False):
    cfg = cfgmod.load_config(config_path or cfgmod.DEFAULT_CONFIG)
    # mock의 생성 시간도 지연 배율을 따라간다 (app/llm.py MockProvider 참조)
    prov = llmmod.make_provider(provider, cfg, latency_mode, delay_scale)
    store = TurnStore(log_dir or (REPO_ROOT / "logs"))
    exp = Experiment(cfg, prov, store, delay_scale)
    handler = type("BoundHandler", (Handler,), {"exp": exp})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.verbose = verbose
    httpd.exp = exp
    httpd.daemon_threads = True
    return httpd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--provider", default="mock", choices=["mock", "anthropic"])
    ap.add_argument("--log-dir", default=str(REPO_ROOT / "logs"))
    ap.add_argument("--mock-latency-mode", default="length", choices=["length", "fixed", "slow"])
    ap.add_argument("--config", default=str(cfgmod.DEFAULT_CONFIG))
    ap.add_argument("--delay-scale", type=float, default=1.0,
                    help="세 조건과 연습 지연에 같은 배율 적용 (E2E 고속 모드). "
                         "1.0이 아니면 본 실험 데이터가 아니다.")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    httpd = build_server(a.port, a.provider, a.log_dir, a.mock_latency_mode,
                         a.config, a.delay_scale, a.verbose)
    host, port = httpd.server_address
    if a.delay_scale != 1.0:
        print(f"⚠️  delay_scale={a.delay_scale} — 축소된 지연입니다. 본 실험용이 아닙니다.")
    print(f"제공자={a.provider}  지연배율={a.delay_scale}  로그={a.log_dir}")
    print(f"http://{host}:{port}/  (연구자 화면: http://{host}:{port}/?researcher=1)")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
