"""Vercel 서버리스 진입점 — 사용성 검사·데모 전용.

★ 본 실험용이 아니다.
  · 서버리스는 파일 저장이 유지되지 않는다. 기록은 **브라우저가 모아
    세션 끝에 내려받는다** (app/static/app.js: downloadLogs).
  · 콜드 스타트가 수백 ms~수 초 걸릴 수 있어, 얕은 답의 3초 지연이
    깨질 수 있다. 그런 턴은 manipulation_ok=false 로 남는다.
  · 실제 세션은 노트북에서 `python3 app/server.py` 로 돌린다.

★★ 이 파일에는 로직을 넣지 않는다.

Vercel 은 함수 진입 파일을 바이트코드로 컴파일해 **빌드 캐시에 얹어
재사용한다** (빌드 로그: "Restored build cache from previous deployment" +
"Compiling Python bytecode"). 그래서 이 파일에 넣은 수정이 배포되지 않고
첫 배포본에 얼어붙는 일이 실제로 일어났다 — 세 번 연속으로.

반면 vercel.json 의 includeFiles 로 실려가는 app/** 와 prompts/** 는 매
빌드마다 새로 복사된다. 그래서 라우팅·응답 로직은 전부 app/server.py 의
Handler 에 두고, 여기서는 그 클래스를 상속해 exp 만 매단다. 이 파일이
낡은 채로 실행돼도 동작이 달라지지 않는다.
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
