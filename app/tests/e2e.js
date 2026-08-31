#!/usr/bin/env node
'use strict';
/**
 * app/tests/e2e.js — 브라우저로 세션 하나를 끝까지 진행한다.
 *
 * 기준 문서
 *   app/CONTRACT.md §7(화면 순서·타이밍 규칙·e2e 훅), §8(검증 파이프라인)
 *   analysis/10-manipulation-check-plan.md §1(독립성), §2(조작 충실도)
 *
 * 사용:
 *   node app/tests/e2e.js --base-url http://127.0.0.1:8000 --participant P03
 *
 * 하는 일
 *   1. ?e2e=1 로 열고 window.__exp 만으로 동의→연습→블록1→휴식→블록2→종료를 진행한다.
 *      (동의 화면의 참가자 ID·집단은 연구자 입력란이라 __exp에 없다. 그 화면에서만
 *       실제 클릭과 같은 경로로 DOM을 채운다. 서버가 ?participant= 로 자동 시작하면
 *       그 경로를 그대로 쓴다.)
 *   2. 턴마다 길이가 크게 다른 한국어 메시지를 보낸다 (기본 10~400자).
 *      → manipulation_check.py의 "입력 길이 ↔ 부과 지연 독립성" 검사에 실제 분산을 준다.
 *   3. 표시될 때마다 lastDisplay()를 읽어 displayTs − deadline 을 확인한다.
 *      ★ 좌우 대칭이 아니다 — 늦는 쪽만 250ms 봐주고, 이른 쪽은 반올림 몫(2ms)
 *        까지만 허용한다. 조기 표시는 지터가 아니라 조작 실패다 (아래 함수 설명).
 *      화면에 조건 단서(금지어·진행 바)가 드러나지 않는지도 함께 본다.
 *   4. 끝나면 서버가 쓴 JSONL을 열어 목표 지연·입력 길이·표시 시각을 다시 확인하고,
 *      표시 오차가 LLM 생성 시간을 따라가지 않는지(기울기 ≈ 0) 본 뒤
 *      로그 경로를 출력한다.
 *
 * ★ 입력 길이 배정 — 왜 무작위로 뽑지 않는가
 *   목표 지연 D는 sha256(session_id|대화|턴)으로 결정되므로 이 스크립트도 D를
 *   미리 계산할 수 있다 (CONTRACT §0 P1: "사후에 D를 재현할 수 있다").
 *   그 값을 이용해 **조건 안에서 입력 길이와 D의 상관이 0이 되도록** 길이를 배정한다.
 *   세션 2개(턴 60개)면 우연 상관의 표준오차가 .13이라, 길이를 무작위로 뽑으면
 *   구현이 완벽해도 보고되는 r이 흔히 .1을 넘는다(검사기는 신뢰구간이 0을
 *   배제할 때만 실패시키므로 통과는 하지만, 파이프라인이 매번 다른 수치를 내며
 *   "구현을 의심하라"는 안내를 띄운다). 배정을 균형 잡으면 그 우연 성분만
 *   사라지고, **지연이 입력 길이를 따라가는 구현은 그대로 걸린다**
 *   (부과 지연에 길이 성분이 실리면 r이 크게 나온다). 검출력은 그대로 두고
 *   잡음만 없애는 방식이다.
 *   예측이 서버 값과 어긋나면 경고를 내고 그대로 진행한다 (배정만 무의미해진다).
 *
 * ★ 상한 길이 — mock 생성 시간과 즉시 조건
 *   mock(--mock-latency-mode length)의 생성 시간은 280 + 3×글자수 + 지터(≤199)ms다
 *   (app/llm.py). 이것이 즉시 조건의 목표 지연 하한(1000ms)을 넘으면
 *   manipulation_check.py가 "즉시 조건 실행 불가"로 (정당하게) 실패한다.
 *   그래서 대부분의 메시지는 그 예산 안에서 뽑고, 예산을 넘는 아주 긴 메시지는
 *   세션당 1개만(--long-inputs) 긺 조건의 마지막 턴이 아닌 자리에 넣는다.
 *   p95 검사가 세션당 1개까지는 허용한다.
 */

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

/* ══════════════════════════════════════════════════════════════════
   0. 옵션
   ══════════════════════════════════════════════════════════════════ */

const DEFAULTS = {
  'base-url': 'http://127.0.0.1:8000',
  participant: 'P01',
  group: 'adhd',
  'log-dir': '',                 // 비우면 /api/session/{id}/end 가 알려주는 경로를 쓴다
  'min-chars': 10,
  'max-chars': 400,
  'long-inputs': 1,              // 예산(즉시 조건 하한)을 넘는 메시지 수 — 위 설명 참조
  tolerance: 250,                // displayTs − deadline 의 **늦은 쪽** 허용 (계획서 §2)
  'early-tolerance': 2,          // 이른 쪽 허용 — 반올림 몫만. ★ 아래 설명 참조
  timeout: 240000,               // 세션 전체 제한
  'step-timeout': 60000,         // 한 동작(턴 표시·화면 전환) 제한
  seed: 20260401,
  headed: false,
  json: '',
};

function parseArgs(argv) {
  const out = Object.assign({}, DEFAULTS);
  for (let i = 2; i < argv.length; i++) {
    const raw = argv[i];
    if (!raw.startsWith('--')) { fail(`알 수 없는 인자: ${raw}`); }
    const key = raw.slice(2);
    if (key === 'headed') { out.headed = true; continue; }
    if (key === 'help') { console.log(usage()); process.exit(0); }
    if (!(key in DEFAULTS)) { fail(`알 수 없는 옵션: --${key}\n${usage()}`); }
    const value = argv[++i];
    if (value === undefined) { fail(`--${key} 에 값이 없습니다`); }
    out[key] = typeof DEFAULTS[key] === 'number' ? Number(value) : value;
  }
  out['base-url'] = String(out['base-url']).replace(/\/+$/, '');
  if (!/^P\d{1,3}$/.test(out.participant)) {
    fail(`--participant 는 P01 형식이어야 합니다: ${out.participant}`);
  }
  return out;
}

function usage() {
  return [
    '사용: node app/tests/e2e.js --base-url http://127.0.0.1:8000 --participant P03',
    '',
    '  --base-url      서버 주소 (기본 http://127.0.0.1:8000)',
    '  --participant   참가자 ID (P01 형식)',
    '  --group         adhd | comparison',
    '  --log-dir       로그 디렉터리 (기본: 서버가 알려주는 경로)',
    '  --min-chars     가장 짧은 입력 (기본 10)',
    '  --max-chars     가장 긴 입력 (기본 400)',
    '  --long-inputs   예산을 넘는 긴 입력의 개수 (기본 1)',
    '  --tolerance     표시가 늦은 쪽 허용 ms (기본 250)',
    '  --early-tolerance  표시가 이른 쪽 허용 ms (기본 2 — 반올림 몫만)',
    '  --timeout       세션 전체 제한 ms (기본 240000)',
    '  --step-timeout  한 동작 제한 ms (기본 60000)',
    '  --seed          길이 배정 난수 시드 (기본 20260401)',
    '  --headed        브라우저 창을 띄운다',
    '  --json <경로>   요약을 JSON으로 저장',
  ].join('\n');
}

function fail(message) {
  console.error(`\n✗ ${message}\n`);
  process.exit(1);
}

/* ══════════════════════════════════════════════════════════════════
   1. playwright 찾기 (전역 설치 — never playwright install)
   ══════════════════════════════════════════════════════════════════ */

function loadPlaywright() {
  const tried = [];
  const candidates = ['playwright'];
  for (const dir of String(process.env.NODE_PATH || '').split(path.delimiter)) {
    if (dir) { candidates.push(path.join(dir, 'playwright')); }
  }
  candidates.push(path.join(path.dirname(process.execPath), '..', 'lib', 'node_modules', 'playwright'));
  try {
    candidates.push(path.join(execSync('npm root -g', { encoding: 'utf8' }).trim(), 'playwright'));
  } catch (e) { /* npm이 없어도 위 후보로 충분할 수 있다 */ }

  for (const c of candidates) {
    try { return require(c); } catch (e) { tried.push(`${c}: ${e.code || e.message}`); }
  }
  fail('playwright 모듈을 찾지 못했습니다. 전역 설치를 확인하세요 '
       + '(NODE_PATH="$(npm root -g)").\n  ' + tried.join('\n  '));
}

/* ══════════════════════════════════════════════════════════════════
   2. 작은 유틸
   ══════════════════════════════════════════════════════════════════ */

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class Deadline {
  constructor(ms) { this.until = Date.now() + ms; }
  left() { return this.until - Date.now(); }
  check(what) {
    if (this.left() <= 0) { throw new Error(`전체 제한 시간을 넘었습니다 (${what})`); }
  }
}

async function waitFor(label, fn, timeoutMs, intervalMs = 25) {
  const until = Date.now() + timeoutMs;
  let last = null;
  for (;;) {
    const value = await fn();
    if (value) { return value; }
    last = value;
    if (Date.now() > until) {
      throw new Error(`시간 초과 (${Math.round(timeoutMs / 1000)}초): ${label}`
                      + (last === null ? '' : ` — 마지막 값 ${JSON.stringify(last)}`));
    }
    await sleep(intervalMs);
  }
}

function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function mean(xs) { return xs.reduce((s, x) => s + x, 0) / xs.length; }

function pearson(xs, ys) {
  if (xs.length < 3) { return 0; }
  const mx = mean(xs), my = mean(ys);
  let num = 0, dx = 0, dy = 0;
  for (let i = 0; i < xs.length; i++) {
    num += (xs[i] - mx) * (ys[i] - my);
    dx += (xs[i] - mx) ** 2;
    dy += (ys[i] - my) ** 2;
  }
  if (dx === 0 || dy === 0) { return 0; }
  return num / Math.sqrt(dx * dy);
}

/**
 * 표시 오차 판정 — ★ 좌우 대칭이 아니다.
 *
 * |오차| ≤ 250ms 로 보면 "마감보다 250ms 이른 표시"가 통과한다. 그런데
 * 축소 실행(verify.sh 기본 --delay-scale 0.1)에서 즉시 조건의 목표 지연은
 * 100~200ms다. 즉 **응답을 받자마자 띄우는**(부과 지연 ≈ 0) 구현이 허용
 * 안에 들어와 버리고, analysis/manipulation_check.py도 |display_error| 로
 * 보므로 파이프라인 전체가 조기 표시를 놓친다.
 *
 * 조기 표시는 스케줄러 지터가 아니라 조작 실패다 — 지연을 실제로 부과하지
 * 않았다는 뜻이다. app/static/app.js의 show()는 nowMs() >= deadline 에서만
 * 발화하므로 정상 구현의 오차는 항상 0 이상이다. 그래서 이른 쪽은 반올림
 * 몫(기본 2ms)만 허용하고, 늦는 쪽만 tolerance를 준다.
 * (bypass_delay 턴 = 안전 경로는 애초에 실패로 따로 잡는다.)
 */
function displayErrorProblem(errorMs, opts) {
  const early = Number(opts['early-tolerance']);
  if (errorMs < -early) {
    return `마감보다 ${-errorMs}ms 이르다 (조기 표시 = 조작 실패, 허용 ${early}ms)`;
  }
  if (errorMs > opts.tolerance) {
    return `마감보다 ${errorMs}ms 늦다 (허용 ${opts.tolerance}ms)`;
  }
  return null;
}

async function postJson(url, body) {
  let res;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify(body),
    });
  } catch (e) {
    throw new Error(`서버에 연결하지 못했습니다: POST ${url} — ${(e && e.message) || e}`
      + ' (서버가 떠 있는지, --base-url이 맞는지 확인하세요)');
  }
  const text = await res.text();
  if (!res.ok) { throw new Error(`POST ${url} → ${res.status} ${text.slice(0, 200)}`); }
  return JSON.parse(text);
}

/* ══════════════════════════════════════════════════════════════════
   3. 목표 지연 D 재현 (app/schedule.py와 같은 규칙 — CONTRACT §0 P1)
   ══════════════════════════════════════════════════════════════════ */

function drawDelayMs(sessionId, conversationIndex, turnIndex, condition, ranges) {
  if (Number(conversationIndex) === 0) { return Math.trunc(ranges.practice.fixed_ms); }
  const seed = `${sessionId}|${Number(conversationIndex)}|${Number(turnIndex)}`;
  const digest = crypto.createHash('sha256').update(seed, 'utf8').digest();
  const unit = Number(digest.readBigUInt64BE(0)) / 2 ** 64;
  const lo = Math.trunc(ranges[condition].min_ms);
  const hi = Math.trunc(ranges[condition].max_ms);
  return lo + Math.floor(unit * (hi - lo));
}

/* ══════════════════════════════════════════════════════════════════
   4. 한국어 메시지 만들기 (길이를 정확히 맞춘다)
      ★ app/safety.py에 걸릴 표현을 쓰지 않는다. 걸리면 오버레이가 떠서
        세션이 멈추고, 그것은 앱의 결함이 아니라 검사 데이터의 결함이다.
   ══════════════════════════════════════════════════════════════════ */

const CLAUSES = {
  a: [
    '어제 저녁에 예전부터 보고 싶었던 다큐멘터리를 한 편 봤습니다',
    '초반에는 설명이 길어서 조금 지루했는데 중반부터는 계속 보게 되더라고요',
    '주인공이 바닷가 마을로 돌아가는 장면이 특히 기억에 남았어요',
    '화면 색감이 전체적으로 차분해서 눈이 편했습니다',
    '같이 본 친구는 결말이 아쉽다고 했는데 저는 그 정도면 괜찮다고 생각했어요',
    '드라마는 보통 한 번에 두세 편씩 몰아서 보는 편입니다',
    '음악이 나오는 부분에서 장면이 바뀌는 방식이 인상적이었습니다',
    '원작 소설을 먼저 읽어서 그런지 인물의 선택이 더 이해가 갔어요',
    '중간에 나오는 시장 장면은 실제 촬영지가 궁금해서 찾아보기도 했습니다',
    '요즘은 짧은 영상보다 두 시간짜리 작품이 오히려 편하게 느껴집니다',
    '자막 없이 보다가 놓친 대사가 있어서 뒷부분을 다시 돌려봤어요',
    '주말에 시간이 나면 감독의 다른 작품도 찾아볼 생각입니다',
  ],
  b: [
    '요즘 일정이 겹쳐서 하루가 어떻게 지나가는지 모르겠습니다',
    '해야 할 일을 적어두기는 하는데 순서를 정하는 데서 시간이 오래 걸려요',
    '아침에는 계획을 세우다가 정작 시작을 늦게 하는 날이 많습니다',
    '팀에서 맡은 부분이 늘어나면서 마감 날짜가 자꾸 앞당겨지는 느낌이에요',
    '저녁에는 피곤한데도 화면을 계속 보게 되어서 잠드는 시간이 밀립니다',
    '주변에 이야기를 꺼내면 다들 비슷하다고 해서 그냥 넘어가게 됩니다',
    '지난달에는 일정을 나눠서 적어봤는데 이번 달에는 그것도 잘 안 되네요',
    '집중이 되는 시간대가 하루에 한두 시간뿐이라 그때를 놓치면 아쉽습니다',
    '주말에 쉬어도 월요일이 되면 다시 비슷한 상태가 되는 것 같아요',
    '무엇부터 손을 대야 할지 정하는 일이 제일 어렵게 느껴집니다',
    '작은 일은 금방 끝나는데 큰 일은 시작 자체를 미루게 됩니다',
    '요즘은 알림을 꺼두고 한 가지만 붙잡고 있으려고 해보는 중입니다',
  ],
};

function makeMessage(context, rng, chars) {
  const pool = CLAUSES[context] || CLAUSES.a;
  let text = '';
  let guard = 0;
  while (text.length < chars && guard++ < 200) {
    const clause = pool[Math.floor(rng() * pool.length)];
    text += (text ? ' ' : '') + clause + '.';
  }
  text = text.slice(0, chars);
  while (text.length && /[\s.]$/.test(text[text.length - 1]) && text.trim().length < 2) {
    text = text.slice(0, -1) + '요';
  }
  if (/\s$/.test(text)) { text = text.slice(0, -1) + '요'; }
  return text;
}

/* ══════════════════════════════════════════════════════════════════
   5. 입력 길이 배정 — 조건 안에서 D와 상관이 0이 되도록
   ══════════════════════════════════════════════════════════════════ */

// app/llm.py MockProvider._latency_ms('length'): 280 + 3×글자수 + 지터(0~199)
const MOCK_BASE_MS = 280;
const MOCK_PER_CHAR_MS = 3;
const MOCK_JITTER_MS = 200;

function safeCharBudget(ranges, opts) {
  // 가장 짧은 목표 지연 하한(즉시)을 mock 생성 시간이 넘지 않는 최대 글자 수.
  // ranges는 이미 배율이 적용된 값이므로 배율을 되돌려 비교한다.
  const scale = opts.scale || 1;
  const floorMs = Math.min(...['immediate', 'medium', 'long']
    .filter((c) => ranges[c]).map((c) => ranges[c].min_ms)) / scale;
  const margin = 60;
  const budget = Math.floor((floorMs - MOCK_BASE_MS - MOCK_JITTER_MS - margin) / MOCK_PER_CHAR_MS);
  return Math.max(20, Math.min(opts['max-chars'], budget));
}

function spread(lo, hi, n) {
  if (n <= 1) { return [Math.round((lo + hi) / 2)]; }
  const out = [];
  for (let i = 0; i < n; i++) { out.push(Math.round(lo + (hi - lo) * (i / (n - 1)))); }
  return out;
}

/**
 * slots: [{conversationIndex, turnIndex, condition, context, target}]
 * 반환: 같은 배열에 chars를 채워 넣고 진단값을 돌려준다.
 *
 * 조건별 길이 다중집합은 참가자와 무관하게 고정한다. 그래야 참가자를 합쳐도
 * 조건 평균이 흔들리지 않아 부분상관의 상쇄가 그대로 유지된다.
 */
function assignLengths(slots, ranges, opts, rng) {
  const conditions = [...new Set(slots.map((s) => s.condition))].sort();
  const budget = safeCharBudget(ranges, opts);
  const lastTurn = Math.max(...slots.map((s) => s.turnIndex));
  const pools = {};
  for (const c of conditions) {
    const n = slots.filter((s) => s.condition === c).length;
    pools[c] = spread(opts['min-chars'], budget, n);
  }
  // 예산을 넘는 긴 입력: 긺 조건(없으면 가장 지연이 큰 조건)의 끝자리에 넣는다.
  const longCondition = conditions.includes('long') ? 'long' : conditions[conditions.length - 1];
  const overBudget = [];
  if (opts['max-chars'] > budget) {
    const howMany = Math.min(Math.max(0, Math.trunc(opts['long-inputs'])),
                             pools[longCondition].length - 1);
    for (let i = 0; i < howMany; i++) {
      const value = Math.round(opts['max-chars'] - i * 20);
      pools[longCondition][pools[longCondition].length - 1 - i] = value;
      overBudget.push(value);
    }
  }

  const groups = {};
  for (const c of conditions) {
    groups[c] = slots.filter((s) => s.condition === c);
  }

  const allowed = (slot, chars) => !(chars > budget && slot.turnIndex === lastTurn);

  // 초기 배정: 목표 지연 순서와 길이 순서를 지그재그로 어긋나게 둔다.
  const state = {};
  for (const c of conditions) {
    const order = groups[c].map((s, i) => i)
      .sort((i, j) => groups[c][i].target - groups[c][j].target);
    const lens = pools[c].slice();
    const zig = [];
    for (let i = 0; i < lens.length; i += 2) { zig.push(lens[i]); }
    for (let i = 1; i < lens.length; i += 2) { zig.push(lens[i]); }
    state[c] = new Array(groups[c].length);
    order.forEach((slotIdx, k) => { state[c][slotIdx] = zig[k]; });
  }

  // manipulation_check.py는 (조건) 중심화와 (조건 × 턴 위치) 중심화 두 가지를
  // 본다. 둘 다 0에 가깝게 만든다.
  const score = () => {
    let perCond = 0;
    const rx = [], ry = [];
    const cells = new Map();
    for (const c of conditions) {
      const xs = state[c], ys = groups[c].map((g) => g.target);
      perCond += Math.abs(pearson(xs, ys));
      const mx = mean(xs), my = mean(ys);
      xs.forEach((x, i) => {
        rx.push(x - mx);
        ry.push(ys[i] - my);
        const key = `${c}|${groups[c][i].turnIndex}`;
        if (!cells.has(key)) { cells.set(key, []); }
        cells.get(key).push([x, ys[i]]);
      });
    }
    const cx = [], cy = [];
    for (const pairs of cells.values()) {
      if (pairs.length < 2) { continue; }
      const mx = mean(pairs.map((q) => q[0])), my = mean(pairs.map((q) => q[1]));
      pairs.forEach(([x, y]) => { cx.push(x - mx); cy.push(y - my); });
    }
    const pooled = pearson(rx, ry);
    const celled = pearson(cx, cy);
    return {
      total: perCond + 3 * Math.abs(pooled) + 2 * Math.abs(celled),
      pooled,
      celled,
    };
  };

  // 언덕 오르기 + 재시작. 국소해에 갇히면 조건×턴 중심화 상관이 남는다.
  const snapshot = () => Object.fromEntries(conditions.map((c) => [c, state[c].slice()]));
  const restore = (snap) => { for (const c of conditions) { state[c] = snap[c].slice(); } };
  let bestSnapshot = snapshot();
  let bestScore = score().total;
  for (let restart = 0; restart < 6; restart++) {
    if (restart > 0) {                     // 무작위로 흔든 뒤 다시 내려간다
      for (const c of conditions) {
        for (let k = state[c].length - 1; k > 0; k--) {
          const m = Math.floor(rng() * (k + 1));
          if (allowed(groups[c][k], state[c][m]) && allowed(groups[c][m], state[c][k])) {
            [state[c][k], state[c][m]] = [state[c][m], state[c][k]];
          }
        }
      }
    }
    let current = score().total;
    for (let step = 0; step < 6000; step++) {
      const c = conditions[Math.floor(rng() * conditions.length)];
      const n = state[c].length;
      const i = Math.floor(rng() * n);
      const j = Math.floor(rng() * n);
      if (i === j) { continue; }
      if (!allowed(groups[c][i], state[c][j]) || !allowed(groups[c][j], state[c][i])) { continue; }
      [state[c][i], state[c][j]] = [state[c][j], state[c][i]];
      const now = score().total;
      if (now < current - 1e-12) { current = now; } else {
        [state[c][i], state[c][j]] = [state[c][j], state[c][i]];
      }
    }
    if (current < bestScore) { bestScore = current; bestSnapshot = snapshot(); }
  }
  restore(bestSnapshot);

  for (const c of conditions) {
    groups[c].forEach((slot, i) => { slot.chars = state[c][i]; });
  }
  const perCondition = {};
  for (const c of conditions) {
    perCondition[c] = pearson(state[c], groups[c].map((g) => g.target));
  }
  const final = score();
  return {
    budget,
    overBudget,
    pooledR: final.pooled,
    cellR: final.celled,
    perCondition,
    lengths: slots.map((s) => s.chars),
  };
}

/* ══════════════════════════════════════════════════════════════════
   6. 브라우저 안에서 쓰는 조각들
   ══════════════════════════════════════════════════════════════════ */

const IN_PAGE = {
  ready: () => !!(window.__exp && typeof window.__exp.state === 'function'),
  state: () => window.__exp.state(),
  done: () => window.__exp.done() === true,
  lastDisplay: () => window.__exp.lastDisplay(),
  idle: () => (window.__e2eNet ? window.__e2eNet.inflight === 0 : true),

  /* ★ 참가자 화면이 조작을 드러내는지 — 눈금 하나가 실험 전체를 망친다.
     조건 이름·"속도/느림/빠름/기다림/지연/대기"·진행 바가 화면에 보이면
     참가자가 조작을 알아채고, 시간 추정과 불편감 응답이 요구특성으로
     오염된다 (docs/00 §1, app/static/app.js §5의 문안 규칙).
     검사 범위는 화면 틀뿐이다 — 대화 기록(#chat-log)은 참가자가 친 글과
     모델 응답이라 여기서 판정할 대상이 아니다.
     연구자 화면(#researcher-app)과 안전 오버레이는 형제 노드라 애초에 제외된다. */
  screenLeak: () => {
    const root = document.getElementById('participant-app');
    if (!root || root.hidden) { return null; }
    let text = root.innerText || '';
    const log = document.getElementById('chat-log');
    const logText = log ? (log.innerText || '') : '';
    if (logText.trim()) { text = text.split(logText).join(' '); }
    const banned = ['속도', '느림', '빠름', '기다림', '지연', '대기',
                    'immediate', 'medium', 'long', 'practice'];
    const words = banned.filter((w) => text.indexOf(w) !== -1);
    const bar = root.querySelector('progress, [role="progressbar"], .progress-bar');
    return (words.length || bar)
      ? { words, bar: bar ? (bar.id || bar.className || bar.tagName) : null }
      : null;
  },
};

/* ══════════════════════════════════════════════════════════════════
   7. 세션 진행
   ══════════════════════════════════════════════════════════════════ */

async function run(opts) {
  const { chromium } = loadPlaywright();
  const base = opts['base-url'];
  const overall = new Deadline(opts.timeout);
  const rng = mulberry32(Number(opts.seed) + Number(String(opts.participant).replace(/\D/g, '')));

  // ── 서버가 실제로 쓰는 값 (배율·범위·계획)을 먼저 읽는다.
  //    턴이 없는 세션은 로그 파일을 만들지 않으므로 분석에 섞이지 않는다.
  const probe = await postJson(`${base}/api/session/start`,
    { participant_id: opts.participant, group: opts.group });
  const ranges = probe.delay_conditions || null;
  const scale = typeof probe.delay_scale === 'number' ? probe.delay_scale : 1;
  const turnsPerConversation = probe.turns_per_conversation || 5;
  const conversations = probe.conversations || [];
  if (!conversations.length) { fail('/api/session/start 가 대화 계획을 주지 않았습니다'); }

  console.log(`■ 참가자 ${opts.participant} (${opts.group})  서버 ${base}`);
  console.log(`  블록 순서 ${JSON.stringify(probe.block_order)}  대화 ${conversations.length}개`
              + ` × ${turnsPerConversation}턴  지연배율 ${scale}`);
  if (scale !== 1) {
    console.log('  ⚠ 지연이 축소된 검증용 세션입니다. 본 실험 데이터가 아닙니다.');
  }

  const browser = await chromium.launch({ headless: !opts.headed });
  const context = await browser.newContext({ locale: 'ko-KR', timezoneId: 'Asia/Seoul' });
  // 앱의 fetch를 감싸 "쓰기가 서버에 닿았는지"를 알 수 있게 한다.
  await context.addInitScript(() => {
    window.__e2eNet = { inflight: 0, total: 0, failed: 0 };
    const original = window.fetch;
    window.fetch = function (...args) {
      window.__e2eNet.inflight += 1;
      window.__e2eNet.total += 1;
      return original.apply(this, args).then(
        (r) => { window.__e2eNet.inflight -= 1; if (!r.ok) { window.__e2eNet.failed += 1; } return r; },
        (e) => { window.__e2eNet.inflight -= 1; window.__e2eNet.failed += 1; throw e; });
    };
  });

  const page = await context.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  page.on('console', (m) => {
    if (m.type() === 'error') { consoleErrors.push(m.text().slice(0, 300)); }
  });
  page.on('pageerror', (e) => pageErrors.push(String(e && e.message || e).slice(0, 300)));

  const displays = [];
  const failures = [];
  const leaks = [];              // 참가자 화면에 드러난 조작 단서
  let sessionId = null;
  let plan = null;
  let planInfo = null;
  let predictionMismatch = 0;

  const step = (what, fn) => waitFor(what, fn, Math.min(opts['step-timeout'], Math.max(1, overall.left())));

  try {
    const url = `${base}/?e2e=1&participant=${encodeURIComponent(opts.participant)}`;
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: opts['step-timeout'] });
    await step('window.__exp 노출 (?e2e=1)', () => page.evaluate(IN_PAGE.ready));

    // ── 동의 화면 ──────────────────────────────────────────────
    let screen = (await page.evaluate(IN_PAGE.state)).screen;
    if (screen === 'consent') {
      // ?participant= 로 자동 시작하는 구현이면 잠깐 기다리는 것으로 끝난다.
      const autoStarted = await Promise.resolve()
        .then(() => waitFor('자동 시작', async () =>
          (await page.evaluate(IN_PAGE.state)).screen !== 'consent', 1500))
        .catch(() => false);
      if (!autoStarted) {
        await page.check('#consent-check');
        await page.fill('#pid', opts.participant);
        await page.selectOption('#group', opts.group);
        await page.click('#btn-start');
      }
      await step('세션 시작 (동의 → 안내)', async () =>
        (await page.evaluate(IN_PAGE.state)).screen !== 'consent');
    }

    // ── 세션 ID → 목표 지연 예측 → 길이 배정 ───────────────────
    sessionId = await step('session_id 확인', async () =>
      page.evaluate(() => { try { return localStorage.getItem('exp.lastSession'); } catch (e) { return null; } }))
      .catch(() => null);

    if (sessionId && ranges) {
      plan = buildPlan(sessionId, conversations, turnsPerConversation, ranges);
      planInfo = assignLengths(plan, ranges, Object.assign({}, opts, { scale }), rng);
      console.log(`  입력 길이 ${Math.min(...planInfo.lengths)}~${Math.max(...planInfo.lengths)}자`
        + `  (mock 예산 ${planInfo.budget}자, 예산 초과 ${planInfo.overBudget.length}개)`);
      console.log(`  계획된 부분상관 r: 조건 통제 ${planInfo.pooledR.toFixed(4)}`
        + ` · 조건×턴 통제 ${planInfo.cellR.toFixed(4)}`
        + `  조건별 ${Object.entries(planInfo.perCondition)
          .map(([c, r]) => `${c} ${r.toFixed(3)}`).join(' · ')}`);
    } else {
      console.log('  ⚠ session_id 또는 지연 범위를 얻지 못해 길이를 무작위로 배정합니다.');
      plan = buildPlan(sessionId || 'unknown', conversations, turnsPerConversation, null);
      plan.forEach((s) => { s.chars = 20 + Math.floor(rng() * 140); });
    }

    // ── 화면 순서를 따라 끝까지 ────────────────────────────────
    let guard = 0;
    for (;;) {
      overall.check('세션 진행');
      if (guard++ > 400) { throw new Error('화면 전환이 끝나지 않습니다 (무한 루프 방지)'); }
      if (await page.evaluate(IN_PAGE.done)) { break; }
      const st = await page.evaluate(IN_PAGE.state);

      switch (st.screen) {
        case 'briefing':
        case 'card':
        case 'break': {
          // 조건 이름이 새어 나온다면 대화 안내 카드가 가장 유력한 자리다.
          const cardWhere = `${st.screen}/대화 ${st.conversationIndex}`;
          const cardLeak = await page.evaluate(IN_PAGE.screenLeak);
          if (cardLeak && !leaks.some((l) => l.where === cardWhere)) {
            leaks.push({ where: cardWhere, ...cardLeak });
          }
          await advance(page, st.screen, step);
          break;
        }

        case 'practice':
        case 'chat': {
          const total = st.screen === 'practice' ? 1 : turnsPerConversation;
          if ((st.turnIndex || 0) >= total) {
            await advance(page, `${st.screen} 종료 패널`, step);
            break;
          }
          const turnIndex = (st.turnIndex || 0) + 1;
          const slot = findSlot(plan, st.conversationIndex, turnIndex);
          const chars = slot ? slot.chars : 40;
          const ctx = slot && slot.context ? slot.context : (st.context || 'a');
          const text = makeMessage(ctx, rng, chars);
          if (text.length !== chars) {
            throw new Error(`메시지 길이가 어긋납니다: ${text.length} ≠ ${chars}`);
          }
          const shown = await sendAndWait(page, text, step, opts);
          const record = {
            conversationIndex: st.conversationIndex,
            turnIndex,
            condition: st.screen === 'practice' ? 'practice' : st.condition,
            context: ctx,
            chars,
            deadline: shown.deadline,
            displayTs: shown.displayTs,
            error: shown.error,
            approxTarget: shown.approxTarget,
            expected: slot ? slot.target : null,
          };
          displays.push(record);
          const problem = displayErrorProblem(record.error, opts);
          if (problem) {
            failures.push(`대화 ${record.conversationIndex} 턴 ${record.turnIndex}`
              + ` (${record.condition}): 표시 오차 ${record.error}ms — ${problem}`);
          }
          if (slot && slot.target != null && record.approxTarget != null
              && Math.abs(record.approxTarget - slot.target) > 400) {
            predictionMismatch += 1;
          }
          const where = `대화 ${st.conversationIndex} 턴 ${turnIndex}`;
          const leak = await page.evaluate(IN_PAGE.screenLeak);
          if (leak && !leaks.some((l) => l.where === where)) {
            leaks.push({ where, ...leak });
          }
          break;
        }

        case 'survey':
        case 'engagement': {
          const before = st.screen;
          await page.evaluate(() => { window.__exp.fillSurvey(); });
          await step(`${before} 제출`, async () =>
            (await page.evaluate(IN_PAGE.state)).screen !== before);
          break;
        }

        case 'done':
          break;

        case 'consent':
          throw new Error('동의 화면으로 되돌아갔습니다');

        default:
          throw new Error(`알 수 없는 화면: ${st.screen}`);
      }
      if ((await page.evaluate(IN_PAGE.state)).screen === 'done'
          && await page.evaluate(IN_PAGE.done)) { break; }
    }

    await step('종료 처리(/end)가 서버에 닿을 때까지', () => page.evaluate(IN_PAGE.idle))
      .catch(() => null);
    if (!sessionId) {
      sessionId = await page.evaluate(() => { try { return localStorage.getItem('exp.lastSession'); } catch (e) { return null; } });
    }
  } finally {
    await page.close().catch(() => {});
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
  }

  if (pageErrors.length) {
    failures.push(`브라우저에서 처리되지 않은 예외 ${pageErrors.length}건: ${pageErrors[0]}`);
  }
  for (const l of leaks) {
    failures.push(`★ 참가자 화면(${l.where})이 조작을 드러낸다`
      + (l.words && l.words.length ? ` — 금지어 ${l.words.join(', ')}` : '')
      + (l.bar ? ` — 진행 바 ${l.bar}` : '')
      + ' (조건을 눈치채면 시간 추정·불편감 응답이 요구특성으로 오염된다)');
  }

  // ── 서버가 쓴 로그로 다시 확인한다 ───────────────────────────
  const verdict = await verifyLog({ base, sessionId, opts, plan, displays, failures,
                                    scale, turnsPerConversation, predictionMismatch });

  printSummary({ opts, displays, failures, planInfo, verdict, consoleErrors,
                 predictionMismatch, scale });

  if (opts.json) {
    fs.writeFileSync(opts.json, JSON.stringify({
      participant: opts.participant, session_id: sessionId, log_path: verdict.logPath,
      delay_scale: scale, turns: displays, failures,
      planned_r: planInfo ? planInfo.pooledR : null,
    }, null, 2), 'utf8');
  }

  return { failures, logPath: verdict.logPath };
}

function buildPlan(sessionId, conversations, turnsPerConversation, ranges) {
  const slots = [];
  for (const conv of conversations) {
    for (let t = 1; t <= turnsPerConversation; t++) {
      slots.push({
        conversationIndex: conv.index,
        turnIndex: t,
        condition: conv.condition,
        context: conv.context,
        target: ranges ? drawDelayMs(sessionId, conv.index, t, conv.condition, ranges) : null,
        chars: null,
      });
    }
  }
  return slots;
}

function findSlot(plan, conversationIndex, turnIndex) {
  return plan.find((s) => s.conversationIndex === conversationIndex && s.turnIndex === turnIndex);
}

async function advance(page, label, step) {
  const before = await page.evaluate(IN_PAGE.state);
  const clicked = await page.evaluate(() => window.__exp.advance());
  if (!clicked) { throw new Error(`${label}: 다음 버튼을 찾지 못했습니다`); }
  await step(`${label} → 다음 화면`, async () => {
    const now = await page.evaluate(IN_PAGE.state);
    return now.screen !== before.screen
      || now.conversationIndex !== before.conversationIndex;
  });
}

async function sendAndWait(page, text, step, opts) {
  await page.evaluate(({ message }) => {
    const previous = window.__exp.lastDisplay();
    window.__e2eTurn = {
      prevTurnId: previous ? previous.turnId : null,
      sentAt: Math.round(performance.timeOrigin + performance.now()),
      settled: false,
      error: null,
    };
    Promise.resolve(window.__exp.send(message)).then(
      () => { window.__e2eTurn.settled = true; },
      (e) => { window.__e2eTurn.error = String((e && e.message) || e); });
  }, { message: text });

  const shown = await step(`턴 표시 (${text.length}자)`, () => page.evaluate(() => {
    const t = window.__e2eTurn;
    if (t.error) { return { sendError: t.error }; }
    const d = window.__exp.lastDisplay();
    if (d && d.turnId !== t.prevTurnId && typeof d.displayTs === 'number') {
      return { turnId: d.turnId, deadline: d.deadline, displayTs: d.displayTs,
               error: d.error, sentAt: t.sentAt };
    }
    return null;
  }));
  if (shown.sendError) { throw new Error(`send() 실패: ${shown.sendError}`); }

  // 표시 기록(/api/turn/display)이 서버에 닿을 때까지. 앱의 send() 프라미스가
  // 해소되지 않는 구현이어도 여기서 막히지 않도록 짧게 기다리고 넘어간다.
  await waitFor('표시 기록 전송', () => page.evaluate(IN_PAGE.idle), 5000).catch(() => null);

  return {
    deadline: shown.deadline,
    displayTs: shown.displayTs,
    error: shown.error,
    approxTarget: shown.deadline - shown.sentAt,
  };
}

/* ══════════════════════════════════════════════════════════════════
   8. 로그 검증
   ══════════════════════════════════════════════════════════════════ */

async function verifyLog(args) {
  const { base, sessionId, opts, plan, displays, failures, scale,
          turnsPerConversation, predictionMismatch } = args;
  const out = { logPath: null, rows: 0 };
  if (!sessionId) {
    failures.push('session_id를 알아내지 못해 로그를 확인할 수 없습니다');
    return out;
  }

  let closed = null;
  try {
    closed = await postJson(`${base}/api/session/${encodeURIComponent(sessionId)}/end`, {});
  } catch (e) {
    failures.push(`세션 종료 확인 실패: ${e.message}`);
  }
  let logPath = closed && closed.path ? closed.path : null;
  if (!logPath) {
    const dir = opts['log-dir'] || path.join(__dirname, '..', '..', 'logs');
    logPath = path.join(dir, `${sessionId}.turns.jsonl`);
  }
  out.logPath = logPath;

  const rows = await waitFor(`로그 파일 ${logPath}`, () => {
    if (!fs.existsSync(logPath)) { return null; }
    const lines = fs.readFileSync(logPath, 'utf8').split('\n').filter((l) => l.trim());
    return lines.length ? lines.map((l) => JSON.parse(l)) : null;
  }, 10000).catch((e) => { failures.push(e.message); return null; });
  if (!rows) { return out; }
  out.rows = rows.length;

  const expectedTurns = plan.length + 1;                    // 연습 1턴 + 대화 6개 × 5턴
  if (rows.length !== expectedTurns) {
    failures.push(`로그 줄 수가 ${rows.length}입니다 (기대 ${expectedTurns})`);
  }
  if (closed && closed.incomplete) {
    failures.push(`표시되지 않은 턴 ${closed.incomplete}개가 로그에 남았습니다`);
  }

  const hashes = {};
  for (const row of rows) {
    const where = `대화 ${row.conversation_index} 턴 ${row.turn_index}`;
    if (row.display_ts == null) { failures.push(`${where}: display_ts가 비어 있습니다`); continue; }
    const imposed = row.display_ts - row.user_input_submit_ts;
    const problem = displayErrorProblem(imposed - row.target_delay_ms, opts);
    if (problem) {
      failures.push(`${where}: 부과 지연 ${imposed}ms vs 목표 ${row.target_delay_ms}ms`
        + ` — ${problem}`);
    }
    if (row.safety_flag) { failures.push(`${where}: 안전 경로가 발동했습니다 — 검사 메시지를 고칠 것`); }
    if (Number(row.delay_scale != null ? row.delay_scale : scale) !== scale) {
      failures.push(`${where}: delay_scale이 ${row.delay_scale}입니다 (기대 ${scale})`);
    }
    if (!row.practice) {
      (hashes[row.context] = hashes[row.context] || new Set()).add(row.prompt_sha256);
      const slot = findSlot(plan, row.conversation_index, row.turn_index);
      if (slot) {
        if (slot.chars != null && row.user_input_chars !== slot.chars) {
          failures.push(`${where}: 입력 길이가 ${row.user_input_chars}자입니다 (보낸 값 ${slot.chars}자)`);
        }
        if (slot.target != null && row.target_delay_ms !== slot.target) {
          args.predictionMismatch = (args.predictionMismatch || 0) + 1;
        }
      }
    }
  }
  // ★ "응답이 도착한 뒤부터 D를 센다"는 고전적 오구현 (계획서 §1 · §2).
  //   그 구현이면 부과 지연 = D + LLM 생성 시간이 되어 표시 오차가 생성
  //   시간을 그대로 따라간다. 그런데 축소 실행에서는 생성 시간이 수십~수백
  //   ms라 ±허용치 안에 통째로 숨는다 — 오차 크기만 보면 잡히지 않는다.
  //   크기 대신 **기울기**를 본다: 정상 구현 ≈ 0, 오구현 ≈ 1.
  const lat = [], err = [];
  for (const row of rows) {
    if (row.display_ts == null || row.practice || row.safety_flag) { continue; }
    lat.push(row.llm_response_ts - row.llm_request_ts);
    err.push(row.display_ts - (row.user_input_submit_ts + row.target_delay_ms));
  }
  if (lat.length >= 8) {
    const mL = mean(lat), mE = mean(err);
    let cov = 0, varL = 0;
    for (let i = 0; i < lat.length; i++) {
      cov += (lat[i] - mL) * (err[i] - mE);
      varL += (lat[i] - mL) ** 2;
    }
    const slope = varL > 0 ? cov / varL : 0;
    const r = pearson(lat, err);
    out.latencySlope = slope;
    out.latencyR = r;
    if (slope > 0.5 && Math.abs(r) > 0.5) {
      failures.push(`표시 오차가 LLM 생성 시간을 따라갑니다`
        + ` (기울기 ${slope.toFixed(2)}, r ${r.toFixed(2)}, n ${lat.length})`
        + ' — 대기 시작점 t0가 전송 시각이 아니라 응답 도착 시각인 구현입니다'
        + ' (계획서 §1: 지연이 입력 길이의 index signal이 된다)');
    }
  }
  for (const [ctx, set] of Object.entries(hashes)) {
    if (set.size !== 1) {
      failures.push(`맥락 ${ctx}에서 프롬프트 해시가 ${set.size}종입니다 — P7 위반`);
    }
  }
  const contexts = Object.keys(hashes);
  if (contexts.length === 2
      && [...hashes[contexts[0]]][0] === [...hashes[contexts[1]]][0]) {
    failures.push('맥락 A와 B의 프롬프트 해시가 같습니다 — 맥락 조작 실패');
  }
  out.mismatch = args.predictionMismatch;
  return out;
}

/* ══════════════════════════════════════════════════════════════════
   9. 요약 출력
   ══════════════════════════════════════════════════════════════════ */

function printSummary(args) {
  const { opts, displays, failures, planInfo, verdict, consoleErrors, scale } = args;
  const errs = displays.map((d) => Math.abs(d.error));
  console.log('');
  console.log('  대화  턴  조건        입력자수   목표(ms)   표시오차(ms)');
  console.log('  ' + '─'.repeat(58));
  for (const d of displays) {
    const target = d.expected != null ? d.expected
      : (d.approxTarget != null ? `~${d.approxTarget}` : '?');
    const flag = displayErrorProblem(d.error, opts) ? '  ✗' : '';
    console.log(`  ${String(d.conversationIndex).padStart(4)}`
      + `${String(d.turnIndex).padStart(5)}`
      + `  ${String(d.condition).padEnd(10)}`
      + `${String(d.chars).padStart(8)}`
      + `${String(target).padStart(11)}`
      + `${String(d.error).padStart(13)}${flag}`);
  }
  console.log('  ' + '─'.repeat(58));
  if (errs.length) {
    const sorted = [...errs].sort((a, b) => a - b);
    const signed = displays.map((d) => d.error);
    console.log(`  표시 오차 |평균| ${Math.round(mean(errs))}ms · 중앙값 `
      + `${sorted[Math.floor(sorted.length / 2)]}ms · 최대 ${Math.max(...errs)}ms`
      + `  (허용 −${opts['early-tolerance']}~+${opts.tolerance}ms, 턴 ${errs.length}개)`);
    console.log(`  가장 이른 표시 ${Math.min(...signed)}ms`
      + ' (음수는 마감 전 표시 = 조작 실패)');
  }
  if (verdict && typeof verdict.latencySlope === 'number') {
    console.log(`  표시 오차 ~ LLM 생성 시간 기울기 ${verdict.latencySlope.toFixed(3)}`
      + ` (r ${verdict.latencyR.toFixed(3)}) — 0에 가까워야 한다`);
  }
  const mismatch = Math.max(args.predictionMismatch || 0, (verdict && verdict.mismatch) || 0);
  if (planInfo) {
    console.log(`  계획된 부분상관 r: 조건 통제 ${planInfo.pooledR.toFixed(4)}`
      + ` · 조건×턴 통제 ${planInfo.cellR.toFixed(4)}`
      + (mismatch
        ? `   ⚠ 목표 지연 예측이 ${mismatch}턴에서 어긋났습니다`
          + ' — app/schedule.py의 추출 규칙이 바뀌었는지 확인하세요.'
          + ' (배정이 무의미해져 독립성 검사에 우연 상관이 남습니다)'
        : ''));
  }
  if (consoleErrors.length) {
    console.log(`  ⚠ 브라우저 console.error ${consoleErrors.length}건 (첫 건: ${consoleErrors[0]})`);
  }
  console.log(`  지연배율 ${scale}${scale !== 1 ? ' — 축소된 검증용 로그입니다' : ''}`);
  console.log(`  로그 파일: ${verdict.logPath}  (${verdict.rows}줄)`);

  if (failures.length) {
    console.log('');
    console.log(`✗ ${opts.participant} 실패 ${failures.length}건`);
    failures.slice(0, 20).forEach((f) => console.log(`  - ${f}`));
    if (failures.length > 20) { console.log(`  … 외 ${failures.length - 20}건`); }
  } else {
    console.log(`✓ ${opts.participant} 세션 완료 — 턴 ${displays.length}개, 표시 오차 모두 허용 안`);
  }
}

/* ══════════════════════════════════════════════════════════════════ */

if (require.main === module) {
  const opts = parseArgs(process.argv);
  run(opts).then((result) => {
    process.exit(result.failures.length ? 1 : 0);
  }).catch((e) => {
    fail(`${opts.participant}: ${e && e.stack ? e.stack : e}`);
  });
}

module.exports = { drawDelayMs, assignLengths, makeMessage, buildPlan, pearson,
                   displayErrorProblem, IN_PAGE };
