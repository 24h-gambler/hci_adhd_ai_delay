"""app/schedule.py 계약 검사.

기준 문서
  - app/CONTRACT.md  §0(P1, P8), §4(조건 배정 · 상쇄)
  - docs/00-study-design-overview.md §1
  - materials/02-participant-briefing.md §4 (연습 턴)

이 파일이 기준이다. 구현이 여기와 어긋나면 구현이 틀린 것이다.
"""

import hashlib
import inspect
import itertools
import json
import os
import pathlib
import random
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import schedule  # noqa: E402
import config  # noqa: E402

APP_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parent
CONFIG_PATH = REPO_ROOT / 'prompts' / 'prompts.yaml'

# 결정론 검사에 쓰는 고정 범위.
# prompts.yaml의 값이 파일럿에서 바뀌어도(OPEN_QUESTIONS Q12) 결정론 검사는
# 흔들리지 않아야 하므로 여기서는 리터럴을 쓴다.
# 범위 자체에 대한 검사는 load_config()가 준 값으로 한다.
FIXED_RANGES = {
    'immediate': {'min_ms': 1000, 'max_ms': 2000},
    'medium': {'min_ms': 8000, 'max_ms': 9000},
    'long': {'min_ms': 16000, 'max_ms': 20000},
    'practice': {'fixed_ms': 800},
}

SESSION_ID = 'P07-1756400000000'


def live_ranges():
    """실제 prompts.yaml에 설정된 지연 범위."""
    return config.load_config(str(CONFIG_PATH))['delay_conditions']


def as_lists(perms):
    """PERMS 원소가 list든 tuple이든 같은 형태로 비교하기 위한 정규화."""
    return [list(p) for p in perms]


# ─────────────────────────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────────────────────────
class TestScheduleConstants(unittest.TestCase):

    def test_conditions_are_the_three_delay_levels(self):
        self.assertEqual(list(schedule.CONDITIONS), ['immediate', 'medium', 'long'])

    def test_perms_contains_exactly_six_distinct_permutations(self):
        """CONTRACT §4 — PERMS는 3개 조건의 6가지 순열이다."""
        perms = as_lists(schedule.PERMS)
        self.assertEqual(len(perms), 6, 'PERMS는 정확히 6개여야 한다')
        as_tuples = {tuple(p) for p in perms}
        self.assertEqual(len(as_tuples), 6, 'PERMS에 중복이 있다')
        expected = {tuple(p) for p in itertools.permutations(schedule.CONDITIONS)}
        self.assertEqual(as_tuples, expected)

    def test_perms_is_in_fixed_sorted_order(self):
        """CONTRACT §4 — "사전순 고정". 순서가 흔들리면 참가자 배정이 통째로 바뀐다."""
        perms = as_lists(schedule.PERMS)
        self.assertEqual(perms, sorted(perms), 'PERMS는 사전순으로 정렬되어 있어야 한다')
        # 사전순 첫 원소는 CONTRACT §4에 그대로 적혀 있다.
        self.assertEqual(perms[0], ['immediate', 'long', 'medium'])

    def test_practice_conversation_index_is_zero(self):
        """CONTRACT §2 — conversation_index 0은 연습 턴 전용이다."""
        self.assertEqual(schedule.PRACTICE_CONVERSATION_INDEX, 0)


# ─────────────────────────────────────────────────────────────────────
# participant_number / make_session_id
# ─────────────────────────────────────────────────────────────────────
class TestParticipantNumber(unittest.TestCase):

    def test_parses_zero_padded_ids(self):
        """CONTRACT §4 — N은 participant_id의 숫자 부분이다 (P07 → 7)."""
        for pid, expected in [('P01', 1), ('P07', 7), ('P12', 12),
                              ('P1', 1), ('P24', 24), ('P007', 7)]:
            with self.subTest(pid=pid):
                got = schedule.participant_number(pid)
                self.assertEqual(got, expected)
                self.assertIs(type(got), int)

    def test_raises_value_error_without_digits(self):
        for pid in ['PXX', 'P', '', '참가자', 'pilot']:
            with self.subTest(pid=pid):
                with self.assertRaises(ValueError):
                    schedule.participant_number(pid)


class TestSessionId(unittest.TestCase):

    def test_make_session_id_format(self):
        """CONTRACT §2 — session_id = f'{participant_id}-{start_ts_ms}'."""
        self.assertEqual(schedule.make_session_id('P01', 1756400000000),
                         'P01-1756400000000')
        self.assertEqual(schedule.make_session_id('P12', 1), 'P12-1')
        # 로그에서 참가자와 세션 시작 시각을 다시 분리할 수 있어야 한다.
        sid = schedule.make_session_id('P07', 1756400000000)
        pid, _, ts = sid.rpartition('-')
        self.assertEqual(pid, 'P07')
        self.assertEqual(int(ts), 1756400000000)


# ─────────────────────────────────────────────────────────────────────
# session_plan
# ─────────────────────────────────────────────────────────────────────
class TestSessionPlan(unittest.TestCase):

    def plan(self, n):
        return schedule.session_plan('P%02d' % n)

    def test_six_conversations_indices_1_to_6(self):
        """docs/00 §1 — 참가자 1명당 대화 6개."""
        for n in range(1, 25):
            with self.subTest(n=n):
                convs = self.plan(n)['conversations']
                self.assertEqual(len(convs), 6)
                self.assertEqual([c['index'] for c in convs], [1, 2, 3, 4, 5, 6])

    def test_block_layout_is_three_and_three(self):
        for n in range(1, 25):
            with self.subTest(n=n):
                convs = self.plan(n)['conversations']
                self.assertEqual([c['block'] for c in convs], [1, 1, 1, 2, 2, 2])

    def test_each_block_contains_each_condition_once(self):
        """블록 안에서 3수준이 한 번씩 — 참가자 내 설계의 전제."""
        for n in range(1, 25):
            for block in (1, 2):
                with self.subTest(n=n, block=block):
                    conds = [c['condition'] for c in self.plan(n)['conversations']
                             if c['block'] == block]
                    self.assertEqual(sorted(conds), sorted(schedule.CONDITIONS))

    def test_block_order_follows_participant_parity(self):
        """CONTRACT §4 — N 홀수 → [a, b], N 짝수 → [b, a]."""
        for n in range(1, 25):
            with self.subTest(n=n):
                expected = ['a', 'b'] if n % 2 == 1 else ['b', 'a']
                self.assertEqual(list(self.plan(n)['block_order']), expected)

    def test_context_follows_block_order(self):
        """블록 1의 맥락 = block_order[0], 블록 2의 맥락 = block_order[1]."""
        for n in range(1, 25):
            with self.subTest(n=n):
                plan = self.plan(n)
                order = list(plan['block_order'])
                for c in plan['conversations']:
                    self.assertEqual(c['context'], order[c['block'] - 1])

    def test_each_condition_context_pair_appears_exactly_once(self):
        """docs/00 §1 — 대화 6개 = 3(지연) × 2(맥락)."""
        for n in range(1, 25):
            with self.subTest(n=n):
                pairs = {(c['context'], c['condition'])
                         for c in self.plan(n)['conversations']}
                self.assertEqual(len(pairs), 6)
                self.assertEqual(
                    pairs,
                    {(ctx, cond) for ctx in ('a', 'b') for cond in schedule.CONDITIONS})

    def test_plan_is_deterministic(self):
        """같은 참가자를 두 번 계획해도 같아야 한다 — 세션 중단 후 재개 대비."""
        for n in (1, 2, 7, 12, 23):
            with self.subTest(n=n):
                self.assertEqual(self.plan(n), self.plan(n))

    def test_plan_depends_only_on_participant_number(self):
        """CONTRACT §4 — 배정은 N의 함수다. 'P07'과 'P7'은 같은 계획이다."""
        a = schedule.session_plan('P07')
        b = schedule.session_plan('P7')
        self.assertEqual(a['block_order'], b['block_order'])
        self.assertEqual([c['condition'] for c in a['conversations']],
                         [c['condition'] for c in b['conversations']])
        self.assertEqual([c['context'] for c in a['conversations']],
                         [c['context'] for c in b['conversations']])

    def test_plan_reports_participant_number(self):
        for n in (1, 7, 12, 24):
            with self.subTest(n=n):
                plan = self.plan(n)
                self.assertEqual(plan['participant_number'], n)
                self.assertIs(type(plan['participant_number']), int)

    def test_practice_index_absent_from_plan(self):
        """P8 — 연습 턴(conversation_index 0)은 세션 계획에 없다."""
        for n in range(1, 25):
            with self.subTest(n=n):
                indices = [c['index'] for c in self.plan(n)['conversations']]
                self.assertNotIn(schedule.PRACTICE_CONVERSATION_INDEX, indices)


# ─────────────────────────────────────────────────────────────────────
# 상쇄 (counterbalancing)
# ─────────────────────────────────────────────────────────────────────
class TestCounterbalancing(unittest.TestCase):

    def block_conditions(self, n, block):
        convs = schedule.session_plan('P%02d' % n)['conversations']
        return [c['condition'] for c in convs if c['block'] == block]

    def test_blocks_never_share_condition_order(self):
        """CONTRACT §4 — "어떤 N에서도 두 블록의 조건 순서가 같으면 안 된다."""
        for n in range(1, 25):
            with self.subTest(n=n):
                self.assertNotEqual(self.block_conditions(n, 1),
                                    self.block_conditions(n, 2),
                                    'N=%d에서 두 블록의 조건 순서가 같다 (블록 간 재무작위화 실패)' % n)

    def test_block_condition_orders_follow_perms_formula(self):
        """CONTRACT §4의 배정식 그대로:
             블록 1 → PERMS[(N-1) % 6]
             블록 2 → PERMS[(N-1+3) % 6]
        (CONTRACT §2의 예시 JSON은 P01 = 이 식의 결과다. 어긋나면 §4가 규범이다.)
        """
        perms = as_lists(schedule.PERMS)
        for n in range(1, 25):
            with self.subTest(n=n):
                self.assertEqual(self.block_conditions(n, 1), perms[(n - 1) % 6])
                self.assertEqual(self.block_conditions(n, 2), perms[(n - 1 + 3) % 6])

    def test_condition_position_coverage_over_first_twelve_participants(self):
        """참가자 1~12에서 각 조건이 블록 내 각 위치에 최소 1회씩 나타난다.
        한 조건이 늘 첫 번째면 순서 효과와 조건 효과가 분리되지 않는다."""
        for block in (1, 2):
            for position in (0, 1, 2):
                with self.subTest(block=block, position=position):
                    seen = {self.block_conditions(n, block)[position]
                            for n in range(1, 13)}
                    self.assertEqual(seen, set(schedule.CONDITIONS),
                                     '블록 %d 위치 %d에서 빠진 조건: %s'
                                     % (block, position, set(schedule.CONDITIONS) - seen))

    def test_block_order_is_balanced_over_first_twelve_participants(self):
        """맥락 순서도 절반씩 상쇄되어야 한다."""
        orders = [tuple(schedule.session_plan('P%02d' % n)['block_order'])
                  for n in range(1, 13)]
        self.assertEqual(orders.count(('a', 'b')), 6)
        self.assertEqual(orders.count(('b', 'a')), 6)


# ─────────────────────────────────────────────────────────────────────
# draw_delay_ms — 이 연구에서 가장 중요한 함수
# ─────────────────────────────────────────────────────────────────────
class TestDrawDelay(unittest.TestCase):

    def test_delay_independent_of_text(self):
        """★ P1의 구조적 증명.

        CONTRACT §0 — "입력 텍스트가 시드에 들어가지 않으므로 D는 텍스트의
        함수일 수 없다." 텍스트를 받을 인자가 존재하지 않는다는 것을 먼저
        확인하고, 그 다음 같은 좌표에 대한 1000회 호출이 완전히 동일함을 본다.
        """
        sig = inspect.signature(schedule.draw_delay_ms)
        params = list(sig.parameters)
        self.assertEqual(
            params[:5],
            ['session_id', 'conversation_index', 'turn_index', 'condition', 'ranges'])
        self.assertEqual(len(params), 5,
                         'draw_delay_ms는 5개 인자만 받는다. 추가 인자는 P1의 구멍이다.')

        forbidden = {'text', 'user_text', 'user_input', 'user_input_text', 'message',
                     'content', 'prompt', 'input', 'chars', 'n_chars', 'length'}
        self.assertEqual(set(p.lower() for p in params) & forbidden, set(),
                         'draw_delay_ms가 입력 내용을 받을 수 있으면 P1이 깨진다')

        # 텍스트를 넘기는 것이 애초에 불가능해야 한다.
        with self.assertRaises(TypeError):
            schedule.draw_delay_ms(SESSION_ID, 1, 1, 'medium', FIXED_RANGES, '안녕하세요')
        with self.assertRaises(TypeError):
            schedule.draw_delay_ms(SESSION_ID, 1, 1, 'medium', FIXED_RANGES,
                                   text='오늘 회사에서 있었던 일을 이야기하고 싶어요')

        first = schedule.draw_delay_ms(SESSION_ID, 1, 1, 'medium', FIXED_RANGES)
        self.assertIs(type(first), int)
        for i in range(1000):
            got = schedule.draw_delay_ms(SESSION_ID, 1, 1, 'medium', FIXED_RANGES)
            self.assertEqual(got, first, '%d번째 호출에서 값이 달라졌다' % i)
            self.assertIs(type(got), int)
            self.assertEqual(repr(got), repr(first))

    def test_delay_ignores_global_random_state(self):
        """전역 random 상태를 건드려도 값이 바뀌면 안 된다.
        모듈 수준 random을 쓰는 구현은 재현이 불가능하고 호출 순서에 의존한다."""
        turns = list(range(1, 11))
        base = [schedule.draw_delay_ms(SESSION_ID, 2, t, 'long', FIXED_RANGES)
                for t in turns]

        # 전역 random 상태는 반드시 되돌린다. 되돌리지 않으면 같은 프로세스에서
        # 뒤에 도는 검사들이 고정 시드를 물려받아, 검사 순서에 따라 결함이
        # 가려지거나 재현되지 않는다.
        saved_state = random.getstate()
        try:
            random.seed(0)
            perturbed = []
            for t in turns:
                random.random()
                perturbed.append(
                    schedule.draw_delay_ms(SESSION_ID, 2, t, 'long', FIXED_RANGES))
                random.seed(t * 7919)
        finally:
            random.setstate(saved_state)
        self.assertEqual(base, perturbed)

        # 호출 순서를 뒤집어도 같아야 한다.
        reversed_draws = {}
        for t in reversed(turns):
            reversed_draws[t] = schedule.draw_delay_ms(SESSION_ID, 2, t, 'long', FIXED_RANGES)
        self.assertEqual(base, [reversed_draws[t] for t in turns])

    def test_delay_is_reproducible_across_processes(self):
        """CONTRACT §0 — "로그에 session_id가 있으므로 사후에 D를 재현할 수 있다."
        내장 hash()를 쓰면 프로세스마다 값이 달라져 재현이 불가능해진다."""
        code = (
            'import sys, json\n'
            'sys.path.insert(0, %r)\n'
            'import schedule\n'
            'ranges = json.loads(%r)\n'
            'print(json.dumps([schedule.draw_delay_ms(%r, 3, t, "long", ranges)'
            ' for t in range(1, 11)]))\n'
        ) % (str(APP_DIR), json.dumps(FIXED_RANGES), SESSION_ID)

        outputs = []
        for hash_seed in ('0', '1', '12345'):
            env = dict(os.environ, PYTHONHASHSEED=hash_seed)
            proc = subprocess.run([sys.executable, '-c', code],
                                  capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs.append(json.loads(proc.stdout))

        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])
        here = [schedule.draw_delay_ms(SESSION_ID, 3, t, 'long', FIXED_RANGES)
                for t in range(1, 11)]
        self.assertEqual(outputs[0], here)

    def test_delay_matches_the_documented_seed_formula(self):
        """★ CONTRACT §0의 식 그대로인지 — 사후 재현 가능성의 전제.

            D = uniform(min, max),  seed = SHA256(f"{session_id}|{conversation_index}|{turn_index}")

        결정론과 프로세스 독립성만으로는 부족하다. 구현이 시드 재료를 바꾸면
        (예: 참가자 번호만 쓰거나, 구분자를 '-'로 바꾸거나, 다이제스트를 md5로
        바꾸면) 위의 다른 검사는 **전부 그대로 통과**하지만, 이미 수집한 로그의
        session_id로 D를 다시 계산할 수 없게 된다. CONTRACT §0이 시드 결정론을
        택한 유일한 이유가 그 재현 가능성이므로 여기서 식을 못 박는다.

        그래서 이 검사는 구현을 호출하지 않고 계약 문서의 식을 직접 다시
        구현해 대조한다.
        """
        ranges = live_ranges()
        for sid in (SESSION_ID, 'P01-1', 'P24-1756400009999'):
            for conv in range(1, 7):
                for turn in range(1, 6):
                    seed = ('%s|%d|%d' % (sid, conv, turn)).encode('utf-8')
                    unit = int.from_bytes(hashlib.sha256(seed).digest()[:8], 'big') / 2 ** 64
                    for condition in schedule.CONDITIONS:
                        lo = ranges[condition]['min_ms']
                        hi = ranges[condition]['max_ms']
                        expected = lo + int(unit * (hi - lo))
                        with self.subTest(sid=sid, conv=conv, turn=turn, condition=condition):
                            self.assertEqual(
                                schedule.draw_delay_ms(sid, conv, turn, condition, ranges),
                                expected,
                                'CONTRACT §0의 시드 식과 다르다 — 로그로 D를 재현할 수 없다')

    def test_unknown_condition_raises(self):
        """조건 이름이 틀리면 조용히 다른 범위로 떨어지면 안 된다.

        기본 범위로 되돌아가거나 첫 조건으로 흘러가면 그 대화 5턴이 통째로
        다른 조건으로 돌아가는데, 로그에는 요청받은 조건 이름이 그대로 남는다.
        조건 라벨과 실제 지연이 어긋난 데이터는 사후에 구제할 수 없다.
        """
        for condition in ('inmediate', 'Medium', 'LONG', 'fast', '', None, 0):
            with self.subTest(condition=condition):
                with self.assertRaises(ValueError):
                    schedule.draw_delay_ms(SESSION_ID, 1, 1, condition, FIXED_RANGES)

    def test_practice_condition_label_is_rejected_outside_practice_index(self):
        """'practice'는 conversation_index 0 전용 라벨이다 (CONTRACT §2).

        분석 대상 대화에서 이 라벨이 통과해 버리면 그 턴은 800ms 고정으로
        돌면서 로그에는 practice: false, 즉 분석 대상으로 남는다.
        """
        for ranges in (FIXED_RANGES, live_ranges()):
            for conv in range(1, 7):
                with self.subTest(conv=conv):
                    with self.assertRaises(ValueError):
                        schedule.draw_delay_ms(SESSION_ID, conv, 1, 'practice', ranges)

    def sample(self, condition, ranges, n_participants=20):
        draws = []
        for pid in range(1, n_participants + 1):
            sid = schedule.make_session_id('P%02d' % pid, 1756400000000 + pid)
            for conv in range(1, 7):
                for turn in range(1, 6):
                    draws.append(schedule.draw_delay_ms(sid, conv, turn, condition, ranges))
        return draws

    def test_delay_within_configured_range(self):
        """조작 충실도 — 조건별 지연 분포가 설정 범위 안에 100% 들어와야 한다
        (analysis/10 §2)."""
        ranges = live_ranges()
        for condition in schedule.CONDITIONS:
            lo = ranges[condition]['min_ms']
            hi = ranges[condition]['max_ms']
            with self.subTest(condition=condition):
                for d in self.sample(condition, ranges):
                    self.assertIs(type(d), int)
                    self.assertGreaterEqual(d, lo)
                    self.assertLessEqual(d, hi)

    def test_delay_spread_covers_range(self):
        """범위 안에서 실제로 균등하게 뽑혀야 한다.
        하한에 붙어 있거나 상수면 조건 내 분산이 사라져 설계가 무의미해진다."""
        ranges = live_ranges()
        for condition in schedule.CONDITIONS:
            lo = ranges[condition]['min_ms']
            hi = ranges[condition]['max_ms']
            width = hi - lo
            with self.subTest(condition=condition):
                draws = self.sample(condition, ranges)
                spread = max(draws) - min(draws)
                self.assertGreaterEqual(
                    spread, 0.6 * width,
                    '%s: 관측 폭 %dms가 범위 폭 %dms의 60%%에 못 미친다'
                    % (condition, spread, width))
                self.assertGreaterEqual(
                    len(set(draws)), 50,
                    '%s: 서로 다른 값이 %d개뿐이다 (ms 해상도로 뽑히지 않는다)'
                    % (condition, len(set(draws))))

    def test_delay_varies_with_turn_index(self):
        """턴마다 다시 뽑는다 (docs/00 §1 "턴마다 균등분포에서 무작위 추출").
        세션당 상수면 턴 내 변동이 사라진다."""
        for condition in schedule.CONDITIONS:
            with self.subTest(condition=condition):
                draws = [schedule.draw_delay_ms(SESSION_ID, 1, t, condition, FIXED_RANGES)
                         for t in range(1, 21)]
                self.assertGreaterEqual(len(set(draws)), 10,
                                        '%s: 턴이 달라도 값이 거의 같다' % condition)

    def test_delay_varies_with_conversation_index(self):
        """시드에 conversation_index가 들어간다 — 대화가 바뀌면 수열도 바뀐다."""
        seqs = [tuple(schedule.draw_delay_ms(SESSION_ID, conv, t, 'medium', FIXED_RANGES)
                      for t in range(1, 6))
                for conv in range(1, 7)]
        self.assertEqual(len(set(seqs)), 6, '대화 index가 시드에 반영되지 않는다')

    def test_delay_varies_with_session_id(self):
        """CONTRACT §0 — "session_id가 들어가므로 참가자·세션마다 다른 수열이 나온다."""
        seqs = []
        for pid in range(1, 21):
            sid = schedule.make_session_id('P%02d' % pid, 1756400000000 + pid)
            seqs.append(tuple(schedule.draw_delay_ms(sid, 1, t, 'medium', FIXED_RANGES)
                              for t in range(1, 6)))
        self.assertEqual(len(set(seqs)), 20, '세션이 달라도 같은 수열이 나온다')

        # 같은 참가자라도 세션 시작 시각이 다르면 다른 수열이어야 한다.
        a = schedule.draw_delay_ms('P07-1756400000000', 1, 1, 'medium', FIXED_RANGES)
        b = schedule.draw_delay_ms('P07-1756400000001', 1, 1, 'medium', FIXED_RANGES)
        self.assertNotEqual(a, b)

    def test_condition_selects_the_band(self):
        """같은 (session, conv, turn)이라도 조건이 다르면 각 조건의 범위 안에 떨어진다."""
        ranges = live_ranges()
        values = {}
        for condition in schedule.CONDITIONS:
            d = schedule.draw_delay_ms(SESSION_ID, 4, 2, condition, ranges)
            values[condition] = d
            self.assertGreaterEqual(d, ranges[condition]['min_ms'])
            self.assertLessEqual(d, ranges[condition]['max_ms'])
        self.assertEqual(len(set(values.values())), 3,
                         'condition 인자가 실제로 쓰이지 않는다: %r' % (values,))

    def test_returns_plain_int(self):
        """로그의 target_delay_ms는 정수 ms다 (analysis/10 §6)."""
        d = schedule.draw_delay_ms(SESSION_ID, 1, 1, 'immediate', FIXED_RANGES)
        self.assertIs(type(d), int)
        self.assertEqual(json.loads(json.dumps({'target_delay_ms': d}))['target_delay_ms'], d)


# ─────────────────────────────────────────────────────────────────────
# 연습 턴 (P8)
# ─────────────────────────────────────────────────────────────────────
class TestPracticeDelay(unittest.TestCase):

    def test_practice_index_returns_fixed_ms_for_every_condition(self):
        """P8 · materials/02 §4 — 연습 턴은 조건 지연을 쓰지 않고 고정 0.8초다."""
        for ranges in (FIXED_RANGES, live_ranges()):
            fixed = ranges['practice']['fixed_ms']
            for condition in list(schedule.CONDITIONS) + ['practice']:
                for sid in (SESSION_ID, 'P01-1', 'P12-999999999999'):
                    for turn in range(1, 6):
                        with self.subTest(condition=condition, sid=sid, turn=turn):
                            d = schedule.draw_delay_ms(
                                sid, schedule.PRACTICE_CONVERSATION_INDEX,
                                turn, condition, ranges)
                            self.assertEqual(d, fixed)
                            self.assertIs(type(d), int)

    def test_practice_delay_is_outside_every_condition_range(self):
        """materials/02 §4 — "즉시 조건 범위도 아니다". 연습이 기준선을 만들면 안 된다."""
        ranges = live_ranges()
        fixed = ranges['practice']['fixed_ms']
        for condition in schedule.CONDITIONS:
            with self.subTest(condition=condition):
                self.assertFalse(
                    ranges[condition]['min_ms'] <= fixed <= ranges[condition]['max_ms'],
                    '연습 고정 지연 %dms가 %s 조건 범위 안에 있다' % (fixed, condition))


if __name__ == '__main__':
    unittest.main()
