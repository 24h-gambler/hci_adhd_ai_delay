# hci_adhd_ai_delay · 질문 설계와 프롬프팅

AI 응답 지연이 대화 경험에 미치는 영향을 보는 실험의 **연구 자료 묶음**이다.
참가자에게 무엇을 말하게 할 것인가(질문 설계)와 AI가 어떻게 답하게 할
것인가(프롬프팅)를 확정하고, 그 통제가 실제로 됐는지 사후에 확인하는
절차까지 담았다.

---

## 왜 이 묶음이 필요한가

대화 내용이 통제되지 않으면 **지연 효과와 내용 효과가 섞인다.**

```
대화 1: AI가 좋은 답을 했다   → 긍정적 평가
대화 2: AI가 엉뚱한 답을 했다 → 부정적 평가

→ 조건 차이가 지연 때문인지 답 품질 때문인지 알 수 없다
```

통제해야 하는 것은 셋이다.

| | 통제 대상 | 수단 | 문서 |
| --- | --- | --- | --- |
| ① | 참가자가 무엇을 말하는가 | 주제 안내 카드 (범위 고정, 내용 자유) | `materials/03` |
| ② | AI가 어떻게 답하는가 | 시스템 프롬프트 + 파라미터 고정 | `materials/04`, `prompts/` |
| ③ | 대화가 얼마나 길어지는가 | 5턴 고정, 자동 종료 | `materials/03 §4` |

그리고 **통제가 됐는지 수치로 확인한다** → `analysis/`

---

## 문서 지도

```
docs/
  00-study-design-overview.md     설계 전제 · 세션 흐름 · 절대 규칙
                                  ★ 설계가 바뀌면 여기를 먼저 고친다

materials/                        참가자와 접촉하는 모든 자료
  01-prescreening-survey.md       사전 스크리닝 (K-ASRS · 챗봇 사용 빈도)
  02-participant-briefing.md      사전 안내문 ★배제 주제 포함
  03-topic-cards.md               주제 안내 카드 2종 (일상 / 고민상담)
  04-system-prompts.md            시스템 프롬프트 설계 근거  ★가장 중요
  05-per-condition-survey.md      조건별 설문 4문항
  06-emotional-engagement-check.md 정서적 관여 확인 문항
  07-interview-guide.md           인터뷰 가이드 (2단계 구성)
  08-debriefing-script.md         디브리핑 스크립트
  09-irb-package.md               IRB 서류 묶음 (불완전 고지 사유서 포함)

prompts/                          앱에 그대로 투입되는 파일
  system_common.txt               공통 (조건·맥락 무관)
  system_safety.txt               안전 규칙 (항상 결합)
  system_context_a.txt            맥락 A 일상 대화
  system_context_b.txt            맥락 B 고민 상담 (공감 L1)
  system_context_b_L2.txt         맥락 B 대안 (공감 L2)
  prompts.yaml                    조합 규칙 · 버전 · 모델 파라미터
  build_prompts.py                조합 + SHA-256 출력

analysis/
  10-manipulation-check-plan.md   조작 점검 계획 (결과 절 4.1)
  manipulation_check.py           자동 검사기

OPEN_QUESTIONS.md                 아직 정하지 못한 것 (🔴 교수님 / 🟡 파일럿 / 🟢 IRB)
```

`materials/`의 번호는 **세션에서 등장하는 순서**다.

---

## 바로 실행해 볼 수 있는 것

```bash
# 시스템 프롬프트를 조합하고 해시를 확인한다
python3 prompts/build_prompts.py
python3 prompts/build_prompts.py --emit context_b      # 본문 출력
python3 prompts/build_prompts.py --json                # 앱이 읽을 형태

# 조작 점검기를 합성 데이터로 돌려 본다 (실제 데이터 없이)
python3 analysis/manipulation_check.py --demo          # 올바른 구현 → 통과
python3 analysis/manipulation_check.py --demo-broken   # 잘못된 구현 → 검출

# 실제 로그로
python3 analysis/manipulation_check.py logs/*.jsonl
```

의존성 없음. 표준 라이브러리만 쓴다.

---

## 이 묶음에서 가장 중요한 세 가지

### 1. 시스템 프롬프트가 지연 조건 3수준에서 완전히 동일해야 한다

말투가 조건마다 다르면 결과가 지연 때문인지 언어 단서 때문인지 알 수 없다.
`prompt_sha256`을 세션 로그에 남겨 두면 **사후에 증명할 수 있다.**
`manipulation_check.py`가 자동으로 검사한다.

### 2. 디브리핑 전까지 어떤 자료에서도 지연을 암시하지 않는다

안내문 · 카드 · 설문 · 모집 문건 · 인터뷰 1단계 어디에도
"속도", "빠름/느림", "기다림", "반응 시간"이 들어가면 안 된다.
참가자가 물어봤을 때 연구자가 할 답도 고정해 두었다
(`materials/02 §6`).

### 3. 배제 주제 안전 경로가 발동한 턴에는 지연을 적용하지 않는다

자해를 언급한 참가자를 17초 동안 빈 화면 앞에 앉혀 두는 것은
그 자체로 해롭다. 목표 지연을 무시하고 즉시 표시하고, 세션을 멈추고,
사람 연구자가 대응한다 (`materials/04 §5`, `materials/09 §6`).

---

## 지금 당장 해야 할 것

`OPEN_QUESTIONS.md`에서 🔴 네 항목(Q1 · Q6 · Q8 · Q10)을 지도교수와 정리하고,
🟡 **Q12를 파일럿 1순위로** 진행한다.

> **Q12 — LLM 응답 시간 분포를 먼저 측정한다.**
> "즉시" 조건(1.0~2.0초)이 실행 불가능할 가능성이 높다. LLM이 목표 시각보다
> 늦게 도착하면 그 턴의 지연이 LLM 생성 시간을 따라가고, 즉시 조건에서만
> conventional 조작의 전제가 깨진다.
> **이 측정 없이는 본 실험을 시작할 수 없다.**

---

## 범위 밖

**지연 주입 엔진과 실험 웹앱 구현은 이 묶음에 없다.** 다만 자료가 그 구현에
거는 요구는 문서에 적어 두었다.

- 목표 지연 D를 **전송 직후에** 뽑고, `t0 + D`에 응답을 표시한다
  (LLM 응답이 온 뒤부터 세면 안 된다) → `analysis/10 §1`
- **스트리밍을 끈다** → `materials/04 §6`
- 로그 스키마 → `analysis/10 §6`
- 안전 경로 · 지연 미적용 · 세션 일시정지 → `materials/04 §5`
