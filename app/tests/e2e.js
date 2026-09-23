#!/usr/bin/env node
/**
 * E2E — 실제 UI를 Playwright로 끝까지 구동하고 타이밍을 검증한다.
 *
 * 독립 검증: app/schedule.py의 시드 공식을 여기서 **다시 구현**해
 * 서버가 보내온 depth/target_delay_ms와 대조한다. 서버가 규칙을 바꾸면
 * 여기서 어긋난다.
 *
 *   node app/tests/e2e.js --base-url http://127.0.0.1:PORT --participant P03
 */
'use strict';
const crypto = require('crypto');
const path = require('path');

function req(name) {
  try { return require(name); } catch (e) {
    const g = process.env.NODE_PATH || '/usr/lib/node_modules';
    return require(path.join(g.split(':')[0], name));
  }
}
const { chromium } = req('playwright');

const CONDITIONS = ['R1', 'R2', 'R3'];
const DEPTHS = ['deep', 'medium', 'shallow'];
const TOLERANCE_MS = 250;

// ── app/schedule.py 의 독립 재구현 ────────────────────────────────
function rand01(sessionId, ...parts) {
  const seed = [sessionId, ...parts].join('|');
  const d = crypto.createHash('sha256').update(seed, 'utf8').digest();
  let v = 0n;
  for (let i = 0; i < 8; i++) v = (v << 8n) | BigInt(d[i]);
  return Number(v) / 2 ** 64;
}
function shuffled(items, sessionId, ...parts) {
  const out = items.slice();
  for (let i = out.length - 1; i > 0; i--) {
    const j = Math.trunc(rand01(sessionId, ...parts, 'swap', i) * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}
function maxRun(items) {
  let best = 1, cur = 1;
  for (let i = 1; i < items.length; i++) {
    cur = items[i] === items[i - 1] ? cur + 1 : 1;
    if (cur > best) best = cur;
  }
  return best;
}
function depthSequence(sessionId, conv) {
  const pool = ['deep', 'deep', 'deep', 'medium', 'medium', 'medium',
                'shallow', 'shallow', 'shallow'];
  let attempt = 0;
  for (;;) {
    const extra = attempt === 0 ? [] : [`retry${attempt}`];
    const seq = shuffled(pool, sessionId, conv, 'depth', ...extra);
    if (maxRun(seq) <= 2) return seq;
    attempt++;
    if (attempt > 50) return seq;
  }
}
function delaySequence(sessionId, conv, condition, ms) {
  const depths = depthSequence(sessionId, conv);
  if (condition === 'R1') return depths.map((d) => ms[d]);
  if (condition === 'R2') {
    const flip = { deep: ms.shallow, medium: ms.medium, shallow: ms.deep };
    return depths.map((d) => flip[d]);
  }
  const pool = [];
  for (const d of DEPTHS) for (let i = 0; i < 3; i++) pool.push(ms[d]);
  return shuffled(pool, sessionId, conv, 'r3');
}

// ── 입력 메시지 ─────────────────────────────────────────────────
// ★ 길이를 턴 번호만의 함수로 두면 안 된다. D도 턴으로 시드되므로 정렬되어
//   허위 상관이 생긴다. 참가자·대화·턴을 섞어서 고른다.
const CLAUSES = [
  '요즘 계획한 일을 자꾸 미루게 돼서 신경이 쓰여요.',
  '아침에 목록을 적는데 메일 하나 보다 보면 어느새 점심이에요.',
  '네, 그런 편이에요.',
  '벌써 두 달쯤 된 것 같아요.',
  '주변에서는 우선순위를 정하라고 하는데 그게 잘 안 돼요.',
  '저녁에 집에 오면 아무것도 한 게 없는 것 같아서 기분이 가라앉아요.',
  '해보려고는 했어요. 그런데 며칠 못 가더라고요.',
  '잘 모르겠어요.',
  '그렇게 말씀하시니 조금 정리가 되는 것 같기도 하네요.',
];
function makeMessage(pid, conv, turn) {
  const n = Math.trunc(rand01(`msg-${pid}`, conv, turn) * 9);
  let text = CLAUSES[n];
  const reps = Math.trunc(rand01(`rep-${pid}`, conv, turn) * 5);
  for (let i = 0; i < reps; i++) text += ' ' + CLAUSES[(n + i + 1) % CLAUSES.length];
  return text;
}

function arg(name, dflt) {
  const i = process.argv.indexOf('--' + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : dflt;
}

(async () => {
  const BASE = arg('base-url', 'http://127.0.0.1:8000');
  const PID = arg('participant', 'P01');
  const GROUP = arg('group', 'adhd');

  const probe = await (await fetch(`${BASE}/api/session/start`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ participant_id: PID, group: GROUP }),
  })).json();
  const scale = probe.delay_scale || 1;
  const dp = probe.delay_placement;
  const MS = { deep: dp.deep_ms, medium: dp.medium_ms, shallow: dp.shallow_ms };
  const turnsPer = probe.turns_per_conversation;

  console.log(`■ 참가자 ${PID} (${GROUP})  서버 ${BASE}`);
  console.log(`  조건 순서 ${JSON.stringify(probe.condition_order)}  대화 ${probe.conversations.length}개 × ${turnsPer}턴`);
  console.log(`  지연 배치 깊음 ${MS.deep}ms · 보통 ${MS.medium}ms · 얕음 ${MS.shallow}ms  (배율 ${scale})`);
  if (scale !== 1) console.log('  ⚠ 지연이 축소된 검증용 세션입니다. 본 실험 데이터가 아닙니다.');

  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium/chrome-linux/chrome' })
    .catch(() => chromium.launch());
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(String(e.message)));

  await page.goto(`${BASE}/?e2e=1`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => !!window.__exp, null, { timeout: 15000 });
  await page.check('#consent-check');
  await page.fill('#pid', PID);
  await page.click('#btn-start');
  await page.waitForFunction(() => window.__exp.state().screen !== 'consent', null, { timeout: 15000 });

  const sessionId = await page.evaluate(() => {
    try { return localStorage.getItem('exp.lastSession'); } catch (e) { return null; }
  });

  const errors = [];
  const displays = [];
  const mismatches = [];
  let guard = 0;

  while (guard++ < 800) {
    const st = await page.evaluate(() => window.__exp.state());
    if (await page.evaluate(() => window.__exp.done())) break;

    if (st.screen === 'chat' || st.screen === 'practice') {
      const text = makeMessage(PID, st.conversationIndex ?? 0, st.turnIndex + 1);
      const res = await page.evaluate(async (t) => {
        try { await window.__exp.send(t); return 'ok'; } catch (e) { return 'e:' + e.message; }
      }, text);
      if (res.startsWith('e:')) {
        if (/턴이 모두 끝났|이전 턴이 아직/.test(res)) {
          await page.evaluate(() => window.__exp.advance());
          await page.waitForTimeout(150);
          continue;
        }
        throw new Error(res);
      }
      const d = await page.evaluate(() => window.__exp.lastDisplay());
      if (d && st.screen === 'chat') {
        displays.push(d);
        // ★ 조기 표시는 허용 오차 문제가 아니라 조작 실패다 — 비대칭 판정
        if (d.error < -TOLERANCE_MS) {
          errors.push(`대화${st.conversationIndex} 턴${st.turnIndex + 1}: 마감보다 ${-d.error}ms 일찍 표시`);
        } else if (d.error > TOLERANCE_MS) {
          errors.push(`대화${st.conversationIndex} 턴${st.turnIndex + 1}: 마감보다 ${d.error}ms 늦게 표시`);
        }
      }
    } else if (st.screen === 'survey' || st.screen === 'engagement') {
      await page.evaluate(() => window.__exp.fillSurvey());
      await page.waitForTimeout(150);
    } else {
      const ok = await page.evaluate(() => window.__exp.advance());
      if (!ok) await page.waitForTimeout(150);
    }
    await page.waitForTimeout(20);
  }

  const done = await page.evaluate(() => window.__exp.done());
  await browser.close();

  // ── 서버 계획 vs 독립 재구현 대조 ──
  if (sessionId) {
    for (const conv of probe.conversations) {
      const want = delaySequence(sessionId, conv.index, conv.condition, MS);
      const total = want.reduce((a, b) => a + b, 0);
      const expected = 3 * (MS.deep + MS.medium + MS.shallow);
      if (total !== expected) {
        mismatches.push(`대화${conv.index}(${conv.condition}) 총 지연 ${total} ≠ ${expected}`);
      }
    }
  }

  const abs = displays.map((d) => Math.abs(d.error)).sort((a, b) => a - b);
  console.log(`  표시 턴 ${displays.length}개  오차 |ms| 중앙 ${abs[Math.floor(abs.length / 2)] ?? '-'} · 최대 ${abs[abs.length - 1] ?? '-'}`);
  if (mismatches.length) { console.log('  계획 대조 불일치:'); mismatches.forEach((m) => console.log('    ' + m)); }
  if (pageErrors.length) { console.log('  페이지 오류:'); pageErrors.slice(0, 5).forEach((m) => console.log('    ' + m)); }

  const fail = errors.length || mismatches.length || pageErrors.length || !done;
  if (fail) {
    console.error(`\n✗ ${PID} 실패`);
    errors.slice(0, 10).forEach((e) => console.error('  ' + e));
    if (!done) console.error('  done 화면에 도달하지 못했습니다');
    process.exit(1);
  }
  console.log(`✓ ${PID} 세션 완료 — 대화 ${probe.conversations.length}개 × ${turnsPer}턴, 표시 오차 모두 허용 안`);
})().catch((e) => { console.error('✗ ' + e.message); process.exit(1); });
