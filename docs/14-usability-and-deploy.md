# 14 · 참가자 경험 사용성 검사 · Vercel 배포

---

# A. 스텝별 사용성 검사

참가자 화면을 Playwright로 실제로 밟으면서 **화면마다 측정**했다.
눈으로 보면 놓치는 것(접힌 부분, 라디오 개수, 내부 스크롤)을 숫자로 잡는다.

## 세션 전체 스텝

```
동의 → 안내 → 연습 1턴 → 주제 카드
  → 대화 1 (9턴) → 문항 3묶음
  → 쉬어가기 → 대화 2 (9턴) → 문항 3묶음
  → 쉬어가기 → 대화 3 (9턴) → 문항 3묶음
  → 세션 종료 문항 (규칙 3택 · QDQ) → 완료
```

전체 41스텝. 대화 27턴 + 문항 4회.

## 찾은 것과 고친 것

### 🔴 문항이 화면 밖으로 1127px 접혀 있었다 — 그게 3번

한 화면에 라디오 **55개**를 쌓은 결과였다.

```
불편감 7 + 관여 지수 4문항×5 + PETS-ER 4문항×7 = 55
```

스크롤이 문제가 아니라 **빠뜨림과 피로**가 문제다. ADHD 성인 대상 연구에서
긴 스크롤 폼은 응답 품질을 떨어뜨린다.

**고친 방법**: 세 묶음으로 나누고 (`1 / 3` 표시) 척도 행 간격을 22px → 14px로.

| | 전 | 후 |
| --- | --- | --- |
| 접힌 부분 | **1127px** | **0 (한 화면에 들어옴)** |

각 묶음은 그 자체로 검증되고, 빠뜨린 항목이 있으면 번호로 알려준다.

### 🟡 채팅 로그가 9턴 뒤 1172px 넘친다

자동 스크롤이 걸려 있어 항상 최신 메시지가 보인다. 일반 챗봇과 같은 동작이라
그대로 둔다. 다만 참가자가 **대화 전체를 한눈에 볼 수 없다** — 인터뷰 회상
화면(`materials/07 §5-1`)이 그 역할을 대신한다.

### 🟡 안내 화면이 216px 넘친다

한 번 읽는 화면이라 허용한다.

### ✅ 유지된 것

- 참가자 화면에 속도·지연·조건 라벨·진행바·경과 시간이 **전혀 없다**
- **대기 중 아무 표시도 하지 않는다** (`indicator: none`을 `config.py`가 강제)
- 차례 표시 `(n / 9)`가 전송 직후 증가 — 지연 구간에 멈춰 있지 않다
- 대화 종료 시 "이번 대화가 끝났습니다" 2초 후 자동 전환
- 본문 17px · 대비 4.5:1 미달 0건 · 모든 입력에 레이블 · Enter 전송

## 측정 방법 (재현)

검사 스크립트는 스크래치패드에 있다. 재현하려면 서버를 띄우고
각 화면에서 아래를 재면 된다.

```js
document.documentElement.scrollHeight - window.innerHeight   // 접힌 부분
scr.querySelectorAll('input[type=radio]').length             // 응답 부담
```

---

# B. Vercel 배포

## 용도 — 사용성 검사·데모 전용

**본 실험용이 아니다.** 두 가지 이유다.

1. **콜드 스타트.** 서버리스 인스턴스가 새로 뜨는 데 수백 ms~수 초가 걸린다.
   얕은 답의 **3초 지연이 깨질 수 있다.** 그런 턴은 `manipulation_ok=false`로
   남는다.
2. **파일 저장이 유지되지 않는다.**

실제 세션은 노트북에서 돌린다.

```bash
python3 app/server.py --port 8000 --provider anthropic
```

## 서버리스 제약을 푼 방법

### 1. 기록 — 브라우저가 모아서 내려받는다

클라이언트가 턴마다 레코드를 쌓고, 종료 화면의 버튼으로 JSONL 두 개를
내려받는다. **스키마는 로컬 실행과 동일하다.**

```
{session_id}.turns.jsonl      27턴
{session_id}.surveys.jsonl    문항 4회
```

검증: 브라우저가 내려받은 로그를 `analysis/manipulation_check.py`에 넣어
**17/17 통과**를 확인했다.

### 2. 세션 상태 — 없어도 된다

계획이 시드 결정론이라 `session_id`만으로 복원된다.
대화 이력은 클라이언트가 **그 대화 것만** 보낸다 (대화 간 격리 유지).

```
Experiment._restore_session(session_id, body)
```

검증: 인스턴스를 버리고 새로 띄운 뒤 같은 세션을 이어가, 깊이·지연이
원래 계획과 일치하고 대화 2에 대화 1의 이력이 새지 않음을 확인했다.

## 배포 구성

```
vercel.json          모든 경로를 api/index.py 로 (정적 파일도 같은 함수가 서빙)
api/index.py         Vercel 런타임이 찾는 handler
requirements.txt     비어 있음 — mock 경로는 표준 라이브러리만
```

정적 파일을 함수가 서빙하므로 **로컬과 배포의 코드 경로가 하나**다.
`app/tests/e2e.js`를 배포본에 그대로 돌릴 수 있다.

## 배포 상태

프로젝트는 대시보드에서 한 번 만들어졌고(`hci_adhd_ai_delay`), 그 뒤로는
브랜치에 푸시할 때마다 자동 배포된다. MCP 토큰으로는 프로젝트 **생성**이
막혀 있었다(403) — 조회·로그·배포 확인은 된다.

## 🔴 배포되고도 API 가 통째로 죽어 있던 사고

처음 네 번의 배포는 전부 "성공"이었고 화면도 떴다. 그런데 `/api/health` 가
JSON 대신 **HTML** 을 돌려줬다. 즉 **API 가 하나도 동작하지 않는 상태로
배포가 성공해 보였다.** 원인이 두 겹이었다.

### 겹 1 — rewrite 가 경로를 바꾼다

`vercel.json` 의 catch-all rewrite 는 함수에 도착하는 경로를 함수 자신의
경로(`/api/index`)로 바꾼다. 빌드 로그도 그렇게 경고한다.

```
WARNING! Internal rewrites in backend framework projects now route requests
using the rewritten destination path.
```

그래서 `/api/health` 요청이 라우터를 지나쳐 **정적 폴백**으로 빠졌고,
폴백은 모르는 경로를 `index.html` 로 덮었다. 200 + HTML.
→ 원래 경로를 `?__p=` 로 같이 넘기고, `route_path()` 가 그것으로 복원한다.
→ 그리고 **`/api/*` 는 절대 HTML 로 덮지 않는다.** 모르면 404 + JSON 이다.
   조용히 덮는 폴백이 "배포는 멀쩡한데 API 는 죽음"을 만든 진짜 원인이다.

### 겹 2 — 진입 파일의 바이트코드가 빌드 캐시에 얼어붙는다

겹 1 을 고쳤는데도 세 번 연속 그대로였다. 빌드 로그 두 줄이 답이었다.

```
Restored build cache from previous deployment (...)
Compiling Python bytecode...
```

Vercel 은 함수 **진입 파일**을 바이트코드로 컴파일해 빌드 캐시에 얹어
재사용한다. `api/index.py` 에 넣은 수정이 배포되지 않고 **첫 배포본에
얼어붙어 있었다.** 반면 `includeFiles` 로 실려가는 `app/**` 와
`prompts/**` 는 매 빌드마다 새로 복사된다.

배포본 하나를 두드렸더니 이런 404 가 나왔고, 여기서 확정됐다.

```json
{"error": "unknown_api_route", "route_path": "/api/index"}
```

본문 형식은 **방금 올린** `app/server.py` 것이고, `route_path` 값은
**첫 판본** `api/index.py` 것이다. 한 배포본에서 같이 나올 수 없는 조합이다.

**그래서 진입 파일에는 로직을 두지 않는다.**

```
app/server.py     라우팅·응답·진단 전부 (매 빌드 새로 복사됨)
api/index.py      srv.Handler 를 상속해 exp 만 매다는 껍데기
```

첫 배포본 `api/index.py` 를 그대로 끼워 재현 검사했고, 모든 경로가 동일하게
동작한다. 진입 파일이 낡은 채 실행돼도 상관없는 구조다.

`app/tests/test_server.py · ServerlessRoutingTest` 가 이걸 지킨다. 특히
마지막 검사는 **진입 파일의 `handler` 가 `log_message` 말고 다른 메서드를
가지면 실패한다** (`ast` 로 확인). 로직을 진입 파일로 되돌리면 "배포는
됐는데 반영은 안 되는" 상태로 돌아가기 때문이다.

### 겹 3 — 진입 파일은 아예 실행되지 않는다

겹 2 를 고친 뒤에도 프로덕션은 화면만 뜨고 시작 버튼이 죽어 있었다.
런타임 로그에 사용자 테스트가 그대로 남아 있었다.

```
07:37:35  GET  /app.js                 200   ← 화면은 떴다
07:38:03  POST /api/session/start      500
07:38:06  POST /api/session/start      500   ← 여기서 죽었다
```

```
AttributeError: 'NoneType' object has no attribute 'plan'
```

`self.exp` 가 `None` 이다. `api/index.py` 가 매다는 `exp = _exp` 가 먹히지
않았다. 그리고 이어서 나온 `HCI_LOG_DIR` 미설정(`os.environ.setdefault` 가
같은 파일에 있다)까지 합치면 결론은 하나다.

**Vercel 은 `api/index.py` 를 실행하지 않는다.** 배포본의 `/api/health` 가
그 사실을 그대로 말한다.

```json
{"ok": true, "server_build": "2026-09-18.d6", "handler": "Handler", "exp": "fallback"}
```

`handler` 가 `Handler` — 즉 `app/server.py` 의 클래스 자체다.
`api/index.py` 의 `handler`(소문자) 가 아니다.

| 겹 | 진입 파일에 기댄 것 | 증상 |
| --- | --- | --- |
| 1 | `route_path` 오버라이드 | `/api/*` 가 전부 200 + HTML |
| 2 | `exp = _exp` 클래스 속성 | 실험 경로가 전부 500 |
| 3 | `HCI_LOG_DIR` 환경변수 | 읽기 전용 파일 시스템 OSError |

세 번 다 같은 가정에서 나왔다. **`app/server.py` 는 아무도 아무것도
해 주지 않는다고 보고 혼자 설 수 있어야 한다.**

- `route_path()` — 원래 경로를 `?__p=` 에서 복원
- `default_experiment()` — 매달린 `exp` 가 없으면 스스로 만든다
- `default_log_dir()` — 쓸 수 있는 곳을 직접 고른다 (안 되면 `/tmp`)

### 배포 확인은 실제 기능을 건드려야 한다

`/api/health` 가 `{"ok": true}` 만 돌려주던 동안, 실험 기능은 통째로 죽어
있었다. **"200 이면 정상"이라는 확인이 이 사고를 세 번 통과시켰다.**
그래서 health 가 실험 객체까지 건드리고 그 결과를 싣는다.

```
exp: "bound"     실행 환경이 매달아 줌 (노트북 실행)
exp: "fallback"  서버가 스스로 세움 (서버리스)
exp: "error"     세우지 못함 + 이유
```

### 배포본을 Vercel 과 같은 조건으로 로컬에서 밟는 법

이 환경에서는 조직 egress 정책이 `*.vercel.app` 을 막아 배포본에
POST 를 보낼 수 없다. 대신 **같은 조건을 로컬에 재현**한다.

```python
# exp 를 매달지 않고, server 객체에 verbose 도 없이 Handler 를 그대로 띄운다
httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
```

여기에 `app/tests/e2e.js` 를 그대로 붙여 참가자 27턴을 완주시켰다.
표시 오차 중앙 5ms · 최대 16ms, 조작 점검 17/17.
`app/tests/test_server.py · ExpFallbackTest` 가 같은 조건을 상시 검사한다.

### 배포본이 최신인지 확인하는 법

모든 응답에 `X-Server-Build` 헤더가 붙는다 (`app/server.py: SERVER_BUILD`).
빌드 로그를 뒤지지 않고 응답 하나로 확인한다.

```
curl -sI https://<배포주소>/api/health | grep -i x-server-build
curl -s  https://<배포주소>/api/health          # {"ok": true} 여야 한다
```

`/api/health` 가 HTML 이면 라우팅이 깨진 것이고, 404 JSON 이면 경로가
복원되지 않은 것이다. 둘 다 즉시 구분된다.

### 확인된 상태 (2026-09-18)

```
GET /api/health
  200  {"ok": true, "server_build": "2026-09-18.d6",
        "handler": "Handler", "exp": "fallback"}

GET /api/session/NOPE/plan
  404  {"error": "'NOPE'"}        ← exp 가 실제로 서 있다는 증거
```

`/api/session/…/plan` 이 404 JSON 이라는 것은 `exp.plan()` 이 호출되어
`KeyError` 를 던졌다는 뜻이다. 500 이면 `exp` 가 없는 것이고, HTML 이면
라우팅이 깨진 것이다. 세 상태가 응답만으로 구분된다.

**프로덕션 주소는 `main` 을 따라간다.** 브랜치에 아무리 배포해도
`hciadhdaidelay.vercel.app` 은 바뀌지 않는다. PR 을 합쳐야 한다.

### 연구자 화면은 배포본에서 비어 있다

`GET /api/session/<id>/plan` 은 **메모리에 있는 세션만** 돌려준다. 서버리스는
인스턴스가 매번 새로 뜨므로 계획표·안전 경보 이력이 비어 보인다.

시드에서 계획을 복원하게 만들 수도 있지만 **일부러 하지 않았다.** 세션 ID를
잘못 입력해도 그럴듯한 계획표가 `alerts: []` 과 함께 뜨기 때문이다. 안전
경보를 지켜보는 화면에서 "경보 없음"과 "세션을 못 찾음"이 같아 보이면 안 된다.
연구자 화면은 노트북 실행(`python3 app/server.py`)에서만 쓴다.

## 🔒 지금은 Vercel 로그인 없이 못 연다

```
ssoProtection: enabled, deploymentType = all_except_custom_domains
```

**프로덕션 주소까지 포함해** 모든 vercel.app 주소가 Vercel 계정 로그인을
요구한다. 다른 사람에게 링크를 보내 사용성 검사를 시키려면 꺼야 한다.

> Vercel → 프로젝트 → Settings → Deployment Protection → Vercel Authentication 끄기

**끄기 전에 확인할 것.** 이 앱은 기본값이 `mock` 제공자라 키가 없지만,
나중에 `ANTHROPIC_API_KEY` 를 환경변수에 넣은 뒤 보호를 꺼 두면 **주소를
아는 누구나 그 키로 모델을 호출**하게 된다. 실제 모델을 붙일 계획이라면
보호를 켠 채로 두거나 비밀번호 보호로 바꾸는 편이 낫다.

### 환경변수 (실제 모델을 붙일 때)

| 변수 | 값 |
| --- | --- |
| `HCI_PROVIDER` | `anthropic` |
| `ANTHROPIC_API_KEY` | (키) |
| `HCI_DELAY_SCALE` | `1.0` — 축소하지 않는다 |

`requirements.txt`의 `anthropic` 주석을 풀어야 한다.
