"""app/config.py 계약 검사.

기준 문서
  - app/CONTRACT.md §0 P4(스트리밍 금지), §1(표준 라이브러리만), §2
  - materials/04-system-prompts.md §6(API 설정), §7(대화 이력)
  - materials/03-topic-cards.md §4(대화 1개 = 5턴)
  - analysis/10-manipulation-check-plan.md §2(조건별 지연 범위)

prompts.yaml은 실험의 버전 고정 지점이다. 파서가 조용히 기본값을 돌려주면
전혀 다른 지연으로 실험이 돌아가고도 아무도 모른다.
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import config  # noqa: E402

APP_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parent
CONFIG_PATH = REPO_ROOT / 'prompts' / 'prompts.yaml'

CONDITIONS = ['immediate', 'medium', 'long']


def load():
    return config.load_config(str(CONFIG_PATH))


class TestLoaderIsStandardLibraryOnly(unittest.TestCase):

    def test_load_config_returns_required_keys(self):
        cfg = load()
        for key in ('version', 'empathy_variant', 'model', 'conversation', 'delay_conditions'):
            with self.subTest(key=key):
                self.assertIn(key, cfg)
        self.assertIsInstance(cfg['model'], dict)
        self.assertIsInstance(cfg['conversation'], dict)
        self.assertIsInstance(cfg['delay_conditions'], dict)

    def test_load_config_does_not_use_pyyaml(self):
        """CONTRACT §1 — 외부 의존성 없음."""
        load()
        self.assertNotIn('yaml', sys.modules)
        self.assertNotIn('ruamel', sys.modules)

    def test_load_config_works_with_yaml_import_blocked(self):
        """PyYAML이 설치된 기계에서도 그것을 쓰지 않는다는 것을 증명한다."""
        code = textwrap.dedent('''
            import sys, json

            class _BlockYaml:
                def find_spec(self, name, path=None, target=None):
                    if name.split('.')[0] in ('yaml', 'ruamel', 'pyyaml', 'oyaml'):
                        raise ImportError('third-party YAML is banned: ' + name)
                    return None

            sys.meta_path.insert(0, _BlockYaml())
            sys.path.insert(0, %r)
            import config
            cfg = config.load_config(%r)
            print(json.dumps({
                "version": cfg["version"],
                "stream": cfg["model"]["stream"],
                "turns": cfg["conversation"]["turns_per_conversation"],
            }))
        ''') % (str(APP_DIR), str(CONFIG_PATH))
        proc = subprocess.run([sys.executable, '-c', code],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        cfg = load()
        self.assertEqual(out['version'], cfg['version'])
        self.assertIs(out['stream'], False)
        self.assertEqual(out['turns'], 5)

    def test_default_path_is_prompts_prompts_yaml(self):
        """서명 기본값 path='prompts/prompts.yaml' — 저장소 루트 기준."""
        cwd = os.getcwd()
        try:
            os.chdir(str(REPO_ROOT))
            self.assertEqual(config.load_config(), load())
        finally:
            os.chdir(cwd)

    def test_missing_file_raises(self):
        """경로가 틀리면 조용히 기본값을 돌려주는 대신 터져야 한다."""
        missing = str(REPO_ROOT / 'prompts' / 'no_such_file.yaml')
        with self.assertRaises(FileNotFoundError):
            config.load_config(missing)


class TestParsedValuesMatchRawFile(unittest.TestCase):
    """파서가 파일을 실제로 읽는지 — 리터럴을 정규식으로 긁어 대조한다."""

    @classmethod
    def setUpClass(cls):
        cls.raw = CONFIG_PATH.read_text(encoding='utf-8')
        cls.cfg = load()

    def test_version_matches_raw_file(self):
        m = re.search(r'^version:\s*"([^"]+)"', self.raw, re.M)
        self.assertIsNotNone(m, 'prompts.yaml에 version이 없다')
        self.assertEqual(self.cfg['version'], m.group(1))
        self.assertRegex(self.cfg['version'], r'^v\d+\.\d+$')

    def test_empathy_variant_matches_raw_file(self):
        m = re.search(r'^empathy_variant:\s*([ABC])\b', self.raw, re.M)
        self.assertIsNotNone(m)
        self.assertEqual(self.cfg['empathy_variant'], m.group(1))

    def test_delay_ranges_match_raw_file(self):
        for name in CONDITIONS:
            with self.subTest(condition=name):
                m = re.search(
                    r'^\s*%s:\s*\{\s*min_ms:\s*(\d+)\s*,\s*max_ms:\s*(\d+)\s*\}' % name,
                    self.raw, re.M)
                self.assertIsNotNone(m, '%s 범위를 파일에서 찾지 못했다' % name)
                self.assertEqual(self.cfg['delay_conditions'][name]['min_ms'], int(m.group(1)))
                self.assertEqual(self.cfg['delay_conditions'][name]['max_ms'], int(m.group(2)))

    def test_practice_fixed_ms_matches_raw_file(self):
        m = re.search(r'^\s*practice:\s*\{\s*fixed_ms:\s*(\d+)\s*\}', self.raw, re.M)
        self.assertIsNotNone(m)
        self.assertEqual(self.cfg['delay_conditions']['practice']['fixed_ms'], int(m.group(1)))


class TestDelayConditions(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.ranges = load()['delay_conditions']

    def test_all_conditions_present(self):
        for name in CONDITIONS + ['practice']:
            with self.subTest(condition=name):
                self.assertIn(name, self.ranges)

    def test_ranges_are_ordered_and_integer_ms(self):
        for name in CONDITIONS:
            with self.subTest(condition=name):
                lo = self.ranges[name]['min_ms']
                hi = self.ranges[name]['max_ms']
                self.assertIs(type(lo), int, '%s.min_ms가 정수 ms가 아니다' % name)
                self.assertIs(type(hi), int, '%s.max_ms가 정수 ms가 아니다' % name)
                self.assertGreater(lo, 0)
                self.assertLess(lo, hi, '%s: min_ms < max_ms 여야 한다' % name)

    def test_ranges_do_not_overlap(self):
        """세 조건이 겹치면 조건 간 대비가 무너지고 조작 자체가 성립하지 않는다."""
        bands = sorted((self.ranges[n]['min_ms'], self.ranges[n]['max_ms'], n)
                       for n in CONDITIONS)
        for (lo1, hi1, n1), (lo2, hi2, n2) in zip(bands, bands[1:]):
            with self.subTest(pair=(n1, n2)):
                self.assertLess(hi1, lo2,
                                '%s(%d~%d)와 %s(%d~%d)의 범위가 겹친다'
                                % (n1, lo1, hi1, n2, lo2, hi2))

    def test_conditions_are_monotone_immediate_medium_long(self):
        """docs/00 §1 — 즉시 < 중간 < 긺."""
        self.assertLess(self.ranges['immediate']['max_ms'], self.ranges['medium']['min_ms'])
        self.assertLess(self.ranges['medium']['max_ms'], self.ranges['long']['min_ms'])

    def test_practice_is_fixed_and_outside_every_condition(self):
        """CONTRACT §2("연습 턴은 800") · materials/02 §4("즉시 조건 범위도 아니다")."""
        fixed = self.ranges['practice']['fixed_ms']
        self.assertIs(type(fixed), int)
        self.assertEqual(fixed, 800)
        for name in CONDITIONS:
            with self.subTest(condition=name):
                self.assertFalse(
                    self.ranges[name]['min_ms'] <= fixed <= self.ranges[name]['max_ms'],
                    '연습 고정 지연이 %s 범위 안에 있다' % name)

    def test_practice_has_no_min_max(self):
        """연습은 무작위 추출이 아니라 고정값이다."""
        self.assertNotIn('min_ms', self.ranges['practice'])
        self.assertNotIn('max_ms', self.ranges['practice'])


class TestModelSettings(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.model = load()['model']

    def test_model_stream_is_false(self):
        """★ P4 — 스트리밍 금지. 문자열 'false'가 아니라 bool False여야 한다."""
        self.assertIn('stream', self.model)
        self.assertIs(self.model['stream'], False)

    def test_model_id_is_a_string(self):
        self.assertIn('id', self.model)
        self.assertIsInstance(self.model['id'], str)
        self.assertNotEqual(self.model['id'].strip(), '')

    def test_temperature_is_within_documented_range(self):
        """materials/04 §6 — 0.6 (지정 범위 0.5~0.7)."""
        temperature = self.model['temperature']
        self.assertIsInstance(temperature, float)
        self.assertGreaterEqual(temperature, 0.5)
        self.assertLessEqual(temperature, 0.7)

    def test_top_p_is_one(self):
        """materials/04 §6 — top_p 1.0 고정, temperature만 조절한다."""
        self.assertEqual(self.model['top_p'], 1.0)
        self.assertIsInstance(self.model['top_p'], float)

    def test_max_tokens_is_a_positive_int(self):
        self.assertIs(type(self.model['max_tokens']), int)
        self.assertGreaterEqual(self.model['max_tokens'], 200)


class TestConversationSettings(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conversation = load()['conversation']

    def test_turns_per_conversation_is_five(self):
        """materials/03 §4 — 대화 1개 = 5턴 고정."""
        self.assertIs(type(self.conversation['turns_per_conversation']), int)
        self.assertEqual(self.conversation['turns_per_conversation'], 5)

    def test_block_structure_matches_design(self):
        """docs/00 §1 — 대화 6개 = 3(지연) × 2(맥락), 분석 가능 턴 30개."""
        conv = self.conversation
        self.assertEqual(conv['conversations_per_block'], 3)
        self.assertEqual(conv['blocks'], 2)
        self.assertEqual(conv['conversations_per_block'] * conv['blocks'], 6)
        self.assertEqual(
            conv['turns_per_conversation'] * conv['conversations_per_block'] * conv['blocks'],
            30)

    def test_reset_history_between_conversations_is_true(self):
        """P6 — 대화 6개는 각각 독립. bool True여야 한다."""
        self.assertIs(self.conversation['reset_history_between_conversations'], True)


class TestEmpathyVariant(unittest.TestCase):

    def test_empathy_variant_is_a_b_or_c(self):
        """materials/04 §4 — A/B/C 중 하나. 로그와 논문 방법 절에 그대로 들어간다."""
        variant = load()['empathy_variant']
        self.assertIsInstance(variant, str)
        self.assertIn(variant, ('A', 'B', 'C'))


class TestScalarParsing(unittest.TestCase):
    """prompts.yaml에 실제로 등장하는 문법만 골라 파서를 검사한다."""

    SAMPLE = textwrap.dedent('''\
        # 맨 위 주석
        version: "v9.9"
        status: draft          # draft | pilot | locked
        locked_at: null        # 아직 안 잠금
        enabled: true
        stream: false
        empathy_variant: C     # A | B | C

        composition:
          # 중첩 주석
          context_a:
            - system_common.txt
            - system_safety.txt

        model:
          id: "TBD"
          temperature: 0.6     # 지정 범위 0.5~0.7
          top_p: 1.0
          max_tokens: 200
          seed: 20260401
          stream: false

        conversation:
          turns_per_conversation: 5
          conversations_per_block: 3
          blocks: 2
          reset_history_between_conversations: true

        delay_conditions:
          immediate: { min_ms: 1000,  max_ms:  2000 }
          medium:    { min_ms: 8000,  max_ms:  9000 }
          long:      { min_ms: 16000, max_ms: 20000 }
          practice:  { fixed_ms: 800 }
        ''')

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = pathlib.Path(cls.tmp.name) / 'sample.yaml'
        path.write_text(cls.SAMPLE, encoding='utf-8')
        cls.cfg = config.load_config(str(path))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_booleans_and_null_become_python_objects(self):
        self.assertIs(self.cfg['enabled'], True)
        self.assertIs(self.cfg['stream'], False)
        self.assertIsNone(self.cfg['locked_at'])

    def test_quoted_and_bare_strings(self):
        self.assertEqual(self.cfg['version'], 'v9.9')
        self.assertEqual(self.cfg['status'], 'draft')
        self.assertEqual(self.cfg['empathy_variant'], 'C')
        self.assertEqual(self.cfg['model']['id'], 'TBD')

    def test_numbers_keep_their_type(self):
        model = self.cfg['model']
        self.assertIs(type(model['max_tokens']), int)
        self.assertEqual(model['max_tokens'], 200)
        self.assertIs(type(model['seed']), int)
        self.assertEqual(model['seed'], 20260401)
        self.assertIsInstance(model['temperature'], float)
        self.assertAlmostEqual(model['temperature'], 0.6)
        self.assertIsInstance(model['top_p'], float)
        self.assertAlmostEqual(model['top_p'], 1.0)

    def test_trailing_comments_are_stripped(self):
        """'draft          # draft | pilot | locked'가 통째로 값이 되면 안 된다."""
        self.assertEqual(self.cfg['status'], 'draft')
        self.assertNotIn('#', str(self.cfg['status']))
        self.assertNotIn('#', str(self.cfg['empathy_variant']))

    def test_block_sequences_become_lists(self):
        self.assertEqual(self.cfg['composition']['context_a'],
                         ['system_common.txt', 'system_safety.txt'])

    def test_inline_flow_maps_become_dicts(self):
        self.assertEqual(self.cfg['delay_conditions']['immediate'],
                         {'min_ms': 1000, 'max_ms': 2000})
        self.assertEqual(self.cfg['delay_conditions']['practice'], {'fixed_ms': 800})


if __name__ == '__main__':
    unittest.main()
