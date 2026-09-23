# 실험 웹앱 · 인터페이스 계약

이 문서가 서버·프런트엔드·테스트가 공유하는 **유일한 기준**이다.
구현이 이 문서와 어긋나면 구현이 틀린 것이다.

관련 설계 문서: `docs/00-study-design-overview.md`,
`materials/04-system-prompts.md`, `analysis/10-manipulation-check-plan.md`

---

## 0. 원칙 — 이 앱이 반드시 지켜야 하는 것

| # | 규칙 | 검증 |
| --- | --- | --- |
| P1 | **목표 지연 D는 사용자 입력 내용과 독립이다** | `test_schedule.py::test_delay_independent_of_text` |
| P2 | 응답은 `submit_ts + D`에 표시된다 (LLM이 얼마 걸렸든) | E2E + `manipulation_check.py` |
| P3 | LLM이 D보다 늦으면 즉시 표시하고 `manipulation_ok=false` | `test_server.py` |
| P4 | 스트리밍 금지 — 응답 전체를 받아 한 번에 표시 | 코드 검사 + `test_llm.py` |
| P5 | 안전 경로 발동 턴은 **지연을 적용하지 않는다** | `test_server.py::test_safety_bypasses_delay` |
| P6 | 대화 6개는 각각 독립 — 대화 간 이력 초기화 | `test_server.py::test_history_reset` |
| P7 | 같은 맥락 안에서 지연 조건 3수준의 프롬프트 해시가 같다 | `test_prompt.py` |
| P8 | 연습 턴은 조건 지연을 쓰지 않고 분석에서 제외된다 | `test_schedule.py` |

### P1을 어떻게 보장하는가 — 구조적 보장

D를 "입력을 읽기 전에 뽑는다"는 **코드 순서에 의존하는 약속**이라
검증할 수 없다. 대신 **시드 결정론**으로 바꾼다.

```
D = uniform(min, max) with seed = SHA256(f"{session_id}|{conversation_index}|{turn_index}")
```

- 입력 텍스트가 시드에 들어가지 않으므로 **D는 텍스트의 함수일 수 없다.**
- 같은 (session, conversation, turn)에 서로 다른 텍스트 1000개를 넣어도
  D가 동일함을 테스트로 증명한다. 상관계수보다 강한 보장이다.
- `session_id`가 들어가므로 참가자·세션마다 다른 수열이 나온다.
- 로그에 `session_id`가 있으므로 사후에 D를 재현할 수 있다.

---

## 1. 실행

```bash
python3 app/server.py --port 8000 --provider mock          # API 키 없이 전체 흐름
python3 app/server.py --port 8000 --provider anthropic     # 실제 모델
```

| 옵션 | 기본 | 설명 |
| --- | --- | --- |
| `--port` | 8000 | |
| `--provider` | `mock` | `mock` \| `anthropic` |
| `--log-dir` | `logs/` | JSONL 출력 |
| `--mock-latency-mode` | `length` | `length`(입력 길이 비례) \| `fixed` \| `slow` |
| `--config` | `prompts/prompts.yaml` | |

`mock` 경로는 **Python 표준 라이브러리만** 쓴다 — 단위 검사와 E2E, 검증
파이프라인 전체가 설치 없이 돌아간다.
`anthropic` 경로만 공식 SDK(`pip install anthropic`)를 **지연 임포트**한다.

### mock 제공자는 일부러 "나쁘게" 만든다

`--mock-latency-mode length`는 **LLM 생성 시간이 입력 길이에 비례**하도록
만든다. 지연 주입이 잘못 구현되어 있으면 이 mock에서 상관이 드러난다.
올바른 구현이면 mock의 길이 의존성이 있어도 부과 지연은 D 그대로다.
**검증용 함정이다. 끄지 않는다.**

---

## 2. HTTP API

모든 요청/응답은 `application/json; charset=utf-8`.
시각은 **epoch 밀리초 정수**.

### `POST /api/session/start`

```jsonc
// 요청
{ "participant_id": "P01", "group": "adhd" }
// 응답
{
  "session_id": "P01-1756400000000",
  "participant_number": 1,
  "block_order": ["a", "b"],
  // ↓ P01(N=1)의 실제 배정. §4의 식으로 계산한 값이다.
  //   블록 1 = PERMS[(1-1)%6] = PERMS[0], 블록 2 = PERMS[(1-1+3)%6] = PERMS[3]
  "conversations": [
    {"index":1,"condition":"R1","turns":9},
    {"index":2,"condition":"R2","turns":9},
    {"index":3,"condition":"R3","turns":9}
  ],
  "turns_per_conversation": 9,
  "prompt_version": "v0.4",
  "base_prompt_sha256": "...",
  "model": "mock-fixed", "temperature": 0.6, "max_tokens": 400
}
```

`conversation_index` 0은 **연습 턴** 전용이다 (세션 계획에 없음).

### `POST /api/turn`

```jsonc
// 요청
{
  "session_id": "P01-...",
  "conversation_index": 1,          // 0 = 연습
  "turn_index": 1,                  // 1..9 (0 = 오프너, /api/opener)
  "text": "사용자 입력 원문",
  "user_input_start_ts": 1756400000000,
  "user_input_submit_ts": 1756400004210     // = t0
}
// 응답
{
  "turn_id": "P01-...:1:1",
  "target_delay_ms": 15000,        // 예: R1 깊은 답. 연습·오프너는 8000
  "deadline_ts": 1756400019210,     // = user_input_submit_ts + target_delay_ms
  "reply": "AI 응답 원문",
  "llm_request_ts": 1756400004230,
  "llm_response_ts": 1756400005110,
  "finish_reason": "stop",          // "stop" | "length" | "fixed"(오프너)
  "safety_flag": false,
  "bypass_delay": false,            // true면 deadline 무시하고 즉시 표시
  "condition": "R1", "depth": "deep",
  "prompt_sha256": "...", "model": "mock-fixed"
}
```

- 서버는 **응답을 통째로** 돌려준다. 스트리밍 없음 (P4).
- `bypass_delay: true`이면 프런트엔드는 `deadline_ts`를 **무시**하고
  즉시 표시한다 (P5).
- 서버는 이 시점에 턴 레코드를 메모리에 보관한다. 아직 로그에 쓰지 않는다.

### `POST /api/turn/display`

프런트엔드가 화면에 실제로 표시한 뒤 즉시 호출한다.
**서버는 이때 턴 레코드를 완성해 JSONL에 append 한다.**

```jsonc
{ "turn_id": "P01-...:1:1", "display_ts": 1756400012640 }
// 응답: { "ok": true, "manipulation_ok": true, "display_error_ms": 18 }
```

### `POST /api/turn/next-input`

다음 턴의 첫 타자 입력 시각. **직전 턴의 `next_input_start_ts`를 채운다.**

```jsonc
{ "turn_id": "P01-...:1:1", "next_input_start_ts": 1756400015220 }
```

### `POST /api/survey`

```jsonc
{
  "session_id": "...", "kind": "per_condition",   // "per_condition" | "engagement"
  "conversation_index": 1,
  "shown_ts": 0, "submitted_ts": 0,
  "responses": { "time_estimate_sec": 9, "discomfort": 4, "...": 0 }
}
```

### `GET /api/session/{session_id}/plan`

연구자 화면용. 세션 계획과 안전 경보 이력을 돌려준다.

### `POST /api/session/{session_id}/end`

미완성 턴을 `display_ts: null`로 마감하고 로그를 닫는다.

### 테스트 패널 (`?test=1`)

설문 자동채우기·자동주행이 되는 임시 패널이다. **그 세션의 모든 레코드에
`test_mode: true` 가 찍힌다** — `start_session` 이 세션에 새기고, 턴·설문·
이벤트가 그것을 그대로 싣는다. 본문이 보내온 값이 아니라 **세션**이 기준이다
(`Experiment.stamp_test_mode`).

`analysis/manipulation_check.py` 는 `test_mode` 가 하나라도 있으면 실패한다.

★ 주석으로 "실세션에서는 쓰지 않는다"고 적어 두는 것만으로는 부족하다.
자동주행이 만든 JSONL 이 진짜 참가자 로그와 구분되지 않으면 조용히 분석에
섞인다. P1을 구조로 보장한 것과 같은 이유다.

### `GET /api/health`

배포본이 **실제로 쓸 수 있는 상태인지** 돌려준다.

```json
{"ok": true, "server_build": "2026-09-23.strict1", "handler": "Handler", "exp": "fallback"}
```

| 필드 | 뜻 |
| --- | --- |
| `ok` | 실험 객체가 서 있는가. 이것이 false면 실험 경로가 전부 죽어 있다 |
| `server_build` | 도는 코드의 판본. 응답 헤더 `X-Server-Build` 와 같다 |
| `handler` | 실행 환경이 띄운 핸들러 클래스 이름 |
| `exp` | `bound`(환경이 매달아 줌) · `fallback`(서버가 스스로 세움) · `error`(+ 이유) |

★ **살아 있다는 것만 보고하면 안 된다.** 배포본이 `{"ok": true}` 만
돌려주던 동안 실험 기능은 통째로 500이었고, 그 확인이 사고를 세 번
통과시켰다. health는 반드시 실험 객체를 건드린 결과를 실어야 한다.

### 서버는 혼자 설 수 있어야 한다

배포 환경(Vercel)은 진입 파일 `api/index.py` 를 실행하지 않는다.
거기서 매단 `exp` 도, 넣은 환경변수도 서버에 닿지 않는다. 그래서
`app/server.py` 가 세 가지를 스스로 해결한다.

| 함수 | 없을 때 무슨 일이 났었나 |
| --- | --- |
| `route_path()` | `/api/*` 가 전부 200 + HTML |
| `default_experiment()` | 실험 경로가 전부 500 |
| `default_log_dir()` | 읽기 전용 파일 시스템 OSError |

`app/tests/test_server.py · ExpFallbackTest` 가 `exp` 를 매달지 않은
`Handler` 를 그대로 띄워 이 성질을 상시 검사한다.

---

## 3. 로그 스키마 (JSONL, 한 줄 = 한 턴)

`analysis/10-manipulation-check-plan.md §6`과 **정확히 일치해야 한다.**
`manipulation_check.py`가 그대로 읽는다.

```jsonc
{
  "session_id": "P01-1756400000000",
  "participant_id": "P01", "group": "adhd",
  "block": 1, "conversation_index": 1, "condition": "medium", "context": "a",
  "turn_index": 1, "practice": false,
  "user_input_start_ts": 0, "user_input_submit_ts": 0,
  "user_input_text": "", "user_input_chars": 0,
  "target_delay_ms": 8412,
  "llm_request_ts": 0, "llm_response_ts": 0, "display_ts": 0,
  "ai_response_text": "", "ai_response_chars": 0,
  "next_input_start_ts": null,
  "safety_flag": false, "manipulation_ok": true,
  "prompt_version": "v0.2", "prompt_sha256": "...", "empathy_variant": "B",
  "model": "mock-1", "temperature": 0.6, "max_tokens": 200,
  "finish_reason": "stop"
}
```

파생값은 로그에 넣지 않는다 — `manipulation_check.py`가 계산한다.

**파일 배치**: `logs/{session_id}.turns.jsonl`, `logs/{session_id}.surveys.jsonl`

**`manipulation_ok` 판정**: `llm_response_ts <= user_input_submit_ts + target_delay_ms`
**이면서** 안전 경로가 아닐 것. 연습 턴은 `practice: true`로 분석에서 빠진다.

---

## 4. 조건 배정 (상쇄)

참가자 번호 `N` = `participant_id`의 숫자 부분 (`P07` → 7).

```
블록 순서:  N이 홀수 → ["a","b"]      N이 짝수 → ["b","a"]
조건 순서:  PERMS = 3개 조건의 6가지 순열 (사전순 고정)
            블록 1 → PERMS[(N-1) % 6]
            블록 2 → PERMS[(N-1+3) % 6]     # 블록 간 재무작위화
```

`PERMS`는 `["immediate","long","medium"]` 식의 정렬된 순열 리스트로 고정한다.
**어떤 N에서도 두 블록의 조건 순서가 같으면 안 된다.**

---

## 5. 안전 경로

1. `/api/turn`에서 **LLM 호출 전에** 사용자 입력을 검사한다.
2. 걸리면 LLM을 호출하지 않고 아래를 그대로 돌려준다.
   ```
   "이 부분은 연구자와 이야기하시는 것이 좋겠습니다."
   ```
   `safety_flag: true`, `bypass_delay: true`, `manipulation_ok: false`
3. 프런트엔드는 즉시 표시하고 **연구자 호출 오버레이**를 띄운다.
   다음 대화로 자동 진행하지 않는다.
4. 연구자 화면(`/researcher`)에 경보가 남는다.

검사 대상 (`app/safety.py`): 자해·자살, 타인 가해, 범죄, 급성 위기.
**키워드 목록은 재현성을 위해 파일로 고정하고 로그에 매칭 여부만 남긴다**
(어떤 키워드에 걸렸는지는 남기지 않는다 — 참가자 보호).

---

## 6. 대화 이력

- 서버가 `(session_id, conversation_index)`별로 메시지 리스트를 보관한다.
- 턴마다 `[system] + 그 대화의 이전 메시지 전부 + [user N]`을 보낸다.
- **`conversation_index`가 바뀌면 새 리스트로 시작한다** (P6).
- 연습 턴(`conversation_index: 0`)의 이력은 어떤 대화에도 섞이지 않는다.

---

## 7. 프런트엔드 화면 순서

```
consent → briefing → practice(오프너+4턴, 8초 고정·분석 제외) → card(1회)
→ [chat(오프너+9턴) → survey] ×3 (사이 break)
→ engagement(매핑 3택) → done
```

본블록 9턴 = 깊음 3 · 보통 3 · 얕음 3. 연습 4턴은 실험설계 PART 2
(워밍업 4–5턴) 하한이다.

### AI 오프너 (PART 3-3)

각 대화 시작 시 AI가 먼저 고정 문구로 화제를 연다. 참가자는 답부터
시작하므로 "안녕하세요" 첫인사와 깊이 지시의 어색한 맞물림이 없다.
- 문구: 대화 위치별 고정 1종 (연습·중간 3종, 조건·참가자와 무관)
- 지연: 8초 고정 (전 조건 동일 — 조작이 아니라 앵커)
- LLM 미호출 · 로그 `opener: true` · 분석 제외 (practice와 동급)
- `POST /api/opener` → 표시 완료는 기존 `/api/turn/display` (turn_index 0)

### 타이밍 규칙 (프런트엔드) — 엄격 모드

```js
// 전송
const submit = nowMs();                       // performance.timeOrigin + performance.now()
POST /api/turn { user_input_submit_ts: submit, ... }
// 응답 도착
const deadline = submit + target_delay_ms;
if (bypass_delay || nowMs() >= deadline) show();          // 즉시
else {
  setTimeout(spin, deadline - nowMs() - 40);              // 40ms 전까지 대기
  function spin(){ if (nowMs() >= deadline) show(); else requestAnimationFrame(spin); }
}
function show(){ appendMessage(reply); POST /api/turn/display {display_ts: nowMs()} }
```

- `nowMs()`는 `performance.timeOrigin + performance.now()`를 반올림한다.
  `Date.now()`는 해상도와 점프 때문에 쓰지 않는다.
- 대기 중 화면은 완전 정지다. 타이핑 점·`…`·스피너·진행 바·남은 시간
  표시를 절대 그리지 않는다. `?indicator=`·`?progress=` URL 옵션은
  엄격 모드에서 무시된다 (기본 `none`/off 고정).
- 진행 표시 `(n/9)`·`이번 대화는 N번` 문구를 화면에 내지 않는다.
- 입력창은 대기 중에도 열려 있다. 대기 중 전송은 큐(`sendQueue`)에
  넣고 현재 턴 표시 직후 새 턴으로 전송한다. 큐 접수 턴은
  `queued_during_wait: true`로 로그에 남는다 (B1).
- 대기 중 첫 타자도 `user_input_start_ts`에 그대로 기록된다 (B2).
- `visibilitychange`/`blur` 이탈은 `attentionEvents` 메모리 로그 +
  연구자 화면·`*.attention.jsonl`에 남긴다 (B4, 서버 영속화는 Phase 2).

### 자동 진행 훅 (E2E 전용)

`?e2e=1`이면 `window.__exp`에 아래를 노출한다. 프로덕션 흐름은 바꾸지 않는다.

```js
window.__exp = {
  state(),                 // {screen, conversationIndex, turnIndex, condition, context}
  send(text),              // 입력 후 전송
  lastDisplay(),           // {turnId, deadline, displayTs, error}
  advance(),               // 안내/카드 화면의 다음 버튼
  fillSurvey(),            // 설문을 기본값으로 채우고 제출
  done()                   // 세션 종료 여부
}
```

---

## 8. 검증 파이프라인

```bash
bash app/verify.sh
```

1. `python3 -m unittest discover app/tests -p 'test_*.py'` — 단위 검사
2. 서버를 mock으로 띄우고 **Playwright로 세션 1개를 끝까지 진행**
3. 생성된 JSONL을 `analysis/manipulation_check.py`에 통과시킨다 (exit 0)
4. `prompts/response_rules.py --jsonl`로 규칙 위반 0건 확인
   (mock 응답은 규칙을 지키도록 작성한다)

**4단계가 전부 통과해야 앱이 정상이다.**
