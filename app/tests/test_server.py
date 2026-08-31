"""app/server.py 계약 검사 (HTTP 계층).

기준 문서
  - app/CONTRACT.md  §0(P1~P8), §2(HTTP API), §3(로그 스키마), §5(안전 경로), §6(대화 이력)
  - analysis/10-manipulation-check-plan.md §6 (로그 스키마 — manipulation_check.py가 그대로 읽는다)
  - materials/04-system-prompts.md §5(안전), §6(스트리밍 금지), §7(대화 이력)

이 파일이 기준이다. 구현이 여기와 어긋나면 구현이 틀린 것이다.

실제 서버를 임시 포트에 in-process로 띄우고 urllib로 두드린다.
로그 디렉터리는 검사마다 새 임시 디렉터리를 쓴다 (참가자 데이터가 섞이지 않도록).

★ 지연 배율(delay_scale)
  검사는 초 단위 지연을 실제로 기다릴 이유가 없다. 세 조건과 연습 지연에
  **같은 배율**을 적용하는 서버 옵션(--delay-scale)을 써서 100배 줄인다.
  배율이 세 조건에 똑같이 적용되는지 자체를 검사한다
  (test_delay_scale_scales_every_condition_equally).
"""

import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import config as cfgmod  # noqa: E402
import safety  # noqa: E402
import schedule  # noqa: E402
import server as srv  # noqa: E402

APP_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parent

# 검사용 배율. 1/100이면 긺 조건도 160~200ms라 전체 검사가 몇 초 안에 끝난다.
TEST_SCALE = 0.01

# analysis/10-manipulation-check-plan.md §6 — 필드와 형. 하나라도 빠지면
# manipulation_check.py가 그 줄을 통째로 버린다.
LOG_SCHEMA = [
    ("participant_id", str),
    ("group", str),
    ("block", int),
    ("conversation_index", int),
    ("condition", str),
    ("context", str),
    ("turn_index", int),
    ("practice", bool),
    ("user_input_start_ts", int),
    ("user_input_submit_ts", int),
    ("user_input_text", str),
    ("user_input_chars", int),
    ("target_delay_ms", int),
    ("llm_request_ts", int),
    ("llm_response_ts", int),
    ("display_ts", int),
    ("ai_response_text", str),
    ("ai_response_chars", int),
    ("finish_reason", str),
    ("next_input_start_ts", (int, type(None))),
    ("safety_flag", bool),
    ("manipulation_ok", bool),
    ("prompt_version", str),
    ("prompt_sha256", str),
    ("model", str),
    ("empathy_variant", str),
    ("temperature", (int, float)),
    ("max_tokens", int),
]

# CONTRACT §2 — /api/session/start 응답
SESSION_START_KEYS = [
    "session_id", "participant_number", "block_order", "conversations",
    "turns_per_conversation", "empathy_variant", "prompt_version",
    "prompt_sha256", "model", "temperature", "max_tokens",
]

BENIGN = [
    "어제 본 다큐멘터리가 생각보다 길었는데 끝까지 봤어요.",
    "주말에 친구랑 전시를 보러 갔다가 근처 서점에도 들렀습니다.",
    "요즘은 저녁에 산책을 하면서 팟캐스트를 듣는 편이에요.",
    "회사 일정이 몰려서 이번 주는 정리가 잘 안 되는 느낌입니다.",
]

# materials/04 §8 테스트 케이스 6 — 안전 경로가 반드시 발동해야 하는 입력
SAFETY_INPUT = "요즘 죽고 싶다는 생각이 자주 듭니다."


def _no_bool(value):
    """JSON에서 True는 int의 부분형이다. bool을 int로 오인하지 않게 걸러낸다."""
    return not isinstance(value, bool)


class ServerCase(unittest.TestCase):
    """서버 하나를 임시 로그 디렉터리와 함께 띄우는 검사 기반 클래스."""

    latency_mode = "length"
    delay_scale = TEST_SCALE

    def setUp(self):
        self.cfg = cfgmod.load_config()
        self.ranges = self.cfg["delay_conditions"]
        self.start_server()

    def start_server(self, latency_mode=None, delay_scale=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log_dir = pathlib.Path(tmp.name)
        self.scale = self.delay_scale if delay_scale is None else delay_scale
        self.httpd = srv.build_server(
            port=0,
            provider="mock",
            log_dir=str(self.log_dir),
            latency_mode=latency_mode or self.latency_mode,
            delay_scale=self.scale,
        )
        self.exp = self.httpd.exp
        self.provider = self.exp.provider
        thread = threading.Thread(target=self.httpd.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()

        def stop():
            self.httpd.shutdown()
            thread.join(timeout=5)
            self.httpd.server_close()

        self.addCleanup(stop)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    # ── HTTP ──
    def post(self, path, body, expect=200):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status, raw, headers = r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            status, raw, headers = e.code, e.read(), dict(e.headers)
        self.assertEqual(status, expect, raw[:400])
        self.last_headers = headers
        self.last_raw = raw
        return json.loads(raw.decode("utf-8"))

    def get(self, path, expect=200):
        try:
            with urllib.request.urlopen(self.base + path, timeout=30) as r:
                status, raw, headers = r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            status, raw, headers = e.code, e.read(), dict(e.headers)
        self.assertEqual(status, expect, raw[:400])
        self.last_headers = headers
        self.last_raw = raw
        return raw

    # ── 편의 ──
    def new_session(self, participant_id="P01", group="adhd"):
        return self.post("/api/session/start",
                         {"participant_id": participant_id, "group": group})

    def conversation_with(self, session, condition):
        for c in session["conversations"]:
            if c["condition"] == condition:
                return c
        self.fail("계획에 %r 조건 대화가 없습니다" % condition)

    def send_turn(self, session, conversation_index, turn_index, text,
                  submit_ts=None, start_ts=None):
        submit = int(time.time() * 1000) if submit_ts is None else submit_ts
        return self.post("/api/turn", {
            "session_id": session["session_id"],
            "conversation_index": conversation_index,
            "turn_index": turn_index,
            "text": text,
            "user_input_start_ts": submit - 1500 if start_ts is None else start_ts,
            "user_input_submit_ts": submit,
        })

    def display(self, turn, at_deadline=False):
        if at_deadline:
            remaining = turn["deadline_ts"] / 1000.0 - time.time()
            if remaining > 0:
                time.sleep(remaining)
        return self.post("/api/turn/display",
                         {"turn_id": turn["turn_id"],
                          "display_ts": int(time.time() * 1000)})

    def turn_and_display(self, session, conv_index, turn_index, text, **kw):
        turn = self.send_turn(session, conv_index, turn_index, text, **kw)
        self.display(turn)
        return turn

    def log_rows(self, session):
        path = self.log_dir / ("%s.turns.jsonl" % session["session_id"])
        self.assertTrue(path.is_file(), "로그 파일이 없습니다: %s" % path)
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def row_for(self, session, conv_index, turn_index):
        for row in self.log_rows(session):
            if row["conversation_index"] == conv_index and row["turn_index"] == turn_index:
                return row
        self.fail("로그에 대화 %s 턴 %s이 없습니다" % (conv_index, turn_index))

    def scaled(self, condition):
        r = self.ranges[condition]
        return round(r["min_ms"] * self.scale), round(r["max_ms"] * self.scale)


# ══════════════════════════════════════════════════════════════════
# CONTRACT §2 — 세션 시작
# ══════════════════════════════════════════════════════════════════

class SessionStartTest(ServerCase):

    def test_session_start_payload_matches_contract(self):
        s = self.new_session("P07", "comparison")
        for key in SESSION_START_KEYS:
            self.assertIn(key, s, "CONTRACT §2 응답에 %r이 없습니다" % key)

        self.assertTrue(s["session_id"].startswith("P07-"), s["session_id"])
        self.assertEqual(s["participant_number"], 7)
        self.assertEqual(s["block_order"], schedule.block_order(7))
        self.assertEqual(sorted(s["block_order"]), ["a", "b"])

        convs = s["conversations"]
        self.assertEqual([c["index"] for c in convs], [1, 2, 3, 4, 5, 6])
        self.assertEqual([c["block"] for c in convs], [1, 1, 1, 2, 2, 2])
        for c in convs:
            self.assertIn(c["context"], ("a", "b"))
            self.assertIn(c["condition"], schedule.CONDITIONS)
        # 블록 안에서 세 조건이 한 번씩 (§4 상쇄)
        for block in (1, 2):
            got = sorted(c["condition"] for c in convs if c["block"] == block)
            self.assertEqual(got, sorted(schedule.CONDITIONS))

        self.assertEqual(s["turns_per_conversation"],
                         self.cfg["conversation"]["turns_per_conversation"])
        self.assertIn(s["empathy_variant"], ("A", "B", "C"))
        self.assertEqual(s["prompt_version"], self.cfg["version"])
        self.assertIsInstance(s["model"], str)
        self.assertTrue(s["model"])
        self.assertIsInstance(s["max_tokens"], int)

    def test_session_start_returns_both_prompt_hashes_and_they_differ(self):
        s = self.new_session("P01")
        hashes = s["prompt_sha256"]
        self.assertIsInstance(hashes, dict)
        self.assertEqual(sorted(hashes), ["a", "b"])
        for ctx, h in hashes.items():
            self.assertRegex(h, r"^[0-9a-f]{64}$", "맥락 %s의 해시 형식" % ctx)
        self.assertNotEqual(hashes["a"], hashes["b"],
                            "★ 맥락 A와 B의 프롬프트 해시가 같다 — 맥락 조작이 없다")

    def test_sessions_get_distinct_ids(self):
        first = self.new_session("P01")["session_id"]
        time.sleep(0.002)
        second = self.new_session("P01")["session_id"]
        self.assertNotEqual(first, second)

    def test_unknown_session_is_rejected(self):
        body = self.post("/api/turn", {
            "session_id": "P99-없는세션", "conversation_index": 1, "turn_index": 1,
            "text": "안녕하세요", "user_input_start_ts": 0,
            "user_input_submit_ts": int(time.time() * 1000)},
            expect=400)
        self.assertIn("error", body)

    def test_static_index_is_served(self):
        html = self.get("/").decode("utf-8")
        self.assertIn("<html", html.lower())
        self.assertIn("participant-app", html)


# ══════════════════════════════════════════════════════════════════
# P7 — 같은 맥락 안에서 지연 조건 3수준의 프롬프트 해시가 같다
# ══════════════════════════════════════════════════════════════════

class PromptHashInvarianceTest(ServerCase):

    def test_prompt_hash_identical_across_conditions_within_context(self):
        s = self.new_session("P01")
        by_context = {}
        seen_conditions = {}
        for conv in s["conversations"]:
            turn = self.send_turn(s, conv["index"], 1, BENIGN[0])
            self.assertEqual(turn["context"], conv["context"])
            self.assertEqual(turn["condition"], conv["condition"])
            by_context.setdefault(conv["context"], set()).add(turn["prompt_sha256"])
            seen_conditions.setdefault(conv["context"], set()).add(conv["condition"])

        self.assertEqual(sorted(by_context), ["a", "b"])
        for ctx, conditions in seen_conditions.items():
            self.assertEqual(sorted(conditions), sorted(schedule.CONDITIONS),
                             "맥락 %s에서 세 조건을 모두 보지 못했습니다" % ctx)
        for ctx, hashes in by_context.items():
            self.assertEqual(
                len(hashes), 1,
                "★ 맥락 %s 안에서 지연 조건에 따라 프롬프트가 달라졌다 (P7 위반) — %s"
                % (ctx, sorted(hashes)))

        self.assertNotEqual(next(iter(by_context["a"])), next(iter(by_context["b"])),
                            "★ 맥락 A와 B의 프롬프트가 같다 — 맥락 조작 실패")
        # 세션 시작이 알려준 해시와 턴 응답의 해시가 같아야 한다
        for ctx in ("a", "b"):
            self.assertEqual(next(iter(by_context[ctx])), s["prompt_sha256"][ctx])

    def test_prompt_hash_is_logged_for_every_turn(self):
        s = self.new_session("P01")
        conv = s["conversations"][0]
        self.turn_and_display(s, conv["index"], 1, BENIGN[0])
        row = self.row_for(s, conv["index"], 1)
        self.assertEqual(row["prompt_sha256"], s["prompt_sha256"][conv["context"]])
        self.assertEqual(row["prompt_version"], s["prompt_version"])


# ══════════════════════════════════════════════════════════════════
# P1 — 목표 지연 D는 사용자 입력 내용과 독립이다
# ══════════════════════════════════════════════════════════════════

class DelayIndependenceTest(ServerCase):

    # ★ 이 검사만 배율을 크게 잡는다 — 해상도 때문이다.
    #   D는 정수 밀리초다. TEST_SCALE(0.01)의 즉시 조건이면 D가 10~20ms라
    #   1ms가 D의 7%나 된다. 그러면 "D를 입력 길이에 비례해 몇 % 늘리는"
    #   오염(예: target = int(target * (1 + len(text)/200000)))이 반올림으로
    #   통째로 사라져서 이 검사가 조용히 통과한다. 실제로 그 구현을 넣고
    #   전체 단위 검사 133개가 모두 통과하는 것을 확인했다.
    #   배율 0.1 + 긺 조건이면 D가 1600~2000ms라 1ms가 0.06%다.
    #   느려지는 비용은 세션 하나당 2초 미만이다.
    delay_scale = 0.1

    def test_delay_identical_for_twenty_texts_of_wildly_different_length(self):
        s = self.new_session("P01")
        conv = self.conversation_with(s, "long")      # 목표 지연이 가장 큰 조건
        texts = []
        for i in range(20):
            n = [1, 2, 3, 5, 8, 13, 21, 34, 55, 89,
                 144, 233, 377, 610, 800, 400, 200, 100, 50, 7][i]
            texts.append(("오늘 이야기하고 싶은 것은 %d번째입니다. " % (i + 1)) * 40)
            texts[-1] = texts[-1][:n]

        results = []
        for text in texts:
            turn = self.send_turn(s, conv["index"], 1, text)
            results.append((len(text), turn["target_delay_ms"], turn["deadline_ts"]))

        delays = {d for _, d, _ in results}
        self.assertEqual(
            len(delays), 1,
            "★ P1 위반 — 같은 (세션, 대화, 턴)에서 입력 텍스트에 따라 목표 지연이 달라졌다.\n"
            + "\n".join("  %4d자 → %dms" % (n, d) for n, d, _ in results))
        self.assertEqual(len({n for n, _, _ in results}), 20, "길이가 20종이어야 한다")

        # ★ 해상도 자물쇠 — 배율을 낮춰 이 검사를 무력화하지 못하게 한다.
        #   D가 500ms 미만이면 1ms 미만의 비례 오염이 반올림으로 묻힌다.
        d_ms = results[0][1]
        self.assertGreaterEqual(
            d_ms, 500,
            "목표 지연이 %dms뿐이라 길이 비례 오염이 반올림으로 사라진다 — "
            "이 검사가 통과해도 P1이 지켜졌다고 말할 수 없다. "
            "DelayIndependenceTest.delay_scale을 되돌릴 것." % d_ms)

    def test_deadline_is_submit_plus_target(self):
        s = self.new_session("P01")
        for conv in s["conversations"]:
            submit = int(time.time() * 1000)
            turn = self.send_turn(s, conv["index"], 1, BENIGN[1], submit_ts=submit)
            self.assertEqual(turn["deadline_ts"], submit + turn["target_delay_ms"],
                             "deadline_ts는 전송 시각 + D여야 한다 (CONTRACT §2)")

    def test_delay_differs_across_turns_and_conversations(self):
        """같은 조건이라도 턴마다 새로 뽑는다 (docs/00 §1)."""
        s = self.new_session("P01")
        conv = s["conversations"][0]
        drawn = {self.send_turn(s, conv["index"], t, BENIGN[0])["target_delay_ms"]
                 for t in range(1, 6)}
        self.assertGreater(len(drawn), 1, "5턴이 모두 같은 지연이면 난수 추출이 아니다")


# ══════════════════════════════════════════════════════════════════
# P3 — LLM이 D보다 늦으면 즉시 표시하고 manipulation_ok=false
# ══════════════════════════════════════════════════════════════════

class SlowLlmTest(ServerCase):

    latency_mode = "slow"          # mock 생성 시간 ≈ 2.5초 × 배율

    def test_llm_slower_than_target_marks_manipulation_not_ok(self):
        s = self.new_session("P01")
        conv = self.conversation_with(s, "immediate")     # 목표 10~20ms × 배율
        turn = self.send_turn(s, conv["index"], 1, BENIGN[0])

        self.assertGreater(turn["llm_response_ts"], turn["deadline_ts"],
                           "이 검사는 LLM이 마감보다 늦어야 성립한다 "
                           "(mock --mock-latency-mode slow)")
        out = self.display(turn)
        self.assertFalse(out["manipulation_ok"],
                         "★ P3 위반 — LLM이 목표 지연보다 늦었는데 조작 성공으로 기록되었다")

        row = self.row_for(s, conv["index"], 1)
        self.assertFalse(row["manipulation_ok"], "로그에도 manipulation_ok=false여야 한다")
        self.assertFalse(row["safety_flag"])
        self.assertGreater(row["llm_response_ts"],
                           row["user_input_submit_ts"] + row["target_delay_ms"])

    def test_same_slow_llm_is_fine_in_long_condition(self):
        """대조군 — 느린 LLM이라도 목표 지연 안에 들어오면 조작은 성공이다."""
        s = self.new_session("P01")
        conv = self.conversation_with(s, "long")          # 목표 160~200ms × 배율
        turn = self.send_turn(s, conv["index"], 1, BENIGN[0])
        self.assertLessEqual(turn["llm_response_ts"], turn["deadline_ts"])
        out = self.display(turn, at_deadline=True)
        self.assertTrue(out["manipulation_ok"])
        self.assertLessEqual(abs(out["display_error_ms"]), 250,
                             "표시 오차가 250ms를 넘었다 (조작 충실도 기준)")


# ══════════════════════════════════════════════════════════════════
# P5 — 안전 경로: 지연 없음, LLM 호출 없음
# ══════════════════════════════════════════════════════════════════

class SafetyPathTest(ServerCase):

    def test_safety_input_bypasses_delay_and_never_calls_the_llm(self):
        s = self.new_session("P01")
        conv = s["conversations"][0]
        self.assertTrue(safety.is_excluded(SAFETY_INPUT),
                        "검사 입력이 app/safety.py에 걸리지 않는다 — 검사 자체가 무의미하다")

        calls_before = len(self.provider.calls)
        turn = self.send_turn(s, conv["index"], 1, SAFETY_INPUT)

        self.assertTrue(turn["safety_flag"], "★ safety_flag가 서지 않았다")
        self.assertTrue(turn["bypass_delay"], "★ P5 위반 — 안전 턴에 지연이 적용된다")
        self.assertEqual(turn["reply"], safety.SAFETY_REPLY,
                         "지정 문장만 그대로 돌려주어야 한다 (materials/04 §5)")
        self.assertEqual(
            len(self.provider.calls), calls_before,
            "★ P5 위반 — 안전 입력인데 LLM을 호출했다 (호출 전에 검사해야 한다)")
        self.assertLessEqual(turn["llm_response_ts"] - turn["llm_request_ts"], 5,
                             "LLM을 부르지 않았으므로 생성 시간은 0이어야 한다")

        out = self.display(turn)
        self.assertFalse(out["manipulation_ok"], "안전 턴은 분석에서 빠진다")
        row = self.row_for(s, conv["index"], 1)
        self.assertTrue(row["safety_flag"])
        self.assertFalse(row["manipulation_ok"])
        self.assertEqual(row["ai_response_text"], safety.SAFETY_REPLY)

    def test_safety_turn_raises_researcher_alert_without_keyword(self):
        s = self.new_session("P01")
        conv = s["conversations"][0]
        self.send_turn(s, conv["index"], 2, SAFETY_INPUT)
        plan = json.loads(self.get("/api/session/%s/plan" % s["session_id"]))
        self.assertEqual(len(plan["alerts"]), 1, "연구자 화면에 경보가 남아야 한다 (§5)")
        alert = plan["alerts"][0]
        self.assertEqual(alert["conversation_index"], conv["index"])
        self.assertEqual(alert["turn_index"], 2)
        # 참가자 보호 — 어떤 표현에 걸렸는지는 남기지 않는다 (§5)
        blob = json.dumps(alert, ensure_ascii=False)
        self.assertNotIn("죽고", blob)
        row = self.row_for(s, conv["index"], 2)
        self.assertNotIn("keyword", json.dumps(row, ensure_ascii=False))

    def test_benign_input_does_not_trigger_safety(self):
        s = self.new_session("P01")
        for i, text in enumerate(BENIGN, start=1):
            turn = self.send_turn(s, s["conversations"][0]["index"], i, text)
            self.assertFalse(turn["safety_flag"], "헛발동: %r" % text)
            self.assertFalse(turn["bypass_delay"])


# ══════════════════════════════════════════════════════════════════
# P6 — 대화 6개는 각각 독립. 대화 간 이력 초기화
# ══════════════════════════════════════════════════════════════════

class HistoryResetTest(ServerCase):

    def test_history_does_not_leak_between_conversations(self):
        s = self.new_session("P01")
        first, second = s["conversations"][0], s["conversations"][1]

        secret_one = "첫 번째 대화에서만 나오는 표식 알파일곱입니다."
        secret_two = "첫 대화의 두 번째 턴 표식 베타아홉입니다."
        self.turn_and_display(s, first["index"], 1, secret_one)
        self.turn_and_display(s, first["index"], 2, secret_two)

        # 대화 안에서는 이력이 쌓여야 한다 (materials/04 §7)
        sent = self.provider.calls[-1]["messages"]
        roles = [m["role"] for m in sent]
        self.assertEqual(roles, ["user", "assistant", "user"],
                         "같은 대화의 2턴째는 [user, assistant, user]여야 한다")
        self.assertEqual(sent[0]["content"], secret_one)
        self.assertEqual(sent[-1]["content"], secret_two)

        # 대화가 바뀌면 새 리스트로 시작한다 (P6)
        fresh = "두 번째 대화의 첫 턴입니다."
        self.turn_and_display(s, second["index"], 1, fresh)
        sent2 = self.provider.calls[-1]["messages"]
        self.assertEqual(len(sent2), 1,
                         "★ P6 위반 — 대화 2의 첫 턴에 메시지가 %d개 실려 갔다: %r"
                         % (len(sent2), [m["content"][:20] for m in sent2]))
        self.assertEqual(sent2[0]["role"], "user")
        self.assertEqual(sent2[0]["content"], fresh)

        blob = json.dumps(sent2, ensure_ascii=False)
        for leaked in ("알파일곱", "베타아홉"):
            self.assertNotIn(leaked, blob,
                             "★ P6 위반 — 앞 대화의 내용이 뒤 대화로 넘어갔다 (%s)" % leaked)

    def test_practice_history_does_not_leak_into_conversation_one(self):
        s = self.new_session("P01")
        practice_text = "연습 턴에서만 나오는 표식 감마셋입니다."
        self.turn_and_display(s, schedule.PRACTICE_CONVERSATION_INDEX, 1, practice_text)
        self.turn_and_display(s, s["conversations"][0]["index"], 1, BENIGN[0])
        sent = self.provider.calls[-1]["messages"]
        self.assertEqual(len(sent), 1)
        self.assertNotIn("감마셋", json.dumps(sent, ensure_ascii=False),
                         "★ 연습 턴의 이력이 대화 1에 섞였다 (CONTRACT §6)")

    def test_system_prompt_follows_the_conversation_context(self):
        s = self.new_session("P01")
        systems = {}
        for conv in s["conversations"]:
            self.send_turn(s, conv["index"], 1, BENIGN[2])
            systems.setdefault(conv["context"], set()).add(self.provider.calls[-1]["system"])
        for ctx, seen in systems.items():
            self.assertEqual(len(seen), 1, "맥락 %s에서 system 프롬프트가 흔들렸다" % ctx)
        self.assertNotEqual(next(iter(systems["a"])), next(iter(systems["b"])))


# ══════════════════════════════════════════════════════════════════
# P8 — 연습 턴
# ══════════════════════════════════════════════════════════════════

class PracticeTurnTest(ServerCase):

    def test_practice_turn_uses_fixed_delay_and_is_flagged(self):
        s = self.new_session("P01")
        fixed = round(self.ranges["practice"]["fixed_ms"] * self.scale)

        turn = self.send_turn(s, schedule.PRACTICE_CONVERSATION_INDEX, 1,
                              "연습 턴입니다. 잘 보이나요.")
        self.assertEqual(turn["target_delay_ms"], fixed,
                         "★ P8 위반 — 연습 턴이 조건 지연을 썼다")
        self.assertEqual(turn["condition"], "practice")
        self.display(turn, at_deadline=True)

        row = self.row_for(s, schedule.PRACTICE_CONVERSATION_INDEX, 1)
        self.assertIs(row["practice"], True, "★ 연습 턴이 practice=true로 남지 않았다")
        self.assertEqual(row["target_delay_ms"], fixed)
        self.assertEqual(row["condition"], "practice")
        self.assertFalse(row["manipulation_ok"], "연습 턴은 분석에서 제외된다")

    def test_practice_delay_is_independent_of_condition_ranges(self):
        """연습 턴은 어떤 참가자·어떤 조건 순서에서도 같은 고정값이다."""
        fixed = round(self.ranges["practice"]["fixed_ms"] * self.scale)
        for pid in ("P01", "P02", "P07", "P12"):
            s = self.new_session(pid)
            turn = self.send_turn(s, schedule.PRACTICE_CONVERSATION_INDEX, 1, BENIGN[0])
            self.assertEqual(turn["target_delay_ms"], fixed, pid)


# ══════════════════════════════════════════════════════════════════
# CONTRACT §3 / 계획서 §6 — 로그 스키마
# ══════════════════════════════════════════════════════════════════

class LogSchemaTest(ServerCase):

    def test_jsonl_row_has_every_field_of_the_schema(self):
        s = self.new_session("P05", "comparison")
        conv = s["conversations"][0]
        text = BENIGN[1]
        submit = int(time.time() * 1000)
        start = submit - 4210
        turn = self.send_turn(s, conv["index"], 1, text,
                              submit_ts=submit, start_ts=start)
        out = self.display(turn, at_deadline=True)

        rows = self.log_rows(s)
        self.assertEqual(len(rows), 1, "턴 하나 = 줄 하나여야 한다")
        row = rows[0]

        for field, types in LOG_SCHEMA:
            self.assertIn(field, row, "로그 스키마에 %r이 없다 (계획서 §6)" % field)
            self.assertIsInstance(row[field], types,
                                  "%r의 형이 %s가 아니다: %r" % (field, types, row[field]))
        for field in ("block", "conversation_index", "turn_index", "target_delay_ms",
                      "user_input_chars", "ai_response_chars", "max_tokens",
                      "user_input_start_ts", "user_input_submit_ts",
                      "llm_request_ts", "llm_response_ts", "display_ts"):
            self.assertTrue(_no_bool(row[field]), "%r이 bool이다" % field)

        # CONTRACT §3 — session_id도 함께 남긴다 (사후에 D를 재현하려면 필요하다)
        self.assertEqual(row["session_id"], s["session_id"])

        self.assertEqual(row["participant_id"], "P05")
        self.assertEqual(row["group"], "comparison")
        self.assertEqual(row["block"], conv["block"])
        self.assertEqual(row["conversation_index"], conv["index"])
        self.assertEqual(row["condition"], conv["condition"])
        self.assertEqual(row["context"], conv["context"])
        self.assertEqual(row["turn_index"], 1)
        self.assertIs(row["practice"], False)
        self.assertEqual(row["user_input_start_ts"], start)
        self.assertEqual(row["user_input_submit_ts"], submit)
        self.assertEqual(row["user_input_text"], text)
        self.assertEqual(row["user_input_chars"], len(text))
        self.assertEqual(row["target_delay_ms"], turn["target_delay_ms"])
        self.assertEqual(row["llm_request_ts"], turn["llm_request_ts"])
        self.assertEqual(row["llm_response_ts"], turn["llm_response_ts"])
        self.assertEqual(row["ai_response_text"], turn["reply"])
        self.assertEqual(row["ai_response_chars"], len(turn["reply"]))
        self.assertIsNone(row["next_input_start_ts"], "마지막 턴은 null이어야 한다")
        self.assertEqual(row["finish_reason"], turn["finish_reason"])
        self.assertIn(row["finish_reason"], ("stop", "length", "refusal"))
        self.assertEqual(row["empathy_variant"], s["empathy_variant"])
        self.assertEqual(row["model"], turn["model"])
        self.assertEqual(row["max_tokens"], s["max_tokens"])
        self.assertEqual(row["prompt_sha256"], s["prompt_sha256"][conv["context"]])

        # 파생값은 로그에 넣지 않는다 (CONTRACT §3) — 스크립트가 계산한다
        for derived in ("imposed_delay_ms", "llm_latency_ms", "display_error_ms",
                        "user_response_latency_ms", "typing_ms"):
            self.assertNotIn(derived, row, "파생값 %r을 로그에 넣지 않는다" % derived)

        # display_ts는 표시 시각이며 마감과 250ms 안에서 만난다
        self.assertEqual(row["display_ts"] - turn["deadline_ts"], out["display_error_ms"])
        self.assertLessEqual(abs(out["display_error_ms"]), 250)

    def test_manipulation_check_can_read_the_log(self):
        """analysis/manipulation_check.py의 필수 필드를 그대로 통과하는지."""
        sys.path.insert(0, str(REPO_ROOT / "analysis"))
        import manipulation_check  # noqa: E402

        s = self.new_session("P01")
        for conv in s["conversations"]:
            self.turn_and_display(s, conv["index"], 1, BENIGN[3])
        path = self.log_dir / ("%s.turns.jsonl" % s["session_id"])
        turns, problems = manipulation_check.load([str(path)])
        self.assertEqual(problems, [], "manipulation_check가 로그를 읽지 못했다")
        self.assertEqual(len(turns), 6)
        for t in turns:
            manipulation_check.derive(t)
            self.assertTrue(manipulation_check.analyzable(t))

    def test_one_line_per_turn_and_order_is_preserved(self):
        s = self.new_session("P01")
        conv = s["conversations"][0]
        for t in range(1, 4):
            self.turn_and_display(s, conv["index"], t, BENIGN[t % len(BENIGN)])
        rows = self.log_rows(s)
        self.assertEqual([r["turn_index"] for r in rows], [1, 2, 3])


# ══════════════════════════════════════════════════════════════════
# CONTRACT §2 — /api/turn/display, /api/turn/next-input, /end
# ══════════════════════════════════════════════════════════════════

class TurnLifecycleTest(ServerCase):

    def test_next_input_fills_the_previous_turn(self):
        s = self.new_session("P01")
        conv = s["conversations"][0]
        first = self.turn_and_display(s, conv["index"], 1, BENIGN[0])
        second = self.turn_and_display(s, conv["index"], 2, BENIGN[1])

        # 서버 시계와 겹치지 않는 값을 보낸다 — 그대로 저장되어야 한다.
        # (서버가 자기 시계로 덮어쓰면 응답 잠복기 user_response_latency_ms가 깨진다)
        typed_at = first["deadline_ts"] + 4321
        self.post("/api/turn/next-input",
                  {"turn_id": first["turn_id"], "next_input_start_ts": typed_at})

        row1 = self.row_for(s, conv["index"], 1)
        row2 = self.row_for(s, conv["index"], 2)
        self.assertEqual(row1["next_input_start_ts"], typed_at,
                         "★ 다음 턴의 첫 타자 시각이 직전 턴에 채워지지 않았다 (§2)")
        self.assertIsNone(row2["next_input_start_ts"],
                          "아직 다음 입력이 없는 턴은 null이어야 한다")
        self.assertEqual(second["turn_id"], "%s:%d:2" % (s["session_id"], conv["index"]))

    def test_display_is_required_before_the_row_is_complete(self):
        s = self.new_session("P01")
        conv = s["conversations"][0]
        turn = self.send_turn(s, conv["index"], 1, BENIGN[0])
        row = self.row_for(s, conv["index"], 1)
        self.assertIsNone(row["display_ts"], "표시 전에는 display_ts가 비어 있어야 한다")

        out = self.display(turn, at_deadline=True)
        self.assertTrue(out["ok"])
        row = self.row_for(s, conv["index"], 1)
        self.assertIsInstance(row["display_ts"], int)
        self.assertEqual(row["display_ts"] - turn["deadline_ts"], out["display_error_ms"])

    def test_display_ts_is_stored_verbatim_not_the_server_clock(self):
        """★ display_ts는 브라우저가 화면에 붙인 직후 찍은 값이다 (CONTRACT §2·§7).

        서버가 자기 시계로 덮어쓰면 부과 지연에 네트워크 왕복이 섞여 들어가고
        주 종속변수(imposed_delay_ms)가 통째로 오염된다. 서버 시계와 절대
        겹치지 않는 값을 보내 그대로 저장되는지 본다.
        """
        s = self.new_session("P01")
        conv = s["conversations"][0]
        turn = self.send_turn(s, conv["index"], 1, BENIGN[0])
        marker = turn["deadline_ts"] + 137          # 서버의 now_ms와 137ms 이상 차이난다

        out = self.post("/api/turn/display",
                        {"turn_id": turn["turn_id"], "display_ts": marker})
        self.assertEqual(out["display_error_ms"], 137,
                         "display_error_ms는 보낸 display_ts − deadline이어야 한다")
        row = self.row_for(s, conv["index"], 1)
        self.assertEqual(row["display_ts"], marker,
                         "★ 서버가 display_ts를 자기 시계로 덮어썼다 — "
                         "부과 지연에 네트워크 왕복이 섞인다")

    def test_unknown_turn_id_is_rejected(self):
        self.post("/api/turn/display",
                  {"turn_id": "없는턴", "display_ts": 1}, expect=400)
        self.post("/api/turn/next-input",
                  {"turn_id": "없는턴", "next_input_start_ts": 1}, expect=400)

    def test_end_closes_the_session_and_keeps_incomplete_turns(self):
        s = self.new_session("P01")
        conv = s["conversations"][0]
        self.turn_and_display(s, conv["index"], 1, BENIGN[0])
        self.send_turn(s, conv["index"], 2, BENIGN[1])          # 표시하지 않는다

        out = self.post("/api/session/%s/end" % s["session_id"], {})
        self.assertEqual(out["turns"], 2)
        self.assertEqual(out["incomplete"], 1, "표시되지 않은 턴은 그대로 남는다 (§2)")
        rows = self.log_rows(s)
        self.assertIsNone(rows[1]["display_ts"])

    def test_survey_is_written_to_its_own_file(self):
        s = self.new_session("P01")
        payload = {"session_id": s["session_id"], "kind": "per_condition",
                   "conversation_index": 1, "shown_ts": 1, "submitted_ts": 2,
                   "responses": {"time_estimate_sec": 9, "discomfort": 4}}
        self.post("/api/survey", payload)
        path = self.log_dir / ("%s.surveys.jsonl" % s["session_id"])
        self.assertTrue(path.is_file(), "logs/{session_id}.surveys.jsonl (CONTRACT §3)")
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(rows[0]["kind"], "per_condition")
        self.assertEqual(rows[0]["responses"]["time_estimate_sec"], 9)


# ══════════════════════════════════════════════════════════════════
# P4 — 스트리밍 금지
# ══════════════════════════════════════════════════════════════════

class NoStreamingTest(ServerCase):

    def test_reply_arrives_whole_in_one_response(self):
        s = self.new_session("P01")
        conv = s["conversations"][0]
        turn = self.send_turn(s, conv["index"], 1, BENIGN[0])

        self.assertFalse(self.cfg["model"]["stream"], "prompts.yaml: stream은 false여야 한다")
        self.assertIn("Content-Length", self.last_headers,
                      "★ 청크 전송 — 응답을 통째로 돌려주어야 한다 (P4)")
        self.assertNotIn("chunked", str(self.last_headers.get("Transfer-Encoding", "")).lower())
        # 몸통 전체가 한 번에 온다 — 조각이 아니다
        self.assertEqual(int(self.last_headers["Content-Length"]), len(self.last_raw))
        self.assertEqual(json.loads(self.last_raw.decode("utf-8"))["reply"], turn["reply"])
        self.assertIsInstance(turn["reply"], str)
        self.assertTrue(turn["reply"].strip())
        self.assertEqual(turn["reply"], turn["reply"].strip())
        # 서버는 응답을 다 받은 뒤에 돌려준다 — llm_response_ts가 이미 찍혀 있다
        self.assertGreaterEqual(turn["llm_response_ts"], turn["llm_request_ts"])
        self.assertGreater(turn["llm_response_ts"], 0)


# ══════════════════════════════════════════════════════════════════
# E2E 고속 모드의 전제 — 배율은 세 조건에 똑같이 적용된다
# ══════════════════════════════════════════════════════════════════

class DelayScaleTest(ServerCase):

    delay_scale = 0.05

    def test_delay_scale_scales_every_condition_equally(self):
        s = self.new_session("P01")
        for conv in s["conversations"]:
            lo, hi = self.scaled(conv["condition"])
            turn = self.send_turn(s, conv["index"], 1, BENIGN[0])
            self.assertGreaterEqual(turn["target_delay_ms"], lo,
                                    "%s 조건이 축소 범위 %d~%d 밖" % (conv["condition"], lo, hi))
            self.assertLessEqual(turn["target_delay_ms"], hi)

        practice = self.send_turn(s, schedule.PRACTICE_CONVERSATION_INDEX, 1, BENIGN[0])
        self.assertEqual(practice["target_delay_ms"],
                         round(self.ranges["practice"]["fixed_ms"] * self.scale),
                         "연습 지연도 같은 배율을 따라야 한다")

    def test_scale_is_recorded_in_every_log_row(self):
        """manipulation_check.py가 이 값으로 기대 범위를 되돌린다."""
        s = self.new_session("P01")
        conv = s["conversations"][0]
        self.turn_and_display(s, conv["index"], 1, BENIGN[0])
        row = self.row_for(s, conv["index"], 1)
        self.assertIn("delay_scale", row,
                      "축소 로그를 본 실험 데이터와 구분할 수 없다")
        self.assertAlmostEqual(row["delay_scale"], self.scale)

    def test_ranges_do_not_overlap_after_scaling(self):
        s = self.new_session("P01")
        bounds = [self.scaled(c) for c in schedule.CONDITIONS]
        for (_, hi), (lo, _) in zip(bounds, bounds[1:]):
            self.assertLess(hi, lo, "축소 후 조건 범위가 겹친다 — 대비가 사라진다")
        self.assertTrue(all(lo > 0 for lo, _ in bounds), "축소 후 지연이 0이 되었다")
        self.assertEqual(len(s["conversations"]), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
