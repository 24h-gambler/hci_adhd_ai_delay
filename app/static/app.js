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
    indicator: pick(params.get('indicator'), ['none', 'dots', 'typing'], 'dots'),
    progress: params.get('progress') !== '0',
    e2e: params.get('e2e') === '1',
    researcher: params.get('researcher') === '1' || /\/researcher\/?$/.test(location.pathname)
  };

  var LIVE_KEY = 'exp.live';
  var LAST_SESSION_KEY = 'exp.lastSession';

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
    awaiting: false,          // /api/turn 왕복 또는 마감 대기 중
    pending: null,            // 표시 대기 중인 턴

    lastDisplay: null,        // {turnId, deadline, displayTs, error}
    displayLog: [],
    safetyEvents: [],
    surveyShownTs: null,
    lastSubmit: Promise.resolve(),
    endPending: false,
    doneReached: false
  };

  var displayWaiters = [];    // e2e send() 해소용 [{resolve, reject}]

  /* ==========================================================
     5. 문안 (참가자에게 보이는 모든 문자열)
        ※ "속도 / 빠름 / 느림 / 지연 / 기다림" 계열 단어는 절대 쓰지 않는다.
     ========================================================== */

  var TOPIC_CARD = {
    a: [
      '최근에 보신 영화, 드라마, 영상 같은\n콘텐츠에 대해 이야기해보세요.',
      '무엇이든 편하게 말씀하시면 됩니다.',
      '5번 주고받으면 이 대화는 마무리됩니다.'
    ],
    b: [
      '요즘 신경 쓰이거나 마음에 걸리는 일에 대해\n이야기해보세요.',
      '말씀하고 싶은 만큼만 하시면 되고,\n불편하시면 언제든 멈추실 수 있습니다.',
      '5번 주고받으면 이 대화는 마무리됩니다.'
    ]
  };

  var TOPIC_LINE = {
    a: '대화 주제 — 최근에 보신 영화, 드라마, 영상 같은 콘텐츠',
    b: '대화 주제 — 요즘 신경 쓰이거나 마음에 걸리는 일'
  };

  var PRACTICE_NOTE = '연습입니다. 아무 말이나 한 문장 입력하고 보내 보세요.';

  var PETS_ITEMS = [
    '〔자리표시자 ③-1〕 PETS 이해·신뢰 요인 1번 문항 — 원척도 문항으로 교체 예정',
    '〔자리표시자 ③-2〕 PETS 이해·신뢰 요인 2번 문항 — 원척도 문항으로 교체 예정',
    '〔자리표시자 ③-3〕 PETS 정서적 조응 요인 1번 문항 — 원척도 문항으로 교체 예정',
    '〔자리표시자 ③-4〕 PETS 정서적 조응 요인 2번 문항 — 원척도 문항으로 교체 예정'
  ];

  var GODSPEED_PAIRS = [
    ['인위적인', '자연스러운'],
    ['기계 같은', '사람 같은'],
    ['의식이 없는', '의식이 있는'],
    ['인공적인', '생명체 같은'],
    ['뻣뻣하게 움직이는', '우아하게 움직이는']   // 5번째 = 파일럿 판단 항목
  ];

  var ENGAGEMENT_ITEMS = {
    a: '실제로 최근에 본 콘텐츠에 대해 이야기했다',
    b: '실제로 요즘 신경 쓰이는 일에 대해 이야기했다',
    rest: [
      '이야기한 내용은 나에게 개인적으로 중요한 일이었다',
      '이야기하면서 감정이 움직였다',
      '평소에 남에게 잘 하지 않는 이야기를 했다',
      '대화에 집중하고 있었다'
    ]
  };

  /* ==========================================================
     6. 화면 전환
     ========================================================== */

  function sectionName(logical) { return logical === 'practice' ? 'chat' : logical; }

  function sectionFor(logical) {
    return document.querySelector('.screen[data-screen="' + sectionName(logical) + '"]');
  }

  function showScreen(logical) {
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
    var steps = [{ screen: 'briefing' }, { screen: 'practice' }];
    var convs = session.conversations || [];
    var blocks = [];
    convs.forEach(function (c) { if (blocks.indexOf(c.block) < 0) { blocks.push(c.block); } });
    blocks.sort(function (x, y) { return x - y; });

    blocks.forEach(function (b, bi) {
      var inBlock = convs.filter(function (c) { return c.block === b; })
                         .sort(function (x, y) { return x.index - y.index; });
      if (!inBlock.length) { return; }
      var ctx = inBlock[0].context;

      steps.push({ screen: 'card', block: b, context: ctx, conv: inBlock[0] });
      inBlock.forEach(function (c) {
        steps.push({ screen: 'chat', block: b, context: c.context, conv: c });
        steps.push({ screen: 'survey', block: b, context: c.context, conv: c });
      });
      steps.push({ screen: 'engagement', block: b, context: ctx, conv: inBlock[inBlock.length - 1] });
      if (bi < blocks.length - 1) { steps.push({ screen: 'break', block: b }); }
    });

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

    post('/api/session/start', { participant_id: pid, group: group }).then(function (res) {
      State.session = res;
      State.turnsTotal = res.turns_per_conversation || 5;
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
    State.block = step.block != null ? step.block : null;
    State.conversationIndex = step.conv ? step.conv.index : null;
    State.condition = step.conv ? step.conv.condition : null;
    State.context = step.context != null ? step.context : null;

    switch (step.screen) {
      case 'briefing':
        showScreen('briefing');
        break;

      case 'practice':
        State.conversationIndex = 0;
        State.condition = 'practice';
        State.context = null;
        startConversation({ index: 0, block: null, context: null, condition: 'practice' }, true);
        break;

      case 'card':
        renderCard(step.context);
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
        finishSession();
        break;

      default:
        console.error('알 수 없는 화면: ' + step.screen);
    }
  }

  function renderCard(ctx) {
    var body = D.topicCardBody;
    clear(body);
    (TOPIC_CARD[ctx] || TOPIC_CARD.a).forEach(function (para) {
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
    State.context = isPractice ? null : conv.context;
    State.turnsTotal = isPractice ? 1 : (State.session && State.session.turns_per_conversation) || 5;
    State.turnsSent = 0;
    State.prevTurnId = null;          // 대화가 바뀌면 직전 턴 연결을 끊는다 (마지막 턴은 next_input null)
    State.inputStartTs = null;
    State.pending = null;
    State.awaiting = false;

    clear(D.chatLog);
    D.chatTopic.textContent = isPractice ? '연습' : (TOPIC_LINE[conv.context] || '');
    D.chatNote.textContent = isPractice ? PRACTICE_NOTE : '';
    D.chatNote.hidden = !isPractice;
    D.endPanel.hidden = true;
    D.composer.hidden = false;
    D.chatInput.value = '';
    setComposerEnabled(true);

    if (isPractice) {
      D.chatProgress.hidden = true;
    } else {
      D.chatProgress.hidden = false;
      D.chatProgressText.textContent = '이번 대화는 ' + State.turnsTotal + '번 주고받으면 마무리됩니다.';
      D.chatCounter.hidden = !OPT.progress;
      renderCounter();
    }

    showScreen(isPractice ? 'practice' : 'chat');
    if (!OPT.e2e) { try { D.chatInput.focus(); } catch (e) { /* 무시 */ } }
  }

  function renderCounter() {
    D.chatCounter.textContent = '(' + State.turnsSent + ' / ' + State.turnsTotal + ')';
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

  /* --- 대기 표시 -------------------------------------------------
     세 조건에서 완전히 동일하다. 경과 시간·진행 바·남은 분량을 절대
     드러내지 않는다. ?indicator=none|dots|typing 로만 바뀐다. */

  function showIndicator() {
    if (OPT.indicator === 'none') { return; }
    hideIndicator();
    var wrap = el('div', 'msg msg-ai msg-wait');
    wrap.id = 'wait-bubble';
    wrap.setAttribute('aria-hidden', 'true');
    var bubble = el('div', 'bubble');
    if (OPT.indicator === 'dots') {
      bubble.textContent = '…';
    } else {
      var dots = el('span', 'wait-dots is-typing');
      dots.appendChild(el('i'));
      dots.appendChild(el('i'));
      dots.appendChild(el('i'));
      bubble.appendChild(dots);
    }
    wrap.appendChild(bubble);
    D.chatLog.appendChild(wrap);
    scrollLogToEnd();
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
    if (State.awaiting) { return; }
    var ts = nowMs();
    State.inputStartTs = ts;

    // 다음 턴의 첫 타자 = 직전 턴의 next_input_start_ts (계약 §2)
    if (State.prevTurnId) {
      var tid = State.prevTurnId;
      State.prevTurnId = null;
      post('/api/turn/next-input', { turn_id: tid, next_input_start_ts: ts })
        .catch(function (err) { console.error(err); });
    }
  }

  /* --- 전송 ------------------------------------------------------ */

  function sendCurrentInput() {
    if (State.awaiting) { return; }
    if (State.screen !== 'chat' && State.screen !== 'practice') { return; }
    if (State.turnsSent >= State.turnsTotal) { return; }
    if (isOverlayOpen()) { return; }

    var text = D.chatInput.value;
    if (!text || !text.trim()) { return; }

    var submit = nowMs();                       // t0
    if (State.inputStartTs == null) { State.inputStartTs = submit; }  // 붙여넣기 등 입력 이벤트가 없던 경우

    var turnIndex = State.turnsSent + 1;
    var startTs = State.inputStartTs;

    appendMessage('user', text);
    scrollLogToEnd();
    D.chatInput.value = '';
    setComposerEnabled(false);
    State.awaiting = true;
    State.inputStartTs = null;

    State.turnsSent = turnIndex;                // 진행 표시는 전송 직후 증가 (계약 §7)
    if (State.screen === 'chat') { renderCounter(); }
    publishLive();

    showIndicator();

    post('/api/turn', {
      session_id: State.session.session_id,
      conversation_index: State.conversationIndex,
      turn_index: turnIndex,
      text: text,
      user_input_start_ts: startTs,
      user_input_submit_ts: submit
    }).then(function (res) {
      scheduleDisplay(res, submit);
    }).catch(function (err) {
      console.error(err);
      // 턴이 성립하지 않았으므로 카운터를 되돌리고 재전송할 수 있게 둔다.
      hideIndicator();
      State.awaiting = false;
      State.turnsSent = turnIndex - 1;
      if (State.screen === 'chat') { renderCounter(); }
      D.chatInput.value = text;
      State.inputStartTs = startTs;
      setComposerEnabled(true);
      toast('화면에 문제가 있습니다. 연구자를 불러주세요.');
      publishLive();
      rejectDisplayWaiters(err);
    });
  }

  /* --- 표시 예약 (계약 §7 그대로) -------------------------------- */

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
        D.endText.textContent = '이 대화는 마무리되었습니다.';
        D.btnEndNext.textContent = '설문으로';
      }
    } else {
      setComposerEnabled(true);
      if (!OPT.e2e) { try { D.chatInput.focus(); } catch (e) { /* 무시 */ } }
    }

    if (p.safety) { openSafetyOverlay(p); }
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
      ts: p ? nowMs() : nowMs(),
      conversation_index: State.conversationIndex,
      turn_index: State.turnsSent,
      turn_id: p ? p.turnId : null,
      condition: State.condition,
      context: State.context
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

    clear(D.qPets);
    PETS_ITEMS.forEach(function (t, i) {
      scaleRow(D.qPets, 'pets_' + (i + 1), {
        text: t, min: 1, max: 7,
        left: '전혀 그렇지 않다', right: '매우 그렇다'
      });
    });

    clear(D.qGodspeed);
    GODSPEED_PAIRS.slice(0, 4).forEach(function (pair, i) {
      scaleRow(D.qGodspeed, 'godspeed_' + (i + 1), {
        text: '', min: 1, max: 5, left: pair[0], right: pair[1]
      });
    });

    clear(D.qGodspeed5);
    scaleRow(D.qGodspeed5, 'godspeed_5', {
      text: '', min: 1, max: 5,
      left: GODSPEED_PAIRS[4][0], right: GODSPEED_PAIRS[4][1]
    });
  }

  function resetSurveyForm() {
    buildSurveyForm();
    D.qTime.value = '';
    D.qTime.disabled = false;
    D.qTimeUnknown.checked = false;
    D.surveyError.hidden = true;
  }

  function openSurvey(step) {
    resetSurveyForm();
    State.surveyShownTs = nowMs();
    showScreen('survey');
  }

  function submitSurvey() {
    var unknown = D.qTimeUnknown.checked;
    var raw = (D.qTime.value || '').trim();
    var missing = [];

    if (!unknown && raw === '') { missing.push('①'); }
    if (!unknown && raw !== '' && (!isFinite(Number(raw)) || Number(raw) < 0)) { missing.push('①'); }
    if (radioValue('discomfort') == null) { missing.push('②'); }
    for (var i = 1; i <= 4; i++) {
      if (radioValue('pets_' + i) == null) { missing.push('③-' + i); }
    }
    for (var j = 1; j <= 4; j++) {
      if (radioValue('godspeed_' + j) == null) { missing.push('④-' + j); }
    }

    if (missing.length) {
      D.surveyError.textContent = '아직 답하지 않은 항목이 있습니다: ' + missing.join(', ');
      D.surveyError.hidden = false;
      return Promise.resolve(false);
    }
    D.surveyError.hidden = true;

    var responses = {
      time_estimate_sec: unknown ? null : Number(raw),
      time_estimate_unknown: unknown,
      discomfort: radioValue('discomfort'),
      pets_1: radioValue('pets_1'),
      pets_2: radioValue('pets_2'),
      pets_3: radioValue('pets_3'),
      pets_4: radioValue('pets_4'),
      godspeed_1: radioValue('godspeed_1'),
      godspeed_2: radioValue('godspeed_2'),
      godspeed_3: radioValue('godspeed_3'),
      godspeed_4: radioValue('godspeed_4'),
      godspeed_5: radioValue('godspeed_5'),
      block: State.block,
      condition: State.condition,
      context: State.context
    };

    return sendSurvey('per_condition', responses);
  }

  function buildEngagementForm(ctx) {
    clear(D.qEngagement);
    var first = ENGAGEMENT_ITEMS[ctx] || ENGAGEMENT_ITEMS.a;
    var items = [first].concat(ENGAGEMENT_ITEMS.rest);
    items.forEach(function (t, i) {
      scaleRow(D.qEngagement, 'engagement_' + (i + 1), {
        text: (i + 1) + '. ' + t, min: 1, max: 7,
        left: '전혀 아니다', right: '매우 그렇다'
      });
    });
    D.qFree.value = '';
    D.engagementError.hidden = true;
  }

  function openEngagement(step) {
    buildEngagementForm(step.context);
    State.surveyShownTs = nowMs();
    showScreen('engagement');
  }

  function submitEngagement() {
    var missing = [];
    for (var i = 1; i <= 5; i++) {
      if (radioValue('engagement_' + i) == null) { missing.push(String(i)); }
    }
    if (missing.length) {
      D.engagementError.textContent = '아직 답하지 않은 항목이 있습니다: ' + missing.join(', ') + '번';
      D.engagementError.hidden = false;
      return Promise.resolve(false);
    }
    D.engagementError.hidden = true;

    var responses = {
      engagement_1: radioValue('engagement_1'),
      engagement_2: radioValue('engagement_2'),
      engagement_3: radioValue('engagement_3'),
      engagement_4: radioValue('engagement_4'),
      engagement_5: radioValue('engagement_5'),
      engagement_freetext: (D.qFree.value || '').trim(),
      block: State.block,
      context: State.context
    };

    return sendSurvey('engagement', responses);
  }

  function sendSurvey(kind, responses) {
    var payload = {
      session_id: State.session.session_id,
      kind: kind,
      conversation_index: State.conversationIndex,
      shown_ts: State.surveyShownTs,
      submitted_ts: nowMs(),
      responses: responses
    };
    var p = post('/api/survey', payload).catch(function (err) {
      console.error(err);
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
        block: State.block,
        conversation_index: State.conversationIndex,
        condition: State.condition,
        context: State.context,
        turns_sent: State.turnsSent,
        turns_total: State.turnsTotal,
        safety_events: State.safetyEvents,
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
  function firstOf(obj, keys) {
    if (!obj) { return null; }
    for (var i = 0; i < keys.length; i++) {
      if (obj[keys[i]] != null) { return obj[keys[i]]; }
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
    ['0', '—', '—', 'practice (연습)', nowConv === 0 ? '진행 중' : (nowConv != null && nowConv > 0 ? '완료' : '대기')]
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
      [String(c.index), String(c.block), String(c.context), String(c.condition), status]
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
          context: State.context
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
        var b = primaryButton();
        if (!b) { return false; }
        b.click();
        return true;
      },

      fillSurvey: function () {
        if (State.screen === 'survey') {
          D.qTime.value = '8';
          D.qTime.dispatchEvent(new Event('input', { bubbles: true }));
          checkRadio('discomfort', 4);
          for (var i = 1; i <= 4; i++) { checkRadio('pets_' + i, 4); }
          for (var j = 1; j <= 5; j++) { checkRadio('godspeed_' + j, 3); }
          D.surveyForm.querySelector('[data-primary]').click();
          return State.lastSubmit;
        }
        if (State.screen === 'engagement') {
          for (var k = 1; k <= 5; k++) { checkRadio('engagement_' + k, 4); }
          D.qFree.value = '자동 점검용 응답입니다.';
          D.qFree.dispatchEvent(new Event('input', { bubbles: true }));
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

    if (OPT.researcher) { initResearcher(); return; }

    D.participantApp.hidden = false;

    // 동의
    D.consentCheck = $('consent-check');
    D.pid = $('pid');
    D.group = $('group');
    D.consentError = $('consent-error');
    D.btnStart = $('btn-start');

    // 카드
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
    D.qTime = $('q-time');
    D.qTimeUnknown = $('q-time-unknown');
    D.qDiscomfort = $('q-discomfort');
    D.qPets = $('q-pets');
    D.qGodspeed = $('q-godspeed');
    D.qGodspeed5 = $('q-godspeed5');
    D.surveyError = $('survey-error');

    D.engagementForm = $('engagement-form');
    D.qEngagement = $('q-engagement');
    D.qFree = $('q-free');
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
      if (e.key === 'Enter' && !e.shiftKey) {
        if (e.isComposing || e.keyCode === 229) { return; }   // 한글 조합 중에는 보내지 않는다
        e.preventDefault();
        sendCurrentInput();
      }
    });

    D.qTimeUnknown.addEventListener('change', function () {
      D.qTime.disabled = D.qTimeUnknown.checked;
      if (D.qTimeUnknown.checked) { D.qTime.value = ''; }
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

    if (OPT.e2e) { installE2E(); }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
