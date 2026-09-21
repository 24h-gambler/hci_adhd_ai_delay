"""Vercel 서버리스 진입점 — 사용성 검사·데모 전용.

★ 본 실험용이 아니다.
  · 서버리스는 파일 저장이 유지되지 않는다. 기록은 **브라우저가 모아
    세션 끝에 내려받는다** (app/static/app.js: downloadLogs).
  · 콜드 스타트가 수백 ms~수 초 걸릴 수 있어, 얕은 답의 3초 지연이
    깨질 수 있다. 그런 턴은 manipulation_ok=false 로 남는다.
  · 실제 세션은 노트북에서 `python3 app/server.py` 로 돌린다.

★★ 이 파일에는 로직을 넣지 않는다. 아무것도 기대하지 마라.

Vercel 은 함수 진입 파일을 바이트코드로 컴파일해 **빌드 캐시에 얹어
재사용한다** (빌드 로그: "Restored build cache from previous deployment" +
"Compiling Python bytecode"). 그래서 이 파일에 넣은 수정이 배포되지 않고
첫 배포본에 얼어붙는 일이 실제로 일어났다 — 세 번 연속으로.

더 나아가, 배포본을 두드려 보면 **이 파일이 아예 실행되지 않는다.**
  /api/health → {"handler": "Handler", "exp": "fallback"}
`Handler` 는 app/server.py 의 클래스다. 여기서 정의한 handler(소문자)가
아니다. 그래서 여기서 매단 exp 도, os.environ.setdefault 로 넣은
HCI_LOG_DIR 도 서버에 닿지 않았다 — 실험 경로가 전부 500 이었고, 고친
뒤에는 읽기 전용 파일 시스템에서 죽었다.

반면 vercel.json 의 includeFiles 로 실려가는 app/** 와 prompts/** 는 매
빌드마다 새로 복사되고, 실제로 실행되는 것도 그쪽이다. 그래서 라우팅·
실험 객체·로그 경로를 전부 app/server.py 가 스스로 해결한다.
  route_path() · default_experiment() · default_log_dir()
이 파일은 Vercel 규약을 만족시키는 껍데기로만 둔다. 낡은 채로 실행되든,
아예 실행되지 않든 동작이 같다.
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

    def log_message(self, fmt, *a):
        pass
