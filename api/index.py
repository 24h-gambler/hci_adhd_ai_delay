"""Vercel 서버리스 어댑터 — 사용성 검사·데모 전용.

★ 본 실험용이 아니다.
  · 서버리스는 파일 저장이 유지되지 않는다. 기록은 **브라우저가 모아
    세션 끝에 내려받는다** (app/static/app.js: downloadLogs).
  · 콜드 스타트가 수백 ms~수 초 걸릴 수 있어, 얕은 답의 3초 지연이
    깨질 수 있다. 그런 턴은 manipulation_ok=false 로 남는다.
  · 실제 세션은 노트북에서 `python3 app/server.py` 로 돌린다.

Vercel Python 런타임은 `handler`(BaseHTTPRequestHandler 하위 클래스)를 찾는다.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "prompts"))

# 서버리스에서 쓸 수 있는 유일한 쓰기 경로
os.environ.setdefault("HCI_LOG_DIR", "/tmp/hci-logs")

import config as cfgmod          # noqa: E402
import llm as llmmod             # noqa: E402
import server as srv             # noqa: E402
from store import TurnStore      # noqa: E402

_cfg = cfgmod.load_config(ROOT / "prompts" / "prompts.yaml")
_provider = llmmod.make_provider(
    os.environ.get("HCI_PROVIDER", "mock"), _cfg,
    os.environ.get("HCI_MOCK_LATENCY", "fixed"),
    float(os.environ.get("HCI_DELAY_SCALE", "1.0")),
)
_store = TurnStore(os.environ["HCI_LOG_DIR"])
_exp = srv.Experiment(_cfg, _provider, _store,
                      float(os.environ.get("HCI_DELAY_SCALE", "1.0")))


class handler(srv.Handler):        # noqa: N801  Vercel 규약
    exp = _exp

    def route_path(self) -> str:
        """★ Vercel rewrite 는 함수에 도착하는 경로를 /api/index 로 바꾼다.

        그대로 두면 /api/health 같은 요청이 전부 정적 폴백으로 빠져
        HTML 이 돌아온다. vercel.json 이 원래 경로를 __p 로 넘기므로
        그것을 라우팅에 쓴다.
        """
        from urllib.parse import parse_qs, urlparse
        u = urlparse(self.path)
        p = parse_qs(u.query).get("__p", [None])[0]
        if p:
            return p if p.startswith("/") else "/" + p
        return u.path

    def log_message(self, fmt, *a):
        pass
