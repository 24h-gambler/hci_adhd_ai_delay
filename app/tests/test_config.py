"""config.py — prompts.yaml 파서와 설계 불변식."""
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
import config as C  # noqa: E402

RAW = (ROOT / "prompts" / "prompts.yaml").read_text(encoding="utf-8")


class TestParsing(unittest.TestCase):
    def setUp(self):
        self.cfg = C.load_config()

    def test_required_keys(self):
        for k in ("version", "model", "conversation", "delay_placement", "composition"):
            self.assertIn(k, self.cfg)

    def test_no_yaml_library_is_used(self):
        src = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
        self.assertNotIn("import yaml", src)
        self.assertNotIn("yaml.safe_load", src)

    def test_values_match_the_raw_file(self):
        for key in ("deep_ms", "medium_ms", "shallow_ms", "practice_ms"):
            m = re.search(rf"^\s*{key}:\s*(\d+)", RAW, re.M)
            self.assertIsNotNone(m, f"{key}가 원본 파일에 없다")
            self.assertEqual(self.cfg["delay_placement"][key], int(m.group(1)))


class TestDesignInvariants(unittest.TestCase):
    def setUp(self):
        self.cfg = C.load_config()

    def test_streaming_is_off(self):
        """CONTRACT P4 — 스트리밍을 켜면 목표 시각 표시가 무의미해진다."""
        self.assertIs(self.cfg["model"]["stream"], False)

    def test_indicator_is_none(self):
        """화면 표시를 넣으면 그게 해석 단서가 되어 질문이 사라진다."""
        self.assertEqual(self.cfg["indicator"], "none")

    def test_three_conversations_nine_turns(self):
        conv = self.cfg["conversation"]
        self.assertEqual(conv["conversations"], 3)
        self.assertEqual(conv["turns_per_conversation"], 9)

    def test_history_resets_between_conversations(self):
        self.assertIs(self.cfg["conversation"]["reset_history_between_conversations"], True)

    def test_depth_delays_are_strictly_ordered(self):
        dp = self.cfg["delay_placement"]
        self.assertLess(dp["shallow_ms"], dp["medium_ms"])
        self.assertLess(dp["medium_ms"], dp["deep_ms"])

    def test_total_delay_arithmetic(self):
        """★ 세 조건의 총 지연이 같다는 전제가 산술로 성립해야 한다."""
        dp = self.cfg["delay_placement"]
        self.assertEqual(
            dp["total_per_conversation_ms"],
            3 * (dp["deep_ms"] + dp["medium_ms"] + dp["shallow_ms"]))
        self.assertEqual(dp["total_per_conversation_ms"], 78000)

    def test_practice_is_the_middle_delay(self):
        dp = self.cfg["delay_placement"]
        self.assertEqual(dp["practice_ms"], dp["medium_ms"])


class TestValidationRejectsBrokenConfig(unittest.TestCase):
    def _write(self, tmp, text):
        p = pathlib.Path(tmp) / "prompts.yaml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_bad_arithmetic_is_refused(self):
        import tempfile
        broken = RAW.replace("total_per_conversation_ms: 78000",
                             "total_per_conversation_ms: 60000")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as cm:
                C.load_config(self._write(tmp, broken))
            self.assertIn("총 지연", str(cm.exception))

    def test_streaming_on_is_refused(self):
        import tempfile
        broken = RAW.replace("stream: false", "stream: true")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                C.load_config(self._write(tmp, broken))

    def test_indicator_other_than_none_is_refused(self):
        import tempfile
        broken = RAW.replace("indicator: none", "indicator: dots")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                C.load_config(self._write(tmp, broken))

    def test_wrong_turn_count_is_refused(self):
        import tempfile
        broken = RAW.replace("turns_per_conversation: 9", "turns_per_conversation: 5")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                C.load_config(self._write(tmp, broken))


class TestScaling(unittest.TestCase):
    def setUp(self):
        self.cfg = C.load_config()

    def test_scale_one_is_identity(self):
        self.assertEqual(C.scaled_delay_placement(self.cfg, 1.0)["deep_ms"],
                         self.cfg["delay_placement"]["deep_ms"])

    def test_scaling_keeps_the_arithmetic(self):
        dp = C.scaled_delay_placement(self.cfg, 0.1)
        self.assertEqual(dp["total_per_conversation_ms"],
                         3 * (dp["deep_ms"] + dp["medium_ms"] + dp["shallow_ms"]))

    def test_scaling_keeps_the_three_levels_distinct(self):
        dp = C.scaled_delay_placement(self.cfg, 0.1)
        self.assertLess(dp["shallow_ms"], dp["medium_ms"])
        self.assertLess(dp["medium_ms"], dp["deep_ms"])

    def test_degenerate_scale_is_refused(self):
        """배율이 너무 작으면 세 깊이가 뭉개진다. 조용히 통과시키면 안 된다."""
        with self.assertRaises(ValueError):
            C.scaled_delay_placement(self.cfg, 0.0001)


if __name__ == "__main__":
    unittest.main()
