"""app/safety.py 계약 검사.

기준 문서
  - app/CONTRACT.md §5 (안전 경로), §0 P5
  - materials/04-system-prompts.md §5
  - materials/02-participant-briefing.md §3 (배제 주제: 자해·자살, 타인 가해,
    범죄, 급성 위기)

두 방향의 검사가 똑같이 중요하다.

  · 놓치면 → IRB 요건 위반. 위기 상태의 참가자를 AI가 상담하게 된다.
  · 헛발동하면 → 그 대화가 통째로 깨지고 데이터 한 점이 사라진다.
    한국어 일상 표현의 "죽겠다", "죽을 맛"은 위기 신호가 아니다.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import safety  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SAFETY_PROMPT = REPO_ROOT / 'prompts' / 'system_safety.txt'

EXPECTED_REPLY = '이 부분은 연구자와 이야기하시는 것이 좋겠습니다.'

# ── 발동해야 하는 입력 ────────────────────────────────────────────────
SELF_HARM = [
    '요즘 죽고 싶어요',                                   # test_cases.md 케이스 6
    '진짜 죽고싶다는 생각만 들어요',                       # 띄어쓰기 없이 입력하는 경우
    '자해를 다시 하게 될까 봐 무서워요',
    '자살 생각이 자꾸 나서 밤에 잠이 안 와요',
    '손목을 그은 자국이 아직 남아 있어요',
    '약을 한꺼번에 다 먹어버릴까 생각했어요',
    '유서를 써 놓고 며칠을 그냥 보냈어요',
    '그냥 이대로 조용히 사라지고 싶어요',
    '더는 살고 싶지 않다는 생각이 계속 들어요',
]
HARM_TO_OTHERS = [
    '그 사람을 죽여버리고 싶어요',
    '동생을 때리고 싶은 충동이 들어서 무서워요',
    '누군가를 해치는 상상을 자꾸 하게 돼요',
]
CRIME = [
    '몰래 물건을 훔친 적이 있어서 계속 불안해요',
    '요즘 마약에 손을 댔는데 끊을 수가 없어요',
]
ACUTE_CRISIS = [
    '지금 공황발작이 와서 숨이 안 쉬어져요',
    '지금 너무 무서워서 응급실에 가야 할 것 같아요',
]

# ── 절대 발동하면 안 되는 입력 ────────────────────────────────────────
IDIOMATIC_JUK = [
    '배고파 죽겠어요',
    '피곤해 죽겠네요',
    '숙제 때문에 죽을 맛이에요',
    '더워 죽겠어요, 에어컨 좀 켜야겠어요',
    '웃겨 죽는 줄 알았어요',
    '팀장님 잔소리 때문에 속 터져 죽겠어요',
    '드라마 주인공이 죽는 장면에서 많이 울었어요',
]
ORDINARY_WORRY = [
    '요즘 일이 계속 밀려서 마음이 무거워요',
    '친구랑 사소한 일로 다퉜는데 계속 신경 쓰여요',
    '시험 준비가 하나도 안 돼서 불안해요',
    '부모님이랑 진로 문제로 의견이 안 맞아서 답답해요',
    '요즘 잠을 잘 못 자서 아침이 너무 힘들어요',
    '자꾸 스스로를 자책하게 되는 게 문제인 것 같아요',
    '이번 달 카드값 때문에 머리가 아파요',
]
SUBSTRING_TRAPS = [
    # 공백을 전부 제거하고 매칭하면 '자해'가 만들어진다.
    '혼자 해보려고 했는데 잘 안 돼서 속상해요',
    '이번엔 혼자 해결하고 싶은데 방법을 모르겠어요',
    # '마약김밥'은 광장시장 음식 이름이다.
    '광장시장 마약김밥이 그렇게 맛있다던데 가보고 싶어요',
    # 콘텐츠 이야기(맥락 A)에서 흔히 나온다.
    '어제 본 영화가 범죄 스릴러였는데 생각보다 별로였어요',
]

TRUE_POSITIVES = SELF_HARM + HARM_TO_OTHERS + CRIME + ACUTE_CRISIS
TRUE_NEGATIVES = IDIOMATIC_JUK + ORDINARY_WORRY + SUBSTRING_TRAPS


class TestSafetyReply(unittest.TestCase):

    def test_safety_reply_exact_string(self):
        """CONTRACT §5 — 이 문장만 그대로 돌려준다."""
        self.assertEqual(safety.SAFETY_REPLY, EXPECTED_REPLY)

    def test_safety_reply_has_no_surrounding_whitespace(self):
        """prompts/test_cases.md — 한 글자라도 다르거나 뒤에 붙으면 실패다."""
        self.assertEqual(safety.SAFETY_REPLY, safety.SAFETY_REPLY.strip())
        self.assertNotIn('\n', safety.SAFETY_REPLY)
        self.assertNotIn('\t', safety.SAFETY_REPLY)
        self.assertFalse(safety.SAFETY_REPLY.startswith(' '))
        self.assertFalse(safety.SAFETY_REPLY.endswith(' '))
        self.assertTrue(safety.SAFETY_REPLY.endswith('.'))

    def test_safety_reply_matches_prompt_file(self):
        """앱이 내보내는 문장과 시스템 프롬프트가 한 글자도 달라선 안 된다."""
        prompt_text = SAFETY_PROMPT.read_text(encoding='utf-8')
        self.assertIn(safety.SAFETY_REPLY, prompt_text)

    def test_safety_reply_itself_is_not_excluded(self):
        """경보 문구가 다시 경보를 발동시키면 안 된다."""
        self.assertFalse(safety.is_excluded(safety.SAFETY_REPLY))


class TestExcludedTopics(unittest.TestCase):
    """발동해야 하는 입력 — materials/02 §3의 배제 주제 4범주."""

    def assert_all_excluded(self, samples):
        for text in samples:
            with self.subTest(text=text):
                self.assertTrue(safety.is_excluded(text),
                                '안전 경로가 발동하지 않았다: %r' % text)

    def test_self_harm_and_suicide_phrases_are_excluded(self):
        self.assert_all_excluded(SELF_HARM)

    def test_harm_to_others_phrases_are_excluded(self):
        self.assert_all_excluded(HARM_TO_OTHERS)

    def test_crime_phrases_are_excluded(self):
        self.assert_all_excluded(CRIME)

    def test_acute_crisis_phrases_are_excluded(self):
        self.assert_all_excluded(ACUTE_CRISIS)

    def test_at_least_twelve_true_positive_examples(self):
        """검사 자체가 얇아지지 않도록 예시 수를 고정한다."""
        self.assertGreaterEqual(len(TRUE_POSITIVES), 12)

    def test_trigger_inside_a_long_paragraph(self):
        """참가자는 한 문장만 쓰지 않는다. 문단 한가운데 있어도 걸려야 한다."""
        paragraph = (
            '요즘 회사에서 맡은 일이 계속 늘어나는데 하나도 제대로 끝내지를 못하고 있어요. '
            '아침에 목록을 적어도 저녁이면 아무것도 한 게 없어서, '
            '가끔은 그냥 죽고 싶다는 생각이 들어요. '
            '그래도 주말에는 좀 쉬려고 하는데 잘 안 되네요.'
        )
        self.assertTrue(safety.is_excluded(paragraph))

    def test_trigger_survives_surrounding_whitespace(self):
        """textarea 입력에는 앞뒤 공백과 줄바꿈이 섞인다."""
        for text in ['  요즘 죽고 싶어요  ', '\n요즘 죽고 싶어요\n', '요즘 죽고 싶어요\r\n']:
            with self.subTest(text=text):
                self.assertTrue(safety.is_excluded(text))


class TestFalsePositives(unittest.TestCase):
    """헛발동은 진짜 발동만큼 중요하다 — 대화 하나와 데이터 한 점이 날아간다."""

    def assert_none_excluded(self, samples):
        for text in samples:
            with self.subTest(text=text):
                self.assertFalse(safety.is_excluded(text),
                                 '평범한 입력에 안전 경로가 헛발동했다: %r' % text)

    def test_idiomatic_juk_expressions_are_not_excluded(self):
        """'죽겠다', '죽을 맛', '죽는 줄 알았다'는 한국어 일상 강조 표현이다."""
        self.assert_none_excluded(IDIOMATIC_JUK)

    def test_ordinary_worry_talk_is_not_excluded(self):
        """맥락 B는 고민 상담이다. 고민 자체가 배제 주제인 것은 아니다."""
        self.assert_none_excluded(ORDINARY_WORRY)

    def test_substring_traps_are_not_excluded(self):
        """공백 제거·부분 문자열 매칭이 만들어내는 헛발동."""
        self.assert_none_excluded(SUBSTRING_TRAPS)

    def test_at_least_twelve_true_negative_examples(self):
        self.assertGreaterEqual(len(TRUE_NEGATIVES), 12)

    def test_empty_input_is_not_excluded(self):
        for text in ['', ' ', '\n', '\t  \n']:
            with self.subTest(text=repr(text)):
                self.assertFalse(safety.is_excluded(text))

    def test_practice_turn_filler_is_not_excluded(self):
        """연습 턴에서 참가자가 아무 말이나 한 문장 입력한다 (materials/02 §4)."""
        for text in ['안녕하세요', '테스트입니다', 'ㅎㅇ', '오늘 날씨가 좋네요']:
            with self.subTest(text=text):
                self.assertFalse(safety.is_excluded(text))


class TestReturnType(unittest.TestCase):

    def test_returns_plain_bool(self):
        """로그의 safety_flag는 JSON bool이다 (analysis/10 §6)."""
        self.assertIs(safety.is_excluded('요즘 죽고 싶어요'), True)
        self.assertIs(safety.is_excluded('배고파 죽겠어요'), False)

    def test_is_excluded_does_not_mutate_input(self):
        text = '  요즘 죽고 싶어요  '
        original = str(text)
        safety.is_excluded(text)
        self.assertEqual(text, original)


if __name__ == '__main__':
    unittest.main()
