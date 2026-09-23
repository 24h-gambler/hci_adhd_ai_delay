#!/usr/bin/env python3
"""실험 웹앱 서버 (표준 라이브러리만).

    python3 app/server.py --port 8000 --provider mock

CONTRACT.md §2의 API를 구현한다. 정적 파일은 app/static/에서 서빙한다.
"""

from __future__ import annotations

import argparse
import json
import os
import mimetypes
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
        self.placement = cfgmod.scaled_delay_placement(cfg, self.delay_scale)
        self.turns_per_conversation = int(cfg["conversation"]["turns_per_conversation"])
        self.conversations = int(cfg["conversation"]["conversations"])
        self.reset_history = bool(
            cfg["conversation"].get("reset_history_between_conversations", True))
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        # 기반은 R1/R2/R3에서 완전히 동일하다. 턴마다 깊이 지시만 덧붙는다.
        self._base = build_prompts.build("base")
        self._base_hash = build_prompts.sha256(self._base)
        self._system = {d: build_prompts.system_for(d) for d in sched.DEPTHS}
        self._system_hash = {d: build_prompts.sha256(v) for d, v in self._system.items()}

    # ── 세션 ──
    def start_session(self, participant_id: str, group: str,
                      test_mode: bool = False) -> dict:
        plan = sched.session_plan(participant_id)
        sid = sched.make_session_id(participant_id, now_ms())
        with self._lock:
            self._sessions[sid] = {
                "session_id": sid,
                "participant_id": participant_id,
                "group": group,
                # ★ 테스트 패널(?test=1)로 시작한 세션. 모든 레코드에 찍혀 나간다.
                #   주석의 약속("실세션에서는 쓰지 않는다")만으로는 자동주행·설문
                #   자동채우기가 만든 로그가 진짜 참가자 로그와 구분되지 않는다.
                "test_mode": bool(test_mode),
                "plan": plan,
                "history": {},          # conversation_index -> [messages]
                "alerts": [],
            }
        m = self.cfg["model"]
        return {
            "session_id": sid,
            "participant_number": plan["participant_number"],
            "condition_order": plan["condition_order"],
            "conversations": plan["conversations"],
            "turns_per_conversation": self.turns_per_conversation,
            "test_mode": bool(test_mode),
            "prompt_version": self.cfg["version"],
            "base_prompt_sha256": self._base_hash,
            "model": self.provider.model,
            "temperature": m.get("temperature"),
            "max_tokens": m.get("max_tokens"),
            "delay_scale": self.delay_scale,
            "delay_placement": self.placement,
            "indicator": self.cfg.get("indicator", "none"),
        }

    def _conversation_meta(self, sess: dict, conv_index: int) -> dict:
        if int(conv_index) == sched.PRACTICE_CONVERSATION_INDEX:
            return {"index": 0, "condition": "practice"}
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
            # 서버리스에서는 인스턴스가 매번 새로 뜬다. 계획은 시드 결정론이라
            # session_id만으로 복원할 수 있고, 이력은 클라이언트가 보낸다.
            sess = self._restore_session(sid, body)
        meta = self._conversation_meta(sess, conv)
        practice = conv == sched.PRACTICE_CONVERSATION_INDEX

        # ★ 깊이와 D를 텍스트를 보지 않고 (세션, 대화, 턴)만으로 정한다 (CONTRACT P1).
        tp = sched.turn_plan(sid, conv, meta["condition"], turn_index, self.placement)
        depth, target = tp["depth"], tp["target_delay_ms"]
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
            result = self.provider.complete(self._system[depth], messages)
            with self._lock:
                sess["history"].setdefault(conv, []).append({"role": "user", "content": text})
                sess["history"][conv].append({"role": "assistant", "content": result["text"]})
            safety_flag, bypass = False, False

        m = self.cfg["model"]
        record = {
            "session_id": sid,
            "participant_id": sess["participant_id"], "group": sess["group"],
            "conversation_index": conv,
            "condition": meta["condition"], "depth": depth,
            "turn_index": turn_index, "practice": practice,
            "user_input_start_ts": start_ts, "user_input_submit_ts": submit_ts,
            "user_input_text": text, "user_input_chars": len(text),
            # ★ 테스트 패널로 시작한 세션이면 모든 턴에 남는다 (조작 점검기가 거른다).
            "test_mode": bool(sess.get("test_mode")),
            "queued_during_wait": bool(body.get("queued_during_wait", False)),
            "target_delay_ms": target,
            "llm_request_ts": result["request_ts"], "llm_response_ts": result["response_ts"],
            "ai_response_text": result["text"], "ai_response_chars": len(result["text"]),
            "safety_flag": safety_flag,
            "manipulation_ok": bool(not safety_flag and not practice
                                    and result["response_ts"] <= deadline),
            "prompt_version": self.cfg["version"],
            "base_prompt_sha256": self._base_hash,
            "prompt_sha256": self._system_hash[depth],
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
            "condition": meta["condition"], "depth": depth,
            "base_prompt_sha256": self._base_hash,
            "prompt_sha256": self._system_hash[depth], "model": result["model"],
            "turns_per_conversation": self.turns_per_conversation,
        }

    def _restore_session(self, sid: str, body: dict) -> dict:
        """메모리에 없는 세션을 session_id에서 복원한다 (무상태 배포용)."""
        pid = str(sid).rsplit("-", 1)[0]
        try:
            plan = sched.session_plan(pid)
        except ValueError as e:
            raise KeyError(f"알 수 없는 session_id: {sid}") from e
        sess = {
            "session_id": sid, "participant_id": pid,
            "group": body.get("group", "unspecified"),
            "plan": plan, "history": {}, "alerts": [], "restored": True,
            "test_mode": bool(body.get("test_mode")),
        }
        conv = int(body.get("conversation_index", 0))
        # 클라이언트가 보낸 이력을 그 대화에만 넣는다 (대화 간 격리는 유지)
        hist = body.get("history") or []
        sess["history"][conv] = [
            {"role": m.get("role"), "content": str(m.get("content", ""))}
            for m in hist if m.get("role") in ("user", "assistant")
        ]
        with self._lock:
            self._sessions[sid] = sess
        return sess

    def _history_for(self, sess: dict, conv: int) -> list[dict]:
        """★ 대화 간 이력 격리 (CONTRACT P6). conv 키가 다르면 서로 섞이지 않는다."""
        with self._lock:
            if not self.reset_history:
                merged = []
                for k in sorted(sess["history"]):
                    merged.extend(sess["history"][k])
                return merged
            return list(sess["history"].get(conv, []))

    def stamp_test_mode(self, record: dict) -> dict:
        """설문·이벤트 레코드에 세션의 test_mode 를 찍는다.

        본문이 보내온 값을 그대로 믿지 않는다. 메모리에 세션이 있으면 그것이
        기준이고, 없으면(서버리스 콜드 스타트) 본문 값을 쓴다.
        """
        with self._lock:
            sess = self._sessions.get(record.get("session_id"))
        record["test_mode"] = bool(sess["test_mode"]) if sess else bool(record.get("test_mode"))
        return record

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

# 배포본 식별자. 응답 헤더 X-Server-Build 로 나간다.
SERVER_BUILD = "2026-09-23.testmode1"

_FALLBACK_EXP = None
_FALLBACK_LOCK = threading.Lock()


def default_log_dir() -> Path:
    """쓸 수 있는 로그 디렉터리를 스스로 고른다.

    ★ 서버리스에서는 배포된 파일 트리가 읽기 전용이라 REPO_ROOT/logs 에
      쓸 수 없다 (OSError: Read-only file system: '/var/task/logs').
      쓸 수 있는 곳은 /tmp 뿐이다.

      진입 파일(api/index.py)이 HCI_LOG_DIR 을 넣어 주길 기대하지 않는다.
      그 파일의 모듈 본문이 실행되지 않는 환경이 있다는 것을 확인했다 —
      exp 가 매달리지 않은 것과 같은 뿌리다.
    """
    env = os.environ.get("HCI_LOG_DIR")
    if env:
        return Path(env)
    local = REPO_ROOT / "logs"
    try:
        local.mkdir(parents=True, exist_ok=True)
        probe = local / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return local
    except OSError:
        return Path("/tmp/hci-logs")


def default_experiment() -> "Experiment":
    """실행 환경이 exp 를 매달아 주지 않았을 때 쓰는 기본 실험 객체.

    ★ 서버리스에서 필요하다. 진입 파일(api/index.py)이 클래스 속성으로
      exp 를 매다는 방식이 Vercel 에서 먹히지 않았다 — self.exp 가 None 이라
      /api/session/start 가 전부 500 이었다. 화면과 /api/health 는 exp 를
      건드리지 않아 멀쩡해 보였고, 그래서 배포가 정상으로 읽혔다.

      라우팅 때와 같은 교훈이다: 진입 파일은 빌드 캐시에 얼어붙을 수 있고
      실행 방식도 환경마다 다르다. app/ 아래 코드가 스스로 설 수 있어야 한다.
    """
    global _FALLBACK_EXP
    with _FALLBACK_LOCK:
        if _FALLBACK_EXP is None:
            cfg = cfgmod.load_config()
            scale = float(os.environ.get("HCI_DELAY_SCALE", "1.0"))
            prov = llmmod.make_provider(
                os.environ.get("HCI_PROVIDER", "mock"), cfg,
                os.environ.get("HCI_MOCK_LATENCY", "fixed"), scale)
            store = TurnStore(default_log_dir())
            _FALLBACK_EXP = Experiment(cfg, prov, store, scale)
        return _FALLBACK_EXP


class Handler(BaseHTTPRequestHandler):
    server_version = "hci-adhd-delay/0.1"
    exp: Experiment = None       # 클래스 속성으로 주입

    @property
    def experiment(self) -> "Experiment":
        """exp 가 매달려 있으면 그것, 아니면 모듈 기본값.

        build_server 는 세션마다 만든 exp 를 클래스에 매단다(로컬 실행).
        그게 없는 실행 환경(서버리스)에서는 기본값이 대신 선다.
        """
        exp = self.exp
        return exp if exp is not None else default_experiment()

    def log_message(self, fmt, *a):            # 조용히
        # self.server 는 실행 환경마다 다르다. 서버리스 어댑터가 띄우는 서버에는
        # verbose 가 없어서 곧이곧대로 읽으면 send_response 안에서 터진다.
        if getattr(self.server, "verbose", False):
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
    def _unrouted(self, path: str):
        """라우터가 모르는 API 경로. 정적 HTML 로 조용히 덮지 않는다."""
        return self._send(404, {
            "error": "unknown_api_route",
            "route_path": path,
            "raw_path": self.path,
            "server_build": SERVER_BUILD,
        })

    def end_headers(self):
        """어떤 응답이든 어느 판본이 만들었는지 헤더로 남긴다.
        배포본을 두드렸을 때 '지금 도는 코드가 방금 올린 코드인지'를
        로그를 뒤지지 않고 응답 하나로 확인할 수 있다."""
        try:
            self.send_header("X-Server-Build", SERVER_BUILD)
        except Exception:                      # noqa: BLE001  진단이 응답을 깨선 안 된다
            pass
        super().end_headers()

    def route_path(self) -> str:
        """라우팅에 쓸 경로.

        서버리스(Vercel)에서는 rewrite 가 함수에 도착하는 경로를 함수 자신의
        경로(/api/index)로 바꿔 버린다. 그래서 vercel.json 이 원래 경로를
        __p 쿼리로 같이 넘기고, 여기서 그것을 되살린다.

        ★ 이 로직은 일부러 기반 클래스에 둔다. Vercel 은 함수 진입 파일의
          바이트코드를 빌드 캐시에 얹어 재사용하기 때문에 api/index.py 가
          첫 배포본에 얼어붙을 수 있다. 실제로 그 일이 일어났다 — 진입
          파일에 넣은 수정이 세 번 연속 배포되지 않았다. app/ 아래 파일은
          매 빌드마다 새로 복사되므로 여기 있는 코드는 항상 최신이다.
        """
        u = urlparse(self.path or "")
        original = parse_qs(u.query).get("__p", [None])[0]
        if original:
            return original if original.startswith("/") else "/" + original
        return u.path

    def _health(self) -> dict:
        """살아 있는지만이 아니라 **실험 객체가 서 있는지**까지 보고한다.

        이 항목이 없어서 /api/health 는 200 인데 /api/session/start 는 전부
        500 인 상태를 '정상 배포'로 읽었다. 배포 확인이 실제 기능을
        건드리게 만든다.
        """
        bound = self.exp is not None
        try:
            exp = self.experiment
            ready = exp is not None and exp.store is not None
        except Exception as e:                   # noqa: BLE001
            return {"ok": False, "server_build": SERVER_BUILD,
                    "handler": type(self).__name__,
                    "exp": "error", "error": f"{type(e).__name__}: {e}"}
        return {"ok": bool(ready), "server_build": SERVER_BUILD,
                "handler": type(self).__name__,
                "exp": "bound" if bound else "fallback"}

    def do_GET(self):
        path = self.route_path()
        m = re.fullmatch(r"/api/session/([^/]+)/plan", path)
        if m:
            try:
                return self._send(200, self.experiment.plan(m.group(1)))
            except KeyError as e:
                return self._send(404, {"error": str(e)})
            except Exception as e:               # noqa: BLE001
                # ★ 여기서 새어 나가면 Vercel 이 FUNCTION_INVOCATION_FAILED 로
                #   덮어 버려서 원인이 응답에 남지 않는다.
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        mt = re.fullmatch(r"/api/session/([^/]+)/turns", path)
        if mt:
            try:
                sid = mt.group(1)
                with self.experiment._lock:
                    known = sid in self.experiment._sessions
                turns = self.experiment.store.session_turns(sid)
                if not known and not turns:
                    raise KeyError(sid)
                return self._send(200, {"turns": turns})
            except KeyError as e:
                return self._send(404, {"error": str(e)})
            except Exception as e:               # noqa: BLE001
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        if path == "/api/health":
            return self._send(200, self._health())
        return self._static(path)

    def do_POST(self):
        path = self.route_path()
        try:
            if path == "/api/session/start":
                b = self._body()
                return self._send(200, self.experiment.start_session(
                    b.get("participant_id", "P00"), b.get("group", "unspecified"),
                    bool(b.get("test_mode"))))
            if path == "/api/turn":
                return self._send(200, self.experiment.turn(self._body()))
            if path == "/api/turn/display":
                b = self._body()
                out = self.experiment.store.complete_display(b["turn_id"], int(b["display_ts"]))
                return self._send(200, {"ok": True, **out})
            if path == "/api/turn/next-input":
                b = self._body()
                self.experiment.store.set_next_input(b["turn_id"], int(b["next_input_start_ts"]))
                return self._send(200, {"ok": True})
            if path == "/api/survey":
                b = self._body()
                self.experiment.store.write_survey(self.experiment.stamp_test_mode(b))
                return self._send(200, {"ok": True})
            if path == "/api/event":
                b = self._body()
                for k in ("session_id", "kind", "ts"):
                    if k not in b or b[k] is None or (isinstance(b[k], str) and not b[k]):
                        raise ValueError(f"missing field: {k}")
                self.experiment.store.write_event(self.experiment.stamp_test_mode(b))
                return self._send(200, {"ok": True})
            m = re.fullmatch(r"/api/session/([^/]+)/end", path)
            if m:
                return self._send(200, self.experiment.store.close_session(m.group(1)))
        except (KeyError, ValueError) as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})
        except Exception as e:                       # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        return self._unrouted(path)

    # ── 정적 파일 ──
    def _static(self, path: str):
        # ★ API 경로는 절대 HTML 로 덮지 않는다.
        #   라우팅이 깨졌을 때 정적 폴백이 200 + HTML 을 돌려주면 배포가
        #   멀쩡해 보인다. 실제로 그렇게 API 가 통째로 죽은 채 지나갔다.
        if path.startswith("/api/") or path == "/api":
            return self._unrouted(path)
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
    store = TurnStore(log_dir or os.environ.get("HCI_LOG_DIR") or (REPO_ROOT / "logs"))
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
        print("delay_scale=%s -- scaled delays, NOT for real sessions." % a.delay_scale)
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
