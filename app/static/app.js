/* AI 대화 경험 연구 · 참가자 화면 로직
 *
 * 기준 문서: app/CONTRACT.md (§2 HTTP API, §7 화면 순서와 타이밍 규칙)
 *
 * 타이밍 요약 — 이 파일에서 가장 중요한 부분이다.
 *   1) 매 턴 첫 타자 입력 시각을 user_input_start_ts로 기록한다 (포커스가 아니라 입력).
 *   2) 전송 시각 submit = nowMs()  → POST /api/turn
 *   3) deadline = submit + target_delay_ms
 *   4) bypass_delay 이거나 이미 deadline이 지났으면 즉시 표시,
 *      아니면 (deadline - 40ms)까지 setTimeout → 이후 requestAnimationFrame 스핀
 *   5) show()는 DOM에 붙인 "직후" display_ts를 찍고 POST /api/turn/display
 *   nowMs()는 performance.timeOrigin + performance.now() 만 쓴다. Date.now() 금지.
 */
(function () {
  'use strict';

  if (window.__EXP_LOADED__) { return; }   // 정적 경로 탐색 때문에 두 번 실려도 한 번만 동작
  window.__EXP_LOADED__ = true;

  /* ==========================================================
     0. 시각
     ========================================================== */

  // performance.timeOrigin 은 문서 수명 동안 고정이다. 한 번만 읽는다.
  var TIME_ORIGIN = (window.performance && typeof performance.timeOrigin === 'number' && performance.timeOrigin > 0)
    ? performance.timeOrigin
    : (Date.now() - (window.performance && performance.now ? performance.now() : 0));

  function nowMs() {
    return Math.round(TIME_ORIGIN + performance.now());
  }

  /* ==========================================================
     1. 실행 옵션 (URL 질의 문자열)
     ========================================================== */

  var params = new URLSearchParams(location.search);

  function pick(value, allowed, fallback) {
    return allowed.indexOf(value) >= 0 ? value : fallback;
  }

  var OPT = {
    // 실험설계 PART 3-4 — 엄격 모드. URL 옵션으로 표시/진행을 켤 수 없다.
    // indicator는 항상 none(정지 화면), progress는 항상 off(진행 카운터 없음).
    // 파일럿용 ?indicator/?progress 질의는 무시한다.
    indicator: 'none',
    progress: false,
    e2e: params.get('e2e') === '1',
    // ★ 테스트용 임시 패널. ?test=1 일 때만 뜬다. 실세션에서는 절대 쓰지 않는다.
    test: params.get('test') === '1',
    researcher: params.get('researcher') === '1' || /\/researcher\/?$/.test(location.pathname)
  };

  var LIVE_KEY = 'exp.live';
  var LAST_SESSION_KEY = 'exp.lastSession';
  var SURVEY_FAIL_KEY = 'exp.surveyFailures';   // 서버에 못 넣은 설문 응답 보관

  /* ==========================================================
     2. DOM 도우미
     ========================================================== */

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = text; }
    return n;
  }
  function clear(node) { while (node && node.firstChild) { node.removeChild(node.firstChild); } }
  function noop() {}

  var D = {};   // init()에서 채운다

  /* ==========================================================
     3. HTTP
     ========================================================== */

  function post(path, body) {
    return fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) { throw new Error('POST ' + path + ' → ' + r.status); }
      return r.json();
    });
  }

  function get(path) {
    return fetch(path, { headers: { 'Accept': 'application/json' } }).then(function (r) {
      if (!r.ok) { throw new Error('GET ' + path + ' → ' + r.status); }
      return r.json();
    });
  }

  /* ==========================================================
     4. 상태
     ========================================================== */

  var State = {
    screen: 'consent',        // 논리 화면 이름 (practice는 chat 섹션을 공유한다)
    session: null,            // /api/session/start 응답 원본
    participantId: null,
    group: null,
    plan: [],                 // 화면 순서
    stepIndex: -1,

    conversationIndex: null,
    condition: null,
    context: null,
    block: null,

    turnsSent: 0,             // 전송 직후 증가 (계약 §7)
    turnsTotal: 5,

    prevTurnId: null,         // 다음 턴 첫 타자 입력 시각을 채워 넣을 직전 턴
    inputStartTs: null,       // 이번 턴 첫 타자 입력 시각
    awaiting: false,          // 서버 왕복 중 (입력은 계속 받는다 — 설계서 PART 3-4)
    inflightText: null,       // 표시 대기 중인 전송 원문 (중복 재전송 판정용)
    pending: null,            // 표시 대기 중인 턴
    sendQueue: [],            // 대기 중 접수된 후속 메시지 {text, queuedAtTs}

    lastDisplay: null,        // {turnId, deadline, displayTs, error}
    displayLog: [],
    safetyEvents: [],
    attentionEvents: [],      // B4: 화면 이탈(visibilitychange/blur) 시각·지속
    surveyShownTs: null,
    lastSubmit: Promise.resolve(),
    surveyPart: 1,
    records: [],
    surveys: [],
    endAutoTimer: null,
    endPending: false,
    doneReached: false
  };

  var displayWaiters = [];    // e2e send() 해소용 [{resolve, reject}]

  /* ==========================================================
     5. 문안 (참가자에게 보이는 모든 문자열)
        ※ "속도 / 빠름 / 느림 / 지연 / 기다림" 계열 단어는 절대 쓰지 않는다.
     ========================================================== */

  var TOPIC_CARD_TITLE = '이번에는 이런 이야기를 해 주세요';

  var TOPIC_CARD = [
    '요즘 신경 쓰이거나 마음에 걸리는 일을\n이야기해 주세요.',
    '실제 고민일 때 연구에 도움이 됩니다.',
    '하고 싶은 만큼만 하세요.\n불편하면 언제든 멈춰도 됩니다.',
    '대화는 아홉 번 주고받으면 끝납니다.\n모두 세 번 합니다.'
  ];

  var TOPIC_LINE = '대화 주제 — 요즘 신경 쓰이거나 마음에 걸리는 일';

  var PRACTICE_NOTE = '연습입니다. 네 번 주고받아 보세요. 아무 말이나 입력하고 보내면 됩니다.';

  // 설계서 PART 4 · 블록 직후 문항(대화마다). PETS-ER 6문항은 CC-BY 원척도
  // (Schmidmaier et al., CHI 2024)의 한국어 초역 — 본 실험 전 원문 대조 확정.
  // Godspeed I 5문항은 materials/06 그대로 (Bartneck et al. 2009, 1–5점).
  var PETS_ITEMS = [
    'AI는 내 감정에 반응했다.',
    'AI는 내가 어떻게 느끼는지 이해한 것 같았다.',
    'AI는 내 감정에 공감했다.',
    'AI는 내 기분을 고려해 답했다.',
    'AI는 내 감정 상태의 변화를 알아차린 것 같았다.',
    'AI는 내 이야기에 정서적으로 조응했다.'
  ];

  var GOD_ITEMS = [
    { key: 'god_1', left: '인위적인', right: '자연스러운' },
    { key: 'god_2', left: '기계 같은', right: '사람 같은' },
    { key: 'god_3', left: '의식이 없는', right: '의식이 있는' },
    { key: 'god_4', left: '인공적인', right: '생명체 같은' },
    { key: 'god_5', left: '뻣뻣한', right: '우아한' }
  ];

  // 명세서 §2-2 — 고민 상담 블록에만. 1~5점. ④는 역방향이며 채점 시 반전한다.
  var ENGAGEMENT_ITEMS = [
    { key: 'engagement_1', text: '방금 이야기한 내용은 실제로 요즘 신경 쓰이는 일이었나요?',
      left: '전혀 아니다', right: '실제 고민이었다' },
    { key: 'engagement_2', text: '그 일이 요즘 얼마나 신경 쓰이나요?',
      left: '전혀 신경 쓰이지 않는다', right: '매우 신경 쓰인다' },
    { key: 'engagement_3', text: '이야기하는 동안 그 일에 대해 얼마나 집중하셨나요?',
      left: '전혀 집중하지 않았다', right: '매우 집중했다' },
    { key: 'engagement_4', text: '평소에 이 이야기를 다른 사람에게 하시는 편인가요?',
      left: '자주 한다', right: '거의 하지 않는다', reverse: true }
  ];

  /* ==========================================================
     6. 화면 전환
     ========================================================== */

  function sectionName(logical) { return logical === 'practice' ? 'chat' : logical; }

  function sectionFor(logical) {
    return document.querySelector('.screen[data-screen="' + sectionName(logical) + '"]');
  }

  function showScreen(logical) {
    // 대화 종료 자동 전환 타이머가 다음 화면으로 새지 않게 한다
    if (State.endAutoTimer) { clearTimeout(State.endAutoTimer); State.endAutoTimer = null; }
    var target = sectionName(logical);
    var list = document.querySelectorAll('.screen');
    for (var i = 0; i < list.length; i++) {
      list[i].classList.toggle('is-active', list[i].getAttribute('data-screen') === target);
    }
    State.screen = logical;
    var scroller = $('screens');
    if (scroller) { scroller.scrollTop = 0; }
    window.scrollTo(0, 0);
    publishLive();
  }

  function toast(text) {
    if (!D.toast) { return; }
    D.toast.textContent = text;
    D.toast.hidden = false;
    clearTimeout(D.toast._t);
    D.toast._t = setTimeout(function () { D.toast.hidden = true; }, 6000);
  }

  /* ==========================================================
     7. 세션 시작과 화면 순서 구성
     ========================================================== */

  function buildPlan(session) {
    // 재설계: 대화 3개(블록) × 9턴. 맥락은 하나이므로 주제 안내는 한 번만.
    var steps = [{ screen: 'briefing' }, { screen: 'practice' }, { screen: 'card' }];
    var convs = (session.conversations || []).slice()
      .sort(function (x, y) { return x.index - y.index; });

    convs.forEach(function (c, i) {
      steps.push({ screen: 'chat', conv: c });
      steps.push({ screen: 'survey', conv: c });
      if (i < convs.length - 1) { steps.push({ screen: 'break', conv: c }); }
    });

    steps.push({ screen: 'engagement' });   // 세션 종료 문항 (3택 규칙 탐지 등)
    steps.push({ screen: 'done' });
    return steps;
  }

  function startSession() {
    var pid = (D.pid.value || '').trim().toUpperCase();
    var group = D.group.value;

    if (!D.consentCheck.checked) { return showConsentError('동의 확인란을 체크해 주세요.'); }
    if (!/^P\d{1,3}$/.test(pid)) { return showConsentError('참가자 ID는 P01 형식으로 입력해 주세요.'); }

    D.consentError.hidden = true;
    D.btnStart.disabled = true;
    State.participantId = pid;
    State.group = group;

    post('/api/session/start',
         // ★ 테스트 패널로 시작했으면 서버가 모든 레코드에 찍는다.
         { participant_id: pid, group: group, test_mode: OPT.test }).then(function (res) {
      State.session = res;
      State.turnsTotal = res.turns_per_conversation || 9;
      State.plan = buildPlan(res);
      State.stepIndex = -1;
      try { localStorage.setItem(LAST_SESSION_KEY, res.session_id); } catch (e) { /* 무시 */ }
      lockNavigation();
      nextStep();
    }).catch(function (err) {
      D.btnStart.disabled = false;
      showConsentError('세션을 시작하지 못했습니다. 연구자를 불러주세요.');
      console.error(err);
    });
  }

  function showConsentError(msg) {
    D.consentError.textContent = msg;
    D.consentError.hidden = false;
  }

  function nextStep() {
    if (isOverlayOpen()) { return; }
    State.stepIndex += 1;
    var step = State.plan[State.stepIndex];
    if (!step) { return; }
    enterStep(step);
  }

  function enterStep(step) {
    State.conversationIndex = step.conv ? step.conv.index : null;
    State.condition = step.conv ? step.conv.condition : null;

    switch (step.screen) {
      case 'briefing':
        showScreen('briefing');
        break;

      case 'practice':
        State.conversationIndex = 0;
        State.condition = 'practice';
        startConversation({ index: 0, block: null, context: null, condition: 'practice' }, true);
        break;

      case 'card':
        renderCard();
        showScreen('card');
        break;

      case 'chat':
        startConversation(step.conv, false);
        break;

      case 'survey':
        openSurvey(step);
        break;

      case 'engagement':
        openEngagement(step);
        break;

      case 'break':
        showScreen('break');
        break;

      case 'done':
        if (D.downloadPanel) { D.downloadPanel.hidden = false; }
        finishSession();
        break;

      default:
        console.error('알 수 없는 화면: ' + step.screen);
    }
  }

  function renderCard() {
    var body = D.topicCardBody;
    clear(body);
    if (D.topicCardTitle) { D.topicCardTitle.textContent = TOPIC_CARD_TITLE; }
    TOPIC_CARD.forEach(function (para) {
      var p = el('p', null, para);
      body.appendChild(p);
    });
  }

  /* ==========================================================
     8. 대화 화면
     ========================================================== */

  function startConversation(conv, isPractice) {
    State.conversationIndex = conv.index;
    State.condition = isPractice ? 'practice' : conv.condition;
    // 설계서 PART 2: 워밍업 4턴(8초 고정·분석 제외), 본블록 9턴.
    State.turnsTotal = isPractice ? 4 : (State.session && State.session.turns_per_conversation) || 9;
    State.turnsSent = 0;
    State.prevTurnId = null;          // 대화가 바뀌면 직전 턴 연결을 끊는다 (마지막 턴은 next_input null)
    State.inputStartTs = null;
    State.pending = null;
    State.awaiting = false;
    State.sendQueue = [];

    clear(D.chatLog);
    D.chatTopic.textContent = isPractice ? '연습' : TOPIC_LINE;
    D.chatNote.textContent = isPractice ? PRACTICE_NOTE : '';
    D.chatNote.hidden = !isPractice;
    if (State.endAutoTimer) { clearTimeout(State.endAutoTimer); State.endAutoTimer = null; }
    D.endPanel.hidden = true;
    D.btnEndNext.hidden = false;
    D.composer.hidden = false;
    D.chatInput.value = '';
    setComposerEnabled(true);

    // 설계서 PART 3-4: 진행 표시 없음 — 연습·본대화 모두 카운터 숨김.
    D.chatProgress.hidden = true;
    if (D.chatCounter) { D.chatCounter.hidden = true; }

    showScreen(isPractice ? 'practice' : 'chat');
    if (!OPT.e2e) { try { D.chatInput.focus(); } catch (e) { /* 무시 */ } }
  }

  function renderCounter() {
    // 설계서 PART 3-4: 진행 표시 없음 — 카운터 렌더링은 no-op으로 유지.
    // (구 코드·E2E 잔재 호출이 있어도 화면에 아무것도 그리지 않는다.)
    if (D.chatCounter) { D.chatCounter.hidden = true; }
    if (D.chatProgress) { D.chatProgress.hidden = true; }
  }

  function setComposerEnabled(on) {
    D.chatInput.disabled = !on;
    D.btnSend.disabled = !on;
  }

  function appendMessage(role, text) {
    var wrap = el('div', 'msg msg-' + role);
    var bubble = el('div', 'bubble', text);
    wrap.appendChild(bubble);
    D.chatLog.appendChild(wrap);
    return wrap;
  }

  /* --- 대기 표시 없음 (엄격 모드) ----------------------------------
     세 조건에서 완전히 동일하다. 정지 화면 — 경과 시간·진행 바·
     남은 분량을 절대 드러내지 않는다. */

  function showIndicator() {
    // 설계서 PART 3-4 엄격 모드: 대기 표시 없음. 어떤 분기에서도 DOM에
    // 대기 버블을 그리지 않는다 (구 ?indicator 옵션은 OPT에서 제거됨).
    return;
  }

  function hideIndicator() {
    var w = $('wait-bubble');
    if (w && w.parentNode) { w.parentNode.removeChild(w); }
  }

  function scrollLogToEnd() {
    D.chatLog.scrollTop = D.chatLog.scrollHeight;
  }

  /* --- 첫 타자 입력 --------------------------------------------- */

  function markInputStart() {
    if (State.inputStartTs != null) { return; }
    // 설계서 PART 3-4: 대기 중에도 입력창 개방 — awaiting 가드를 두지 않는다.
    // 대기 중 첫 타자(B2)도 그대로 기록된다.
    var ts = nowMs();
    State.inputStartTs = ts;

    // 다음 턴의 첫 타자 = 직전 턴의 next_input_start_ts (계약 §2)
    if (State.prevTurnId) {
      var tid = State.prevTurnId;
      State.prevTurnId = null;
      var nrec = findRecord(tid);
      if (nrec) { nrec.next_input_start_ts = ts; }
      post('/api/turn/next-input', { turn_id: tid, next_input_start_ts: ts })
        .catch(function (err) { console.error(err); });
    }
  }

  /* --- 전송 ------------------------------------------------------ */

  function sendCurrentInput() {
    if (State.screen !== 'chat' && State.screen !== 'practice') { return; }
    if (State.turnsSent + State.sendQueue.length >= State.turnsTotal) { return; }
    if (isOverlayOpen()) { return; }

    var text = D.chatInput.value;
    if (!text || !text.trim()) { return; }

    // 설계서 PART 3-4: 대기 중 전송은 큐에 넣고 현재 턴 종료 후 처리 (B1).
    // 입력창은 잠그지 않는다 — 대기 중에도 타이핑·전송이 된다.
    // 단, 날아간 메시지와 같은 내용의 재전송(연타·불안 반응)은 턴으로 만들지
    // 않고 B1(견딤 곤란 지표)로만 기록한다. 중복 턴 방지.
    if (State.awaiting) {
      var norm = text.trim();
      var dup = (State.inflightText != null && norm === State.inflightText.trim());
      if (!dup) {
        for (var qi = 0; qi < State.sendQueue.length; qi++) {
          if (State.sendQueue[qi].text.trim() === norm) { dup = true; break; }
        }
      }
      if (dup) {
        var ev = { kind: 'B1_duplicate_ignored', ts: nowMs(), screen: State.screen,
          conversation_index: State.conversationIndex };
        State.attentionEvents.push(ev);
        postEvent('B1_duplicate_ignored', ev);
        D.chatInput.value = '';
        State.inputStartTs = null;
        toast('이미 전송되었습니다.');
        publishLive();
        return;
      }
      var qStart = State.inputStartTs != null ? State.inputStartTs : nowMs();
      var bubble = appendMessage('user', text);
      scrollLogToEnd();
      State.sendQueue.push({
        text: text, startTs: qStart, queuedAtTs: nowMs(), bubble: bubble, queued: true
      });
      D.chatInput.value = '';
      State.inputStartTs = null;
      publishLive();
      return;
    }
    doSubmit(text, State.inputStartTs, null, false);
  }

  function doSubmit(text, startTs, existingBubble, queued) {
    var submit = nowMs();                       // t0
    if (startTs == null) { startTs = submit; }  // 붙여넣기 등 입력 이벤트가 없던 경우
    State.inflightText = text;

    var turnIndex = State.turnsSent + 1;
    var userBubble = existingBubble || appendMessage('user', text);
    scrollLogToEnd();
    D.chatInput.value = '';
    // 엄격 모드: 대기 중에도 입력창을 열어 둔다 (잠금 없음).
    setComposerEnabled(true);
    State.awaiting = true;
    State.inputStartTs = null;

    State.turnsSent = turnIndex;                // 전송 직후 증가 (내부 상태용, 화면 미표시)
    publishLive();

    showIndicator();

    post('/api/turn', {
      session_id: State.session.session_id,
      participant_id: State.participantId,
      group: State.group,
      conversation_index: State.conversationIndex,
      turn_index: turnIndex,
      text: text,
      user_input_start_ts: startTs,
      user_input_submit_ts: submit,
      queued_during_wait: !!queued,
      // 서버가 무상태일 수 있다 (서버리스). 이 대화의 이력만 함께 보낸다.
      history: State.records
        .filter(function (r) { return r.conversation_index === State.conversationIndex; })
        .reduce(function (acc, r) {
          acc.push({ role: 'user', content: r.user_input_text });
          acc.push({ role: 'assistant', content: r.ai_response_text });
          return acc;
        }, [])
    }).then(function (res) {
      recordTurn(res, submit, startTs, text, State.screen === 'practice', !!queued);
      scheduleDisplay(res, submit);
    }).catch(function (err) {
      console.error(err);
      // 턴이 성립하지 않았으므로 카운터를 되돌리고 재전송할 수 있게 둔다.
      // 화면에 붙인 사용자 말풍선도 함께 거둔다 — 남겨 두면 재전송 때 같은
      // 문장이 두 번 보이고, 화면 대화 기록이 로그의 턴 순서와 어긋난다.
      // (큐로 들어온 말풍선은 거두지 않고 큐에 그대로 둔다.)
      hideIndicator();
      if (!queued && userBubble && userBubble.parentNode) { userBubble.parentNode.removeChild(userBubble); }
      State.awaiting = false;
      State.inflightText = null;
      State.turnsSent = turnIndex - 1;
      D.chatInput.value = text;
      State.inputStartTs = startTs;
      setComposerEnabled(true);
      toast('화면에 문제가 있습니다. 연구자를 불러주세요.');
      publishLive();
      rejectDisplayWaiters(err);
    });
  }

  /* --- 표시 예약 (계약 §7 그대로) -------------------------------- */

  function recordTurn(res, submit, startTs, text, practice, queued) {
    var rec = {
      session_id: State.session ? State.session.session_id : null,
      participant_id: State.participantId,
      group: State.group,
      conversation_index: State.conversationIndex,
      condition: res.condition, depth: res.depth,
      turn_index: State.turnsSent, practice: !!practice,
      user_input_start_ts: startTs, user_input_submit_ts: submit,
      user_input_text: text, user_input_chars: text.length,
      queued_during_wait: !!queued,   // B1: 대기 중 큐로 접수된 전송
      target_delay_ms: res.target_delay_ms,
      llm_request_ts: res.llm_request_ts, llm_response_ts: res.llm_response_ts,
      display_ts: null,
      ai_response_text: res.reply, ai_response_chars: (res.reply || '').length,
      next_input_start_ts: null,
      safety_flag: !!res.safety_flag, manipulation_ok: null,
      prompt_version: State.session ? State.session.prompt_version : null,
      base_prompt_sha256: res.base_prompt_sha256,
      prompt_sha256: res.prompt_sha256,
      model: res.model, finish_reason: res.finish_reason,
      delay_scale: State.session ? State.session.delay_scale : 1,
      turn_id: res.turn_id
    };
    State.records.push(rec);
    return rec;
  }

  function findRecord(turnId) {
    for (var i = State.records.length - 1; i >= 0; i--) {
      if (State.records[i].turn_id === turnId) { return State.records[i]; }
    }
    return null;
  }

  function buildJsonl() {
    return State.records.map(function (r) {
      var o = {}; Object.keys(r).forEach(function (k) { if (k !== 'turn_id') { o[k] = r[k]; } });
      return JSON.stringify(o);
    }).join('\n') + '\n';
  }

  function csvCell(v) {
    if (v == null) { return ''; }
    var t = String(v).replace(/"/g, '""');
    return (/[",\n]/.test(t)) ? '"' + t + '"' : t;
  }

  function buildTurnsCsv() {
    var cols = ['conversation_index', 'turn_index', 'practice', 'condition', 'depth',
      'user_input_chars', 'ai_response_chars', 'target_delay_ms', 'observed_delay_ms',
      'display_error_ms', 'queued_during_wait', 'safety_flag', 'manipulation_ok',
      'prompt_sha256', 'model', 'finish_reason'];
    var lines = [cols.join(',')];
    State.records.forEach(function (r) {
      lines.push(cols.map(function (c) {
        if (c === 'observed_delay_ms') {
          return (r.display_ts != null) ? (r.display_ts - r.user_input_submit_ts) : '';
        }
        if (c === 'display_error_ms') {
          return (r.display_ts != null) ? (r.display_ts - (r.user_input_submit_ts + r.target_delay_ms)) : '';
        }
        return csvCell(r[c]);
      }).join(','));
    });
    return lines.join('\n') + '\n';
  }

  function buildSurveysCsv() {
    var cols = ['kind', 'conversation_index', 'time_estimate_sec', 'time_estimate_unknown',
      'discomfort', 'one_word', 'effort', 'pets_1', 'pets_2', 'pets_3', 'pets_4', 'pets_5',
      'pets_6', 'god_1', 'god_2', 'god_3', 'god_4', 'god_5',
      'engagement_1', 'engagement_2', 'engagement_3', 'engagement_4', 'engagement_index',
      'rule_guess', 'shown_ts', 'submitted_ts'];
    var lines = [cols.join(',')];
    State.surveys.forEach(function (p) {
      var r = p.responses || {};
      lines.push(cols.map(function (c) {
        if (c === 'kind') { return csvCell(p.kind); }
        if (c === 'conversation_index') { return csvCell(p.conversation_index); }
        if (c === 'shown_ts') { return csvCell(p.shown_ts); }
        if (c === 'submitted_ts') { return csvCell(p.submitted_ts); }
        return csvCell(r[c]);
      }).join(','));
    });
    return lines.join('\n') + '\n';
  }

  function downloadLogs() {
    var sid = (State.session && State.session.session_id) || 'session';
    [[sid + '.turns.jsonl', buildJsonl()],
     [sid + '.surveys.jsonl', State.surveys.map(function (x) { return JSON.stringify(x); }).join('\n') + '\n'],
     [sid + '.attention.jsonl', State.attentionEvents.map(function (a) { return JSON.stringify(a); }).join('\n') + '\n'],
     [sid + '.turns.csv', buildTurnsCsv()],
     [sid + '.surveys.csv', buildSurveysCsv()]
    ].forEach(function (pair) {
      var blob = new Blob([pair[1]], { type: 'application/x-ndjson' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = pair[0];
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
    });
  }

  function scheduleDisplay(res, submit) {
    // deadline은 계약서 코드와 동일하게 클라이언트가 계산한다.
    // 서버의 deadline_ts는 같은 값이어야 하며, 다르면 로그에 남긴다.
    var deadline = submit + res.target_delay_ms;
    if (typeof res.deadline_ts === 'number' && res.deadline_ts !== deadline) {
      console.warn('deadline 불일치: 서버 ' + res.deadline_ts + ' / 화면 ' + deadline);
    }

    var pending = {
      turnId: res.turn_id,
      submit: submit,
      deadline: deadline,
      targetDelayMs: res.target_delay_ms,
      reply: res.reply,
      safety: !!res.safety_flag,
      bypass: !!res.bypass_delay,
      condition: res.condition || State.condition,
      displayed: false,
      guard: null
    };
    State.pending = pending;

    if (pending.bypass || nowMs() >= deadline) {
      show(pending);                                       // 즉시 표시
      return;
    }

    setTimeout(function () { spin(pending); }, deadline - nowMs() - 40);

    // requestAnimationFrame이 굶는 상황(탭 비활성 등) 대비 안전망.
    // deadline 이후에만 발화하므로 조기 표시는 일어나지 않는다.
    pending.guard = setTimeout(function () {
      if (!pending.displayed && nowMs() >= pending.deadline) { show(pending); }
    }, Math.max(0, deadline - nowMs()) + 250);
  }

  function spin(p) {
    if (p.displayed) { return; }
    if (nowMs() >= p.deadline) { show(p); }
    else { requestAnimationFrame(function () { spin(p); }); }
  }

  /* --- 표시 ------------------------------------------------------ */

  function show(p) {
    if (p.displayed) { return; }

    // 구조적 방어: bypass가 아닌 턴은 어떤 경로로도 마감 전에 표시되지 않는다.
    if (!p.bypass && nowMs() < p.deadline) { spin(p); return; }

    p.displayed = true;
    if (p.guard) { clearTimeout(p.guard); p.guard = null; }

    hideIndicator();
    appendMessage('ai', p.reply);          // ← DOM 삽입
    var displayTs = nowMs();               // ← 삽입 직후 즉시 기록
    scrollLogToEnd();

    State.pending = null;
    State.awaiting = false;
    State.inflightText = null;
    State.prevTurnId = p.turnId;
    State.lastDisplay = {
      turnId: p.turnId,
      deadline: p.deadline,
      displayTs: displayTs,
      error: displayTs - p.deadline
    };
    State.displayLog.push({
      turnId: p.turnId,
      deadline: p.deadline,
      displayTs: displayTs,
      error: displayTs - p.deadline,
      bypass: p.bypass,
      manipulationOk: null
    });

    var clientRec = findRecord(p.turnId);
    if (clientRec) {
      clientRec.display_ts = displayTs;
      clientRec.manipulation_ok = !clientRec.safety_flag && !clientRec.practice
        && clientRec.llm_response_ts <= clientRec.user_input_submit_ts + clientRec.target_delay_ms;
    }

    var record = State.displayLog[State.displayLog.length - 1];
    var done = post('/api/turn/display', { turn_id: p.turnId, display_ts: displayTs })
      .then(function (r) {
        record.manipulationOk = (r && typeof r.manipulation_ok === 'boolean') ? r.manipulation_ok : null;
        if (r && typeof r.display_error_ms === 'number') { record.serverError = r.display_error_ms; }
        publishLive();
      })
      .catch(function (err) { console.error(err); });

    afterDisplay(p);
    publishLive();

    // e2e send()는 표시 기록이 서버에 닿은 뒤 해소한다 (로그 유실 방지).
    done.then(resolveDisplayWaiters, resolveDisplayWaiters);
  }

  function afterDisplay(p) {
    var conversationOver = State.turnsSent >= State.turnsTotal;

    if (conversationOver) {
      D.composer.hidden = true;
      D.endPanel.hidden = false;
      if (State.screen === 'practice') {
        D.endText.textContent = '연습이 끝났습니다.';
        D.btnEndNext.textContent = '다음';
      } else {
        // 명세서 §4 — 갑자기 바뀌면 대화의 여운이 끊긴다. 2초 표시 후 자동 전환.
        D.endText.textContent = '이번 대화가 끝났습니다.';
        D.btnEndNext.hidden = true;
        State.endAutoTimer = setTimeout(function () {
          D.btnEndNext.hidden = false;
          if (State.screen === 'chat') { nextStep(); }
        }, 2000);
      }
    } else {
      setComposerEnabled(true);
      if (!OPT.e2e) { try { D.chatInput.focus(); } catch (e) { /* 무시 */ } }
    }

    if (p.safety) { openSafetyOverlay(p); return; }

    // 대기 중 큐(B1) 드레인: 현재 턴 표시 직후 다음 큐 메시지를 새 턴으로 전송.
    // 말풍선은 큐 접수 시점에 이미 붙였으므로 재사용한다.
    if (State.sendQueue.length && State.turnsSent < State.turnsTotal &&
        (State.screen === 'chat' || State.screen === 'practice')) {
      var q = State.sendQueue.shift();
      doSubmit(q.text, q.startTs, q.bubble, true);
    }
  }

  function resolveDisplayWaiters() {
    var list = displayWaiters;
    displayWaiters = [];
    list.forEach(function (w) { try { w.resolve(); } catch (e) { console.error(e); } });
  }

  function rejectDisplayWaiters(err) {
    var list = displayWaiters;
    displayWaiters = [];
    list.forEach(function (w) { try { w.reject(err); } catch (e) { console.error(e); } });
  }

  /* ==========================================================
     9. 안전 경로
     ========================================================== */

  function isOverlayOpen() { return D.safetyOverlay && !D.safetyOverlay.hidden; }

  function openSafetyOverlay(p) {
    State.safetyEvents.push({
      ts: nowMs(),
      conversation_index: State.conversationIndex,
      turn_index: State.turnsSent,
      turn_id: p ? p.turnId : null,
      condition: State.condition,
    });
    setComposerEnabled(false);
    D.safetyUnlock.checked = false;
    D.safetyContinue.disabled = true;
    D.safetyEnd.disabled = true;
    D.safetyOverlay.hidden = false;
    publishLive();
  }

  function closeSafetyOverlay() {
    D.safetyOverlay.hidden = true;
    // 다음 대화로 자동 진행하지 않는다. 현재 대화 상태만 복원한다.
    if (State.screen === 'chat' || State.screen === 'practice') {
      if (State.turnsSent < State.turnsTotal) {
        D.composer.hidden = false;
        setComposerEnabled(true);
      }
    }
    publishLive();
  }

  function endSessionEarly() {
    D.safetyOverlay.hidden = true;
    State.stepIndex = State.plan.length - 1;   // done 단계
    finishSession();
  }

  /* ==========================================================
     10. 설문
     ========================================================== */

  function scaleRow(container, name, opts) {
    var item = el('div', 'scale-item');
    var text = el('p', 'scale-text', opts.text);
    item.appendChild(text);

    var scale = el('div', 'scale');
    scale.appendChild(el('span', 'anchor left', opts.left));

    var ticks = el('div', 'ticks');
    for (var v = opts.min; v <= opts.max; v++) {
      var lab = el('label', 'tick');
      var input = document.createElement('input');
      input.type = 'radio';
      input.setAttribute('name', name);
      input.setAttribute('value', String(v));
      lab.appendChild(input);
      lab.appendChild(el('span', null, String(v)));
      ticks.appendChild(lab);
    }
    scale.appendChild(ticks);
    scale.appendChild(el('span', 'anchor right', opts.right));
    item.appendChild(scale);
    container.appendChild(item);
  }

  function radioValue(name) {
    var checked = document.querySelector('input[name="' + name + '"]:checked');
    return checked ? Number(checked.getAttribute('value')) : null;
  }

  function buildSurveyForm() {
    clear(D.qDiscomfort);
    scaleRow(D.qDiscomfort, 'discomfort', {
      text: '', min: 1, max: 7,
      left: '전혀 불편하지 않았다', right: '매우 불편했다'
    });

    clear(D.qEffort);
    scaleRow(D.qEffort, 'effort', {
      text: '', min: 1, max: 7,
      left: '전혀 공을 들이지 않았다', right: '매우 공을 들였다'
    });

    clear(D.qPets);
    PETS_ITEMS.forEach(function (t, i) {
      scaleRow(D.qPets, 'pets_' + (i + 1), {
        text: t, min: 1, max: 7,
        left: '전혀 그렇지 않다', right: '매우 그렇다'
      });
    });

    clear(D.qGod);
    GOD_ITEMS.forEach(function (it) {
      scaleRow(D.qGod, it.key, {
        text: '', min: 1, max: 5, left: it.left, right: it.right
      });
    });
  }

  function showSurveyPart(n) {
    State.surveyPart = n;
    D.surveyParts.forEach(function (el, i) { el.hidden = (i + 1) !== n; });
    if (D.surveyPartLine) { D.surveyPartLine.textContent = n + ' / ' + D.surveyParts.length; }
    if (D.btnSurveyNext) {
      D.btnSurveyNext.textContent = (n < D.surveyParts.length) ? '다음' : '제출';
    }
    window.scrollTo(0, 0);
    var host = $('screens');
    if (host) { host.scrollTop = 0; }
  }

  function resetSurveyForm() {
    buildSurveyForm();
    showSurveyPart(1);
    if (D.qWord) { D.qWord.value = ''; }
    if (D.qTime) { D.qTime.value = ''; }
    if (D.qTimeUnknown) { D.qTimeUnknown.checked = false; }
    D.surveyError.hidden = true;
  }

  function openSurvey(step) {
    resetSurveyForm();
    State.surveyShownTs = nowMs();
    showScreen('survey');
  }

  function submitSurvey() {
    var missing = [];
    if (State.surveyPart < D.surveyParts.length) {
      if (State.surveyPart === 1) {
        var tRaw = D.qTime ? D.qTime.value.trim() : '';
        var tUnknown = D.qTimeUnknown ? D.qTimeUnknown.checked : false;
        if (!tUnknown && (tRaw === '' || isNaN(Number(tRaw)))) { missing.push('①-시간'); }
        if (radioValue('discomfort') == null) { missing.push('②-불편'); }
        if (!(D.qWord && D.qWord.value.trim())) { missing.push('③-한단어'); }
      } else if (State.surveyPart === 2) {
        if (radioValue('effort') == null) { missing.push('④-노력'); }
      } else if (State.surveyPart === 3) {
        for (var pi = 1; pi <= PETS_ITEMS.length; pi++) {
          if (radioValue('pets_' + pi) == null) { missing.push('⑤-' + pi); }
        }
      }
      if (missing.length) {
        D.surveyError.textContent = '아직 답하지 않은 항목이 있습니다: ' + missing.join(', ');
        D.surveyError.hidden = false;
        return Promise.resolve(false);
      }
      D.surveyError.hidden = true;
      showSurveyPart(State.surveyPart + 1);
      return Promise.resolve(false);      // 아직 제출하지 않는다
    }
    GOD_ITEMS.forEach(function (it) {
      if (radioValue(it.key) == null) { missing.push('⑥-' + it.key); }
    });

    if (missing.length) {
      D.surveyError.textContent = '아직 답하지 않은 항목이 있습니다: ' + missing.join(', ');
      D.surveyError.hidden = false;
      return Promise.resolve(false);
    }
    D.surveyError.hidden = true;

    var tVal = D.qTime ? D.qTime.value.trim() : '';
    var tUnk = D.qTimeUnknown ? D.qTimeUnknown.checked : false;
    var responses = {
      time_estimate_sec: tUnk ? null : Number(tVal),
      time_estimate_unknown: !!tUnk,
      discomfort: radioValue('discomfort'),
      one_word: D.qWord ? D.qWord.value.trim() : '',
      effort: radioValue('effort'),
      condition: State.condition,
      conversation_index: State.conversationIndex
    };
    for (var i = 1; i <= PETS_ITEMS.length; i++) { responses['pets_' + i] = radioValue('pets_' + i); }
    GOD_ITEMS.forEach(function (it) { responses[it.key] = radioValue(it.key); });

    return sendSurvey('per_condition', responses);
  }

  function buildEngagementForm() {
    var picked = document.querySelector('input[name="rule_guess"]:checked');
    if (picked) { picked.checked = false; }
    clear(D.qEngagement);
    ENGAGEMENT_ITEMS.forEach(function (it) {
      scaleRow(D.qEngagement, it.key, {
        text: it.text, min: 1, max: 5, left: it.left, right: it.right
      });
    });
    D.engagementError.hidden = true;
  }

  function openEngagement() {
    buildEngagementForm();
    State.surveyShownTs = nowMs();
    showScreen('engagement');
  }

  function submitEngagement() {
    var missing = [];
    var guess = document.querySelector('input[name="rule_guess"]:checked');
    if (!guess) { missing.push('가-규칙'); }
    ENGAGEMENT_ITEMS.forEach(function (it) {
      if (radioValue(it.key) == null) { missing.push('나-' + it.key); }
    });
    if (missing.length) {
      D.engagementError.textContent = '아직 답하지 않은 항목이 있습니다: ' + missing.join(', ');
      D.engagementError.hidden = false;
      return Promise.resolve(false);
    }
    D.engagementError.hidden = true;

    var responses = { rule_guess: guess.value };
    ENGAGEMENT_ITEMS.forEach(function (it) { responses[it.key] = radioValue(it.key); });
    var raw4 = radioValue('engagement_4');
    responses.engagement_4_reversed = (raw4 == null) ? null : (6 - raw4);
    var vals = [responses.engagement_1, responses.engagement_2,
                responses.engagement_3, responses.engagement_4_reversed];
    responses.engagement_index = vals.some(function (v) { return v == null; })
      ? null : Math.round((vals.reduce(function (a, b) { return a + b; }, 0) / 4) * 100) / 100;
    return sendSurvey('session_end', responses);
  }

  function sendSurvey(kind, responses) {
    var payload = {
      session_id: State.session.session_id,
      participant_id: State.participantId,     // materials/05 "로그에 남길 것"
      kind: kind,
      conversation_index: State.conversationIndex,
      shown_ts: State.surveyShownTs,
      submitted_ts: nowMs(),
      test_mode: OPT.test,
      responses: responses
    };
    State.surveys.push(payload);
    var p = post('/api/survey', payload).catch(function (err) {
      console.error(err);
      // 참가자를 화면에 붙잡아 두지는 않는다. 대신 응답을 브라우저에 남겨
      // 나중에 연구자가 회수할 수 있게 한다 — 조용한 유실을 막는다.
      try {
        var kept = JSON.parse(localStorage.getItem(SURVEY_FAIL_KEY) || '[]');
        kept.push(payload);
        localStorage.setItem(SURVEY_FAIL_KEY, JSON.stringify(kept));
      } catch (e) { /* localStorage 없음 — 무시 */ }
      toast('설문 저장에 문제가 있었습니다. 연구자를 불러주세요.');
    }).then(function () {
      nextStep();          // 응답이 저장된 뒤 진행. 뒤로 가기는 없다.
      return true;
    });
    State.lastSubmit = p;
    return p;
  }

  /* ==========================================================
     11. 종료
     ========================================================== */

  function finishSession() {
    showScreen('done');
    State.doneReached = true;
    if (!State.session) { return; }
    State.endPending = true;
    post('/api/session/' + encodeURIComponent(State.session.session_id) + '/end', {})
      .catch(function (err) { console.error(err); })
      .then(function () {
        State.endPending = false;
        publishLive();
      });
  }

  /* ==========================================================
     12. 진행 상황 공유 (연구자 화면용, 같은 브라우저 안에서만)
     ========================================================== */

  function publishLive() {
    if (OPT.researcher) { return; }
    try {
      var payload = {
        updated_ts: nowMs(),
        session_id: State.session ? State.session.session_id : null,
        participant_id: State.participantId,
        group: State.group,
        participant_number: State.session ? State.session.participant_number : null,
        block_order: State.session ? State.session.block_order : null,
        conversations: State.session ? State.session.conversations : null,
        model: State.session ? State.session.model : null,
        empathy_variant: State.session ? State.session.empathy_variant : null,
        prompt_version: State.session ? State.session.prompt_version : null,
        screen: State.screen,
        conversation_index: State.conversationIndex,
        condition: State.condition,
        condition: State.condition,
        turns_sent: State.turnsSent,
        turns_total: State.turnsTotal,
        safety_events: State.safetyEvents,
        attention_events: State.attentionEvents,
        queue_len: State.sendQueue.length,
        display_log: State.displayLog.slice(-40),
        options: OPT
      };
      localStorage.setItem(LIVE_KEY, JSON.stringify(payload));
    } catch (e) { /* localStorage 없음 — 무시 */ }
  }

  /* ==========================================================
     13. 연구자 화면
     ========================================================== */

  var RS = { sessionId: null, timer: null, lastPlan: null };

  function initResearcher() {
    document.title = '연구자 화면 · AI 대화 경험 연구';
    D.researcherApp.hidden = false;

    var fromUrl = params.get('session');
    var live = readLive();
    var stored = null;
    try { stored = localStorage.getItem(LAST_SESSION_KEY); } catch (e) { stored = null; }

    RS.sessionId = fromUrl || (live && live.session_id) || stored || null;
    if (RS.sessionId) { D.rsSessionId.value = RS.sessionId; }
    D.rsLoad.addEventListener('click', function () {
      RS.sessionId = (D.rsSessionId.value || '').trim() || null;
      refreshResearcher();
    });

    // 짝지은 회상 (연구자 화면 전용 배선 — init 후반부는 연구자 분기에서 실행 안 됨)
    if (D.rsRecallLoad) { D.rsRecallLoad.addEventListener('click', loadRecall); }
    if (D.rsRecallPrint) { D.rsRecallPrint.addEventListener('click', function () { window.print(); }); }

    window.addEventListener('storage', function (e) {
      if (e.key === LIVE_KEY) { renderResearcher(readLive(), RS.lastPlan); }
    });

    refreshResearcher();
    RS.timer = setInterval(refreshResearcher, 2000);
  }

  function readLive() {
    try {
      var raw = localStorage.getItem(LIVE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  function refreshResearcher() {
    var live = readLive();
    if (!RS.sessionId && live && live.session_id) {
      RS.sessionId = live.session_id;
      D.rsSessionId.value = RS.sessionId;
    }
    if (!RS.sessionId) { renderResearcher(live, null); return; }

    get('/api/session/' + encodeURIComponent(RS.sessionId) + '/plan').then(function (plan) {
      RS.lastPlan = plan;
      renderResearcher(live, plan);
    }).catch(function (err) {
      RS.lastPlan = { error: String(err && err.message ? err.message : err) };
      renderResearcher(live, RS.lastPlan);
    });
  }

  // 서버 응답 모양이 조금씩 달라도 견디도록 여러 후보 키를 훑는다.
  // server.py의 /api/session/{id}/plan은 세션 계획을 `plan` 아래에 중첩해 돌려주므로
  // (participant_number / block_order / conversations) 최상위와 plan 양쪽을 본다.
  function firstOf(obj, keys) {
    if (!obj) { return null; }
    var scopes = [obj, obj.plan];
    for (var s = 0; s < scopes.length; s++) {
      var o = scopes[s];
      if (!o || typeof o !== 'object') { continue; }
      for (var i = 0; i < keys.length; i++) {
        if (o[keys[i]] != null) { return o[keys[i]]; }
      }
    }
    return null;
  }

  function renderResearcher(live, plan) {
    var planBody = plan && plan.error ? null : plan;
    var conversations = firstOf(planBody, ['conversations']) ||
                        (live && live.conversations) || [];
    var safety = firstOf(planBody, ['safety_events', 'safety_alerts', 'alerts', 'safety']) ||
                 (live && live.safety_events) || [];

    // 메타
    clear(D.rsMeta);
    var meta = [
      ['세션 ID', RS.sessionId || '—'],
      ['참가자', String(firstOf(planBody, ['participant_id']) || (live && live.participant_id) || '—') +
                 ' (' + String(firstOf(planBody, ['participant_number']) || (live && live.participant_number) || '—') + ')'],
      ['집단', String(firstOf(planBody, ['group']) || (live && live.group) || '—')],
      ['블록 순서', JSON.stringify(firstOf(planBody, ['block_order']) || (live && live.block_order) || [])],
      ['모델', String(firstOf(planBody, ['model']) || (live && live.model) || '—')],
      ['프롬프트', String(firstOf(planBody, ['prompt_version']) || (live && live.prompt_version) || '—') +
                   ' / 공감 ' + String(firstOf(planBody, ['empathy_variant']) || (live && live.empathy_variant) || '—')],
      ['화면 옵션', live && live.options
        ? ('indicator=' + live.options.indicator + ' · progress=' + (live.options.progress ? 'on' : 'off'))
        : '—']
    ];
    meta.forEach(function (kv) {
      D.rsMeta.appendChild(el('dt', null, kv[0]));
      D.rsMeta.appendChild(el('dd', null, kv[1]));
    });

    // 현재 진행 위치
    clear(D.rsPosition);
    if (live && live.session_id === RS.sessionId) {
      var line1 = el('div');
      line1.appendChild(document.createTextNode('화면 '));
      line1.appendChild(el('span', 'rs-now', String(live.screen)));
      D.rsPosition.appendChild(line1);

      if (live.conversation_index != null) {
        var line2 = el('div', null,
          '대화 ' + live.conversation_index +
          (live.block ? ' · 블록 ' + live.block : '') +
          (live.context ? ' · 맥락 ' + live.context : '') +
          (live.condition ? ' · 조건 ' + live.condition : '') +
          ' · 턴 ' + live.turns_sent + ' / ' + live.turns_total);
        D.rsPosition.appendChild(line2);
      }
      var age = Math.max(0, nowMs() - (live.updated_ts || 0));
      D.rsPosition.appendChild(el('div', 'rs-note', '참가자 화면 갱신 ' + Math.round(age / 1000) + '초 전'));
    } else {
      D.rsPosition.appendChild(el('div', 'rs-none',
        '이 브라우저에서 진행 중인 참가자 화면이 없습니다. (계획표와 경보는 서버에서 읽습니다)'));
    }

    // 안전 경보
    clear(D.rsSafety);
    var safetyList = Array.isArray(safety) ? safety : [];
    if (!safetyList.length) {
      D.rsSafety.appendChild(el('div', 'rs-none', '경보 없음'));
    } else {
      safetyList.forEach(function (s) {
        var box = el('div', 'rs-safety-item');
        var when = s.ts || s.timestamp || s.display_ts || s.user_input_submit_ts;
        box.appendChild(el('div', null, '안전 경로 발동' +
          (s.conversation_index != null ? ' · 대화 ' + s.conversation_index : '') +
          (s.turn_index != null ? ' · 턴 ' + s.turn_index : '')));
        box.appendChild(el('div', 'rs-note', when ? new Date(when).toLocaleString('ko-KR') : ''));
        D.rsSafety.appendChild(box);
      });
    }

    // 계획표
    var tbody = D.rsPlan.querySelector('tbody');
    clear(tbody);
    var nowConv = live && live.session_id === RS.sessionId ? live.conversation_index : null;

    var practiceRow = document.createElement('tr');
    ['0', 'practice', '연습 (고정 8초)', '4', nowConv === 0 ? '진행 중' : (nowConv != null && nowConv > 0 ? '완료' : '대기')]
      .forEach(function (t) { practiceRow.appendChild(el('td', null, t)); });
    if (nowConv === 0) { practiceRow.className = 'is-now'; }
    tbody.appendChild(practiceRow);

    (conversations || []).forEach(function (c) {
      var status = '대기';
      if (nowConv != null) {
        if (c.index === nowConv) { status = '진행 중'; }
        else if (c.index < nowConv) { status = '완료'; }
      }
      var tr = document.createElement('tr');
      var PLACE = { R1: '깊음←길게', R2: '깊음←짧게', R3: '규칙 없음' };
      [String(c.index), String(c.condition), PLACE[c.condition] || '—', String(c.turns || 9), status]
        .forEach(function (t) { tr.appendChild(el('td', null, t)); });
      if (c.index === nowConv) { tr.className = 'is-now'; }
      tbody.appendChild(tr);
    });

    // 표시 오차
    var dbody = D.rsDisplay.querySelector('tbody');
    clear(dbody);
    var log = (live && live.session_id === RS.sessionId && live.display_log) || [];
    log.slice().reverse().forEach(function (r) {
      var tr = document.createElement('tr');
      tr.appendChild(el('td', null, String(r.turnId)));
      tr.appendChild(el('td', null, String(r.deadline)));
      tr.appendChild(el('td', null, String(r.displayTs)));
      var errTd = el('td', null, (r.error > 0 ? '+' : '') + r.error);
      if (Math.abs(r.error) > 200) { errTd.className = 'bad'; }
      tr.appendChild(errTd);
      var okTd = el('td', null, r.bypass ? 'bypass' : (r.manipulationOk === null ? '—' : (r.manipulationOk ? 'ok' : 'FAIL')));
      if (r.manipulationOk === false) { okTd.className = 'bad'; }
      tr.appendChild(okTd);
      dbody.appendChild(tr);
    });

    D.rsRaw.textContent = JSON.stringify(plan || {}, null, 2);
    D.rsUpdated.textContent = new Date(nowMs()).toLocaleTimeString('ko-KR');
  }

  /* ==========================================================
     14. 뒤로 가기 차단
     ========================================================== */

  function lockNavigation() {
    try {
      history.pushState({ exp: 1 }, '', location.href);
      window.addEventListener('popstate', function () {
        history.pushState({ exp: 1 }, '', location.href);
      });
    } catch (e) { /* 무시 */ }

    if (!OPT.e2e) {
      window.addEventListener('beforeunload', function (e) {
        if (State.doneReached) { return; }
        e.preventDefault();
        e.returnValue = '';
      });
    }
  }

    function postEvent(kind, extra) {
    if (!State.session) { return; }
    var body = { session_id: State.session.session_id, kind: kind, ts: nowMs() };
    if (extra) { for (var k in extra) { if (k !== 'kind' && k !== 'ts' && k !== 'session_id') { body[k] = extra[k]; } } }
    post('/api/event', body).catch(function (err) { console.error(err); });
  }

  /* ==========================================================
     14b. 짝지은 자극 회상 (연구자 화면 · 조건명 숨김)
     같은 깊이·다른 지연 쌍을 자동 추출. 참가자에게 보여줄 때는
     대기 시간만 표시하고 조건·목표지연은 절대 내지 않는다.
     ========================================================== */

  function observedSec(t) {
    if (t.display_ts == null || t.user_input_submit_ts == null) { return null; }
    return Math.round((t.display_ts - t.user_input_submit_ts) / 100) / 10;
  }

  function truncText(t, n) {
    t = String(t || '');
    return t.length > n ? t.slice(0, n) + '…' : t;
  }

  function pickPair(turns, depth) {
    // 같은 깊이·다른 지연 쌍. 선택 기준은 목표 지연의 양극단(서로 다른 대화),
    // 화면 표시·면담 발화는 관측 대기만 쓴다. 조건명·목표값은 절대 안 나감.
    var cand = turns.filter(function (t) {
      return !t.practice && !t.safety_flag && t.depth === depth && t.display_ts != null;
    });
    if (cand.length < 2) { return null; }
    var sorted = cand.slice().sort(function (a, b) { return b.target_delay_ms - a.target_delay_ms; });
    var slow = sorted[0], fast = null;
    for (var i = sorted.length - 1; i > 0; i--) {
      if (sorted[i].conversation_index !== slow.conversation_index &&
          sorted[i].target_delay_ms < slow.target_delay_ms) { fast = sorted[i]; break; }
    }
    if (!fast) { return null; }
    return {
      slow: { turn: slow, obs: slow.display_ts - slow.user_input_submit_ts },
      fast: { turn: fast, obs: fast.display_ts - fast.user_input_submit_ts }
    };
  }

  function renderRecall(turns) {
    clear(D.rsRecall);
    if (!turns || !turns.length) {
      D.rsRecall.appendChild(el('div', 'rs-none', '턴 기록이 없습니다.'));
      return;
    }
    var DEPTH_KO = { deep: '깊은 답', medium: '보통 답', shallow: '얕은 답' };
    [['deep', '깊은 답 쌍 (같은 깊이 · 다른 대기)'],
     ['shallow', '얕은 답 쌍 (같은 깊이 · 다른 대기)']].forEach(function (spec) {
      var pair = pickPair(turns, spec[0]);
      var box = el('div', 'rs-recall-pair');
      box.appendChild(el('h3', null, spec[1]));
      if (!pair) {
        box.appendChild(el('div', 'rs-none', '해당 쌍을 찾지 못했습니다 (표시 완료 턴 부족).'));
      } else {
        [pair.slow, pair.fast].forEach(function (o, idx) {
          var card = el('div', 'rs-recall-card');
          card.appendChild(el('p', 'rs-recall-meta',
            (idx === 0 ? 'A' : 'B') + ' · ' + (DEPTH_KO[spec[0]] || spec[0]) +
            ' · 대기 약 ' + observedSec(o.turn) + '초'));
          card.appendChild(el('p', null, '참가자: ' + truncText(o.turn.user_input_text, 200)));
          card.appendChild(el('p', null, 'AI: ' + truncText(o.turn.ai_response_text, 300)));
          box.appendChild(card);
        });
        var ask = el('p', 'rs-note', '면담 질문 예: "A와 B가 각각 어떠셨어요?" (조건명은 말하지 않습니다)');
        box.appendChild(ask);
      }
      D.rsRecall.appendChild(box);
    });

    var all = el('div', 'rs-recall-all');
    all.appendChild(el('h3', null, '전체 턴 (대기 시간만 표기)'));
    var byConv = {};
    turns.forEach(function (t) {
      if (t.practice || t.conversation_index == null) { return; }
      (byConv[t.conversation_index] = byConv[t.conversation_index] || []).push(t);
    });
    Object.keys(byConv).sort().forEach(function (ci) {
      all.appendChild(el('h4', null, '대화 ' + ci));
      byConv[ci].slice().sort(function (a, b) { return a.turn_index - b.turn_index; })
        .forEach(function (t) {
          var line = el('div', 'rs-recall-turn');
          line.appendChild(el('p', 'rs-recall-meta',
            '턴 ' + t.turn_index + ' · ' + (DEPTH_KO[t.depth] || t.depth) +
            ' · 대기 ' + (observedSec(t) == null ? '—' : '약 ' + observedSec(t) + '초')));
          line.appendChild(el('p', null, '참가자: ' + truncText(t.user_input_text, 160)));
          line.appendChild(el('p', null, 'AI: ' + truncText(t.ai_response_text, 240)));
          all.appendChild(line);
        });
    });
    D.rsRecall.appendChild(all);
  }

  function loadRecall() {
    if (!RS.sessionId) {
      D.rsRecall.textContent = '세션 ID를 먼저 입력하세요.';
      return;
    }
    D.rsRecall.textContent = '불러오는 중…';
    get('/api/session/' + encodeURIComponent(RS.sessionId) + '/turns').then(function (r) {
      renderRecall(r.turns || []);
    }).catch(function (err) {
      D.rsRecall.textContent = '불러오지 못했습니다: ' + (err && err.message ? err.message : err);
    });
  }

  /* ==========================================================
     14c. TEST PANEL (?test=1 only, never in real sessions)
     ========================================================== */

  var TEST_MSGS = [
    'MSG-A', 'MSG-BB', 'MSG-CCC', 'MSG-DDDD', 'MSG-EEEEE',
    'MSG-FFFFFF', 'MSG-GGGGGGG', 'MSG-HHHHHHHH', 'MSG-IIIIIIIII'
  ];
  var testAuto = { running: false, mi: 0 };

  function testLog(msg) {
    var box = $('test-log');
    if (box) { box.textContent = msg; }
  }

  function installTestPanel() {
    var panel = document.createElement('div');
    panel.id = 'test-panel';
    panel.appendChild(el('strong', null, '테스트용 (실세션 금지)'));
    var log = el('div', null, '대기 중');
    log.id = 'test-log';
    panel.appendChild(log);
    function btn(label, fn) {
      var b = el('button', 'btn btn-small', label);
      b.type = 'button';
      b.addEventListener('click', fn);
      panel.appendChild(b);
      return b;
    }
    btn('설문채우기', function () {
      if (!window.__exp) { return testLog('세션 없음'); }
      window.__exp.fillSurvey().then(
        function () { testLog('설문 제출됨'); },
        function (e) { testLog('설문 실패: ' + (e && e.message ? e.message : e)); });
    });
    btn('다음', function () {
      if (!window.__exp) { return testLog('세션 없음'); }
      testLog(window.__exp.advance() ? '다음으로 이동' : '누를 버튼 없음');
    });
    btn('메시지 보내기', function () {
      if (!window.__exp) { return testLog('세션 없음'); }
      var st = window.__exp.state();
      var text = TEST_MSGS[testAuto.mi++ % TEST_MSGS.length] + ' (' + st.conversationIndex + '-' + (st.turnIndex + 1) + ')';
      window.__exp.send(text).then(
        function () { testLog('표시됨'); },
        function (e) { testLog('전송 실패: ' + (e && e.message ? e.message : e)); });
    });
    btn('로그 내려받기', function () { downloadLogs(); testLog('내려받기 실행'); });
    var dash = el('div', null, '');
    dash.id = 'test-dash';
    panel.appendChild(dash);
    document.body.appendChild(panel);
    updateTestDash();
    setInterval(updateTestDash, 1000);
  }

  function testSec(ms) {
    return (Math.round(ms / 100) / 10) + 's';
  }

  function updateTestDash() {
    var box = $('test-dash');
    if (!box) { return; }
    if (!State.session) { box.textContent = 'no session'; return; }
    var dp = State.session.delay_placement || {};
    var scale = State.session.delay_scale || 1;
    var d = dp.deep_ms || 0, m = dp.medium_ms || 0, sh = dp.shallow_ms || 0;
    var total = 3 * (d + m + sh);
    var lines = [];
    lines.push('DELAY: R1 deep' + testSec(d) + '/mid' + testSec(m) + '/sh' + testSec(sh)
      + ' | R2 deep' + testSec(sh) + '/mid' + testSec(m) + '/sh' + testSec(d)
      + ' | R3 shuffled | sum ' + testSec(total) + '/conv'
      + (scale !== 1 ? ' (x' + scale + ' scaled)' : ' (FULL)'));
    lines.push('ORDER: ' + (State.session.condition_order || []).join('>'));
    var pos = State.screen
      + (State.conversationIndex != null ? ' conv' + State.conversationIndex : '')
      + (State.condition ? '(' + State.condition + ')' : '')
      + ' turn ' + State.turnsSent + '/' + State.turnsTotal;
    lines.push('POS: ' + pos);
    if (State.lastDisplay) {
      var rec = null;
      for (var i = State.records.length - 1; i >= 0; i--) {
        if (State.records[i].turn_id === State.lastDisplay.turnId) { rec = State.records[i]; break; }
      }
      if (rec) {
        var obs = rec.display_ts - rec.user_input_submit_ts;
        var err = rec.display_ts - (rec.user_input_submit_ts + rec.target_delay_ms);
        lines.push('LAST: ' + rec.depth + ' target' + testSec(rec.target_delay_ms)
          + ' obs' + testSec(obs) + ' err' + (err >= 0 ? '+' : '') + err + 'ms'
          + (rec.queued_during_wait ? ' QUEUED' : ''));
      }
    }
    box.textContent = lines.join('\n');
  }

/* ==========================================================
     15. e2e 훅 (?e2e=1) — 프로덕션 흐름을 바꾸지 않는다.
        실제 클릭과 같은 경로를 그대로 밟는다.
     ========================================================== */

  function activeSection() { return sectionFor(State.screen); }

  function primaryButton() {
    var sec = activeSection();
    if (!sec) { return null; }
    var list = sec.querySelectorAll('[data-primary]');
    for (var i = 0; i < list.length; i++) {
      var b = list[i];
      if (!b.disabled && b.offsetParent !== null) { return b; }
    }
    return null;
  }

  function installE2E() {
    window.__exp = {
      state: function () {
        return {
          screen: State.screen,
          conversationIndex: State.conversationIndex,
          turnIndex: State.turnsSent,
          condition: State.condition,
            };
      },

      send: function (text) {
        return new Promise(function (resolve, reject) {
          if (State.screen !== 'chat' && State.screen !== 'practice') {
            reject(new Error('대화 화면이 아닙니다: ' + State.screen));
            return;
          }
          if (isOverlayOpen()) { reject(new Error('안전 오버레이가 열려 있습니다')); return; }
          if (State.awaiting) { reject(new Error('이전 턴이 아직 끝나지 않았습니다')); return; }
          if (State.turnsSent >= State.turnsTotal) { reject(new Error('이 대화의 턴이 모두 끝났습니다')); return; }
          if (!String(text).trim()) { reject(new Error('빈 메시지는 보낼 수 없습니다')); return; }

          displayWaiters.push({ resolve: resolve, reject: reject });
          // 실제 입력과 같은 경로: 값 설정 → input 이벤트(첫 타자 기록) → 전송 버튼 클릭
          D.chatInput.value = String(text);
          D.chatInput.dispatchEvent(new Event('input', { bubbles: true }));
          D.btnSend.click();
        });
      },

      lastDisplay: function () {
        return State.lastDisplay ? {
          turnId: State.lastDisplay.turnId,
          deadline: State.lastDisplay.deadline,
          displayTs: State.lastDisplay.displayTs,
          error: State.lastDisplay.error
        } : null;
      },

      advance: function () {
        // 명세서 §4 — 대화 종료는 2초 뒤 자동 전환이라 누를 버튼이 없다.
        // 곧 화면이 바뀌므로 성공으로 본다.
        if (State.endAutoTimer) { return true; }
        var b = primaryButton();
        if (!b) { return false; }
        b.click();
        return true;
      },

      fillSurvey: function () {
        if (State.screen === 'survey') {
          if (State.surveyPart === 1) {
            D.qTime.value = '8';
            D.qTime.dispatchEvent(new Event('input', { bubbles: true }));
            checkRadio('discomfort', 4);
            D.qWord.value = '차분';
            D.qWord.dispatchEvent(new Event('input', { bubbles: true }));
            D.btnSurveyNext.click();
          } else if (State.surveyPart === 2) {
            checkRadio('effort', 4);
            D.btnSurveyNext.click();
          } else if (State.surveyPart === 3) {
            for (var pi = 1; pi <= PETS_ITEMS.length; pi++) { checkRadio('pets_' + pi, 4); }
            D.btnSurveyNext.click();
          } else {
            GOD_ITEMS.forEach(function (it) { checkRadio(it.key, 3); });
            D.btnSurveyNext.click();
          }
          return State.lastSubmit;
        }
        if (State.screen === 'engagement') {
          var g = document.querySelector('input[name="rule_guess"][value="R3"]');
          if (g) { g.checked = true; g.dispatchEvent(new Event('change', { bubbles: true })); }
          ENGAGEMENT_ITEMS.forEach(function (it) { checkRadio(it.key, 4); });
          D.engagementForm.querySelector('[data-primary]').click();
          return State.lastSubmit;
        }
        return Promise.reject(new Error('설문 화면이 아닙니다: ' + State.screen));
      },
      done: function () {
        return State.doneReached === true && State.endPending === false;
      }
    };
  }

  function checkRadio(name, value) {
    var input = document.querySelector('input[name="' + name + '"][value="' + value + '"]');
    if (input) {
      input.checked = true;
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  /* ==========================================================
     16. 초기화
     ========================================================== */

  function init() {
    D.participantApp = $('participant-app');
    D.researcherApp = $('researcher-app');
    D.toast = $('toast');

    // 연구자 화면
    D.rsSessionId = $('rs-session-id');
    D.rsLoad = $('rs-load');
    D.rsMeta = $('rs-meta');
    D.rsPosition = $('rs-position');
    D.rsSafety = $('rs-safety');
    D.rsPlan = $('rs-plan');
    D.rsDisplay = $('rs-display');
    D.rsRaw = $('rs-raw');
    D.rsUpdated = $('rs-updated');
    D.rsRecall = $('rs-recall');
    D.rsRecallLoad = $('rs-recall-load');
    D.rsRecallPrint = $('rs-recall-print');

    if (OPT.researcher) { initResearcher(); return; }

    D.participantApp.hidden = false;

    // 동의
    D.consentCheck = $('consent-check');
    D.pid = $('pid');
    D.group = $('group');
    D.consentError = $('consent-error');
    D.btnStart = $('btn-start');

    // 카드
    D.topicCardTitle = $('topic-card-title');
    D.topicCardBody = $('topic-card-body');

    // 대화
    D.chatTopic = $('chat-topic');
    D.chatProgress = $('chat-progress');
    D.chatProgressText = $('chat-progress-text');
    D.chatCounter = $('chat-counter');
    D.chatLog = $('chat-log');
    D.chatNote = $('chat-note');
    D.composer = $('composer');
    D.chatInput = $('chat-input');
    D.btnSend = $('btn-send');
    D.endPanel = $('end-panel');
    D.endText = $('end-text');
    D.btnEndNext = $('btn-end-next');

    // 설문
    D.surveyForm = $('survey-form');
    D.qDiscomfort = $('q-discomfort');
    D.qPets = $('q-pets');
    D.surveyError = $('survey-error');

    D.engagementForm = $('engagement-form');
    D.qEngagement = $('q-engagement');
    D.qWord = $('q-word');
    D.qTime = $('q-time');
    D.qTimeUnknown = $('q-time-unknown');
    D.qEffort = $('q-effort');
    D.qGod = $('q-god');
    D.btnStop = $('btn-stop');
    D.surveyParts = Array.prototype.slice.call(document.querySelectorAll('.survey-part'));
    D.surveyPartLine = $('survey-part-line');
    D.btnSurveyNext = $('btn-survey-next');
    D.btnDownload = $('btn-download');
    if (D.btnDownload) { D.btnDownload.addEventListener('click', downloadLogs); }
    D.downloadPanel = $('download-panel');
    D.engagementBlock = $('engagement-block');
    D.engagementError = $('engagement-error');

    // 안전
    D.safetyOverlay = $('safety-overlay');
    D.safetyUnlock = $('safety-unlock');
    D.safetyContinue = $('safety-continue');
    D.safetyEnd = $('safety-end');

    // --- 이벤트 ---
    D.btnStart.addEventListener('click', startSession);
    D.pid.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); startSession(); }
    });

    D.participantApp.addEventListener('click', function (e) {
      var t = e.target.closest ? e.target.closest('[data-action="next"]') : null;
      if (t && !t.disabled) { nextStep(); }
    });

    // 첫 타자 입력 — 포커스가 아니라 실제 입력에서 기록한다.
    D.chatInput.addEventListener('input', markInputStart);
    D.chatInput.addEventListener('compositionstart', markInputStart);

    D.btnSend.addEventListener('click', sendCurrentInput);
    D.chatInput.addEventListener('keydown', function (e) {
      if (e.repeat) { return; }   // 키 반복 입력은 무시 (중복 전송 방지)
      if (e.key === 'Enter' && !e.shiftKey) {
        if (e.isComposing || e.keyCode === 229) { return; }   // 한글 조합 중에는 보내지 않는다
        e.preventDefault();
        sendCurrentInput();
      }
    });


    D.surveyForm.addEventListener('submit', function (e) {
      e.preventDefault();
      State.lastSubmit = submitSurvey();
    });
    D.engagementForm.addEventListener('submit', function (e) {
      e.preventDefault();
      State.lastSubmit = submitEngagement();
    });

    D.safetyUnlock.addEventListener('change', function () {
      D.safetyContinue.disabled = !D.safetyUnlock.checked;
      D.safetyEnd.disabled = !D.safetyUnlock.checked;
    });
    D.safetyContinue.addEventListener('click', closeSafetyOverlay);
    D.safetyEnd.addEventListener('click', endSessionEarly);

    buildSurveyForm();
    showScreen('consent');

    // B4: 화면 이탈 기록 — 서버 로그는 Phase 2에서 붙이고, 1차는 메모리+
    // 연구자 화면·내보내기에 남긴다. 대기 중 이탈(B4)과 주의 이탈 분석용.
    (function initAttentionLog() {
      var hideTs = null;
      function markHide(kind) {
        if (hideTs != null) { return; }
        hideTs = nowMs();
        State.attentionEvents.push({ kind: kind + '_hide', ts: hideTs,
          screen: State.screen, conversation_index: State.conversationIndex });
        publishLive();
      }
      function markShow(kind) {
        if (hideTs == null) { return; }
        var dur = nowMs() - hideTs;
        State.attentionEvents.push({ kind: kind + '_show', ts: nowMs(),
          duration_ms: dur, screen: State.screen,
          conversation_index: State.conversationIndex });
        hideTs = null;
        publishLive();
      }
      document.addEventListener('visibilitychange', function () {
        if (document.hidden) { markHide('visibility'); } else { markShow('visibility'); }
      });
      window.addEventListener('blur', function () { markHide('blur'); });
      window.addEventListener('focus', function () { markShow('blur'); });
    })();

    // B3: 대화 로그 스크롤(이전 메시지 열람) — 3초 쓰로틀
    (function initScrollLog() {
      var lastSent = 0;
      D.chatLog.addEventListener('scroll', function () {
        var t = nowMs();
        if (t - lastSent < 3000) { return; }
        lastSent = t;
        var ev = { kind: 'scroll', ts: t, screen: State.screen,
          conversation_index: State.conversationIndex };
        State.attentionEvents.push(ev);
        postEvent('B3_scroll', ev);
      });
    })();

    // B5: 새로고침·앱 전환 시도 — 이탈 시도 기록 후 차단 경고는 lockNavigation이 담당
    window.addEventListener('pagehide', function () {
      var ev = { session_id: State.session ? State.session.session_id : null,
        kind: 'B5_page_hide', ts: nowMs(), screen: State.screen };
      try {
        if (navigator.sendBeacon) {
          navigator.sendBeacon('/api/event', new Blob([JSON.stringify(ev)],
            { type: 'application/json; charset=utf-8' }));
        }
      } catch (e) { /* 무시 */ }
    });

    // B8: 블록 중단 요청 — 참가자가 직접 멈춤. 연구자 호출 후 done으로.
    if (D.btnStop) {
      D.btnStop.addEventListener('click', function () {
        if (!window.confirm('대화를 여기서 멈추시겠어요? 연구자를 불러주세요.')) { return; }
        var ev = { kind: 'B8_stop_request', ts: nowMs(), screen: State.screen,
          conversation_index: State.conversationIndex, turn_index: State.turnsSent };
        State.attentionEvents.push(ev);
        postEvent('B8_stop_request', ev);
        toast('연구자를 불러주세요.');
        endSessionEarly();
      });
    }

    if (OPT.e2e || OPT.test) { installE2E(); }
    if (OPT.test) { installTestPanel(); }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
