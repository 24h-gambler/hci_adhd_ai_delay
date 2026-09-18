"""schedule.py — 지연 배치 3조건 설계의 성질을 검사한다.

기준: docs/13-redesign-delay-placement.md, app/CONTRACT.md
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import schedule as S  # noqa: E402


class TestConstants(unittest.TestCase):
    def test_three_conditions(self):
        self.assertEqual(S.CONDITIONS, ["R1", "R2", "R3"])

    def test_perms_are_the_six_distinct_permutations(self):
        self.assertEqual(len(S.PERMS), 6)
        self.assertEqual(len({tuple(p) for p in S.PERMS}), 6)
        for p in S.PERMS:
            self.assertEqual(sorted(p), sorted(S.CONDITIONS))
        self.assertEqual(S.PERMS, sorted(S.PERMS), "고정 정렬 순서여야 재현된다")

    def test_three_conversations_nine_turns(self):
        self.assertEqual(S.CONVERSATIONS, 3, "3블록이다. 6개가 아니다")
        self.assertEqual(S.TURNS_PER_CONVERSATION, 9, "깊음3·보통3·얕음3")

    def test_total_delay_is_78_seconds(self):
        self.assertEqual(S.TOTAL_DELAY_MS, 78000)
        self.assertEqual(
            S.TOTAL_DELAY_MS,
            3 * (S.DELAY_MS["deep"] + S.DELAY_MS["medium"] + S.DELAY_MS["shallow"]))

    def test_depth_delays_are_ordered(self):
        self.assertLess(S.DELAY_MS["shallow"], S.DELAY_MS["medium"])
        self.assertLess(S.DELAY_MS["medium"], S.DELAY_MS["deep"])


class TestParticipant(unittest.TestCase):
    def test_participant_number(self):
        self.assertEqual(S.participant_number("P01"), 1)
        self.assertEqual(S.participant_number("P07"), 7)
        self.assertEqual(S.participant_number("P12"), 12)
        with self.assertRaises(ValueError):
            S.participant_number("PXX")

    def test_session_plan_shape(self):
        plan = S.session_plan("P03")
        convs = plan["conversations"]
        self.assertEqual(len(convs), 3)
        self.assertEqual([c["index"] for c in convs], [1, 2, 3])
        self.assertEqual(sorted(c["condition"] for c in convs), sorted(S.CONDITIONS))
        for c in convs:
            self.assertEqual(c["turns"], 9)

    def test_condition_order_cycles_all_six_perms(self):
        seen = {tuple(S.condition_order(n)) for n in range(1, 7)}
        self.assertEqual(len(seen), 6, "참가자 6명이면 6순열이 모두 나와야 한다")

    def test_position_coverage_is_balanced(self):
        rep = S.coverage_report(12)
        for cond, counts in rep["position_counts"].items():
            self.assertEqual(counts, [4, 4, 4], f"{cond}이 세 위치에 고르게 배치되지 않았다")


class TestDepthSequence(unittest.TestCase):
    SID = "P01-1700000000000"

    def test_exactly_three_of_each_depth(self):
        for conv in (1, 2, 3):
            seq = S.depth_sequence(self.SID, conv)
            self.assertEqual(len(seq), 9)
            for d in S.DEPTHS:
                self.assertEqual(seq.count(d), 3, f"대화 {conv}의 {d}가 3개가 아니다")

    def test_deterministic(self):
        a = [S.depth_sequence(self.SID, 1) for _ in range(50)]
        self.assertEqual(len({tuple(x) for x in a}), 1)

    def test_differs_by_conversation_and_session(self):
        seqs = {tuple(S.depth_sequence(self.SID, c)) for c in (1, 2, 3)}
        self.assertGreater(len(seqs), 1, "대화마다 깊이 순서가 같으면 안 된다")
        other = S.depth_sequence("P02-1700000000000", 1)
        self.assertNotEqual(other, S.depth_sequence(self.SID, 1))

    def test_depth_order_is_identical_across_conditions(self):
        """★ 조건이 달라도 깊이 순서는 같아야 한다.

        그래야 R1과 R2의 차이가 '깊은 답에 무엇이 붙느냐'로만 남는다.
        depth_sequence는 조건을 인자로 받지도 않는다.
        """
        import inspect
        params = inspect.signature(S.depth_sequence).parameters
        self.assertNotIn("condition", params)


class TestDelayPlacement(unittest.TestCase):
    SID = "P01-1700000000000"

    def test_total_is_78s_in_every_condition(self):
        for cond in S.CONDITIONS:
            for conv in (1, 2, 3):
                total = sum(S.delay_sequence(self.SID, conv, cond))
                self.assertEqual(total, 78000, f"{cond} 대화{conv}의 총 지연이 78초가 아니다")

    def test_same_multiset_in_every_condition(self):
        want = sorted([15000] * 3 + [8000] * 3 + [3000] * 3)
        for cond in S.CONDITIONS:
            got = sorted(S.delay_sequence(self.SID, 1, cond))
            self.assertEqual(got, want, f"{cond}의 지연 다중집합이 다르다")

    def test_r1_puts_long_delay_on_deep(self):
        depths = S.depth_sequence(self.SID, 1)
        delays = S.delay_sequence(self.SID, 1, "R1")
        for d, ms in zip(depths, delays):
            self.assertEqual(ms, S.DELAY_MS[d], "R1은 깊이와 지연이 같은 방향이어야 한다")

    def test_r2_is_the_exact_reverse_of_r1(self):
        depths = S.depth_sequence(self.SID, 1)
        delays = S.delay_sequence(self.SID, 1, "R2")
        flip = {"deep": 3000, "medium": 8000, "shallow": 15000}
        for d, ms in zip(depths, delays):
            self.assertEqual(ms, flip[d], "R2는 깊은 답에 짧은 지연이 붙어야 한다")

    def test_r3_is_not_a_depth_mapping(self):
        """R3은 규칙이 없어야 한다 — 어떤 대화에서든 깊이↔지연이 일정하면 안 된다."""
        consistent = 0
        for conv in (1, 2, 3):
            depths = S.depth_sequence(self.SID, conv)
            delays = S.delay_sequence(self.SID, conv, "R3")
            mapping = {}
            ok = True
            for d, ms in zip(depths, delays):
                if mapping.setdefault(d, ms) != ms:
                    ok = False
            if ok:
                consistent += 1
        self.assertLess(consistent, 3, "R3이 세 대화 모두에서 규칙을 가지면 조건이 아니다")

    def test_unknown_condition_raises(self):
        with self.assertRaises(ValueError):
            S.delay_sequence(self.SID, 1, "R9")


class TestIndependenceFromText(unittest.TestCase):
    """★ CONTRACT P1 — 지연은 사용자 입력의 함수가 아니다."""
    SID = "P01-1700000000000"

    def test_draw_delay_takes_no_text_argument(self):
        import inspect
        params = list(inspect.signature(S.draw_delay_ms).parameters)
        for bad in ("text", "message", "user_input", "chars", "length"):
            self.assertNotIn(bad, params)

    def test_turn_plan_takes_no_text_argument(self):
        import inspect
        params = list(inspect.signature(S.turn_plan).parameters)
        for bad in ("text", "message", "user_input", "chars", "length"):
            self.assertNotIn(bad, params)

    def test_identical_across_many_calls(self):
        vals = {S.draw_delay_ms(self.SID, 2, 5, "R1") for _ in range(1000)}
        self.assertEqual(len(vals), 1)

    def test_seed_formula_is_pinned(self):
        """시드 공식이 바뀌면 로그의 session_id로 D를 재현할 수 없다."""
        import hashlib
        for conv in (1, 2, 3):
            for turn in range(1, 10):
                seed = f"{self.SID}|{conv}|{turn}".encode("utf-8")
                want = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big") / 2 ** 64
                self.assertAlmostEqual(S._rand(self.SID, conv, turn), want, places=12)


class TestTurnPlan(unittest.TestCase):
    SID = "P01-1700000000000"

    def test_turn_plan_matches_sequences(self):
        for cond in S.CONDITIONS:
            depths = S.depth_sequence(self.SID, 1)
            delays = S.delay_sequence(self.SID, 1, cond)
            for i in range(9):
                tp = S.turn_plan(self.SID, 1, cond, i + 1)
                self.assertEqual(tp["depth"], depths[i])
                self.assertEqual(tp["target_delay_ms"], delays[i])

    def test_out_of_range_turn_raises(self):
        for bad in (0, 10, 99):
            with self.assertRaises(ValueError):
                S.turn_plan(self.SID, 1, "R1", bad)

    def test_practice_is_fixed_and_condition_independent(self):
        vals = {S.turn_plan(self.SID, 0, c, 1)["target_delay_ms"] for c in S.CONDITIONS}
        self.assertEqual(vals, {S.PRACTICE_DELAY_MS})

    def test_practice_delay_is_the_middle_level(self):
        """연습이 가장 빠르면 이후 모든 조건이 그보다 느리게 느껴진다 (앵커링)."""
        self.assertEqual(S.PRACTICE_DELAY_MS, S.DELAY_MS["medium"])

    def test_scaled_placement_is_honoured(self):
        placement = {"deep_ms": 1500, "medium_ms": 800, "shallow_ms": 300, "practice_ms": 800}
        total = sum(S.delay_sequence(self.SID, 1, "R1", placement))
        self.assertEqual(total, 3 * (1500 + 800 + 300))


if __name__ == "__main__":
    unittest.main()
