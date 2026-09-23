#!/usr/bin/env python3
"""조작 점검 · 통제 확인 (analysis/10-manipulation-check-plan.md 구현)

입력: 턴 단위 JSONL 로그 (스키마는 계획서 §6)
출력: 사람이 읽는 보고서 또는 --json

의존성 없음(표준 라이브러리만). 파일럿 중 빠른 점검용이며,
논문에 싣는 응답 유사도는 문장 임베딩으로 다시 계산할 것.

사용:
    python3 analysis/manipulation_check.py logs/*.jsonl
    python3 analysis/manipulation_check.py --demo
    python3 analysis/manipulation_check.py --demo-broken   # 잘못된 구현을 잡아내는지 확인
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import random
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "prompts"))
try:
    import response_rules
except ImportError:
    response_rules = None

CONDITIONS = ["R1", "R2", "R3"]
CONDITION_KO = {"R1": "R1 깊게", "R2": "R2 짧게", "R3": "R3 무규칙"}
DEPTHS = ["deep", "medium", "shallow"]
DEPTH_KO = {"deep": "깊음", "medium": "보통", "shallow": "얕음"}

# prompts/prompts.yaml의 delay_conditions와 일치해야 한다.
# 지연 배치 설계에서는 조건이 아니라 **깊이**가 목표 지연을 정한다.
TARGET_MS = {"deep": 15000, "medium": 8000, "shallow": 3000}
TOTAL_PER_CONVERSATION_MS = 3 * sum(TARGET_MS.values())   # 78000

DISPLAY_TOLERANCE_MS = 250   # |실제 − 목표| 허용 오차
DEFAULT_EQUIV_BOUND = 0.10   # 등가 한계 |r|


# ─────────────────────────────── 통계 ───────────────────────────────

def mean(xs):
    return st.fmean(xs) if xs else float("nan")


def sd(xs):
    return st.stdev(xs) if len(xs) > 1 else 0.0


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return float("nan")
    return num / (dx * dy)


def fisher_ci(r, n, conf=0.95):
    """Fisher z 변환 기반 상관계수 신뢰구간."""
    if n < 4 or math.isnan(r) or abs(r) >= 1:
        return (float("nan"), float("nan"))
    z = math.atanh(r)
    se = 1.0 / math.sqrt(n - 3)
    crit = 1.959963985 if abs(conf - 0.95) < 1e-9 else abs(_ppf((1 + conf) / 2))
    lo, hi = z - crit * se, z + crit * se
    return (math.tanh(lo), math.tanh(hi))


def _ppf(p):
    """표준정규 분위수 (Acklam 근사). conf가 0.95가 아닐 때만 쓴다."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def required_n_for_ci(r, bound, conf=0.95):
    """|r|의 95% CI가 (−bound, bound) 안에 들어오려면 필요한 턴 수.

    Fisher z에서 CI 반폭 = crit / sqrt(N−3) 이므로
        N ≥ 3 + (crit / (atanh(bound) − |atanh(r)|))²
    r이 bound에 가까울수록 필요한 N이 급격히 커진다.
    """
    if math.isnan(r) or abs(r) >= bound:
        return None
    crit = 1.959963985 if abs(conf - 0.95) < 1e-9 else abs(_ppf((1 + conf) / 2))
    margin = math.atanh(bound) - abs(math.atanh(r))
    if margin <= 0:
        return None
    return int(math.ceil(3 + (crit / margin) ** 2))


def bigrams(text):
    t = "".join(text.split())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def mean_pairwise_similarity(texts, max_pairs=2000, rng=None):
    """문자 바이그램 Jaccard의 평균 쌍별 유사도. 표면 어휘만 본다."""
    grams = [bigrams(t) for t in texts if t and t.strip()]
    if len(grams) < 2:
        return float("nan")
    pairs = [(i, j) for i in range(len(grams)) for j in range(i + 1, len(grams))]
    if len(pairs) > max_pairs:
        pairs = (rng or random.Random(0)).sample(pairs, max_pairs)
    return mean([jaccard(grams[i], grams[j]) for i, j in pairs])


def mean_cross_similarity(texts_a, texts_b, max_pairs=2000, rng=None):
    ga = [bigrams(t) for t in texts_a if t and t.strip()]
    gb = [bigrams(t) for t in texts_b if t and t.strip()]
    if not ga or not gb:
        return float("nan")
    pairs = [(i, j) for i in range(len(ga)) for j in range(len(gb))]
    if len(pairs) > max_pairs:
        pairs = (rng or random.Random(0)).sample(pairs, max_pairs)
    return mean([jaccard(ga[i], gb[j]) for i, j in pairs])


# ─────────────────────────────── 로딩 ───────────────────────────────

REQUIRED = [
    "condition", "depth", "user_input_chars",
    "user_input_submit_ts", "display_ts", "target_delay_ms",
    "llm_request_ts", "llm_response_ts", "ai_response_text",
]


def load(paths):
    turns, problems = [], []
    for p in paths:
        for lineno, line in enumerate(Path(p).read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                problems.append(f"{p}:{lineno} JSON 파싱 실패 — {e}")
                continue
            missing = [k for k in REQUIRED if k not in rec]
            if missing:
                problems.append(f"{p}:{lineno} 필수 필드 없음 — {', '.join(missing)}")
                continue
            rec["_source"] = f"{p}:{lineno}"
            turns.append(rec)
    return turns, problems


def derive(t):
    # display_ts는 None일 수 있다. 세션이 중간에 끝나면(참가자 철회, 안전 중단,
    # 브라우저 종료) 그 턴은 화면에 표시되지 않은 채 로그에 남는다.
    disp = t.get("display_ts")
    t["displayed"] = disp is not None
    t["imposed_delay_ms"] = (disp - t["user_input_submit_ts"]) if disp is not None else None
    t["llm_latency_ms"] = t["llm_response_ts"] - t["llm_request_ts"]
    t["display_error_ms"] = (
        t["imposed_delay_ms"] - t["target_delay_ms"] if disp is not None else None)
    nxt = t.get("next_input_start_ts")
    t["user_response_latency_ms"] = (nxt - disp) if (nxt and disp is not None) else None
    start = t.get("user_input_start_ts")
    t["typing_ms"] = (t["user_input_submit_ts"] - start) if start else None
    if "ai_response_chars" not in t:
        t["ai_response_chars"] = len(t.get("ai_response_text") or "")
    return t


def analyzable(t):
    return (
        not t.get("practice")
        and not t.get("opener")          # 오프너(고정 문구·8초·분석 제외)
        and not t.get("safety_flag")
        and t.get("displayed")            # 표시되지 않은 턴은 부과 지연이 없다
        and t.get("condition") in CONDITIONS
    )


# ─────────────────────────────── 검사 ───────────────────────────────

class Report:
    def __init__(self):
        self.lines, self.checks, self.notes, self.data = [], [], [], {}

    def say(self, s=""):
        self.lines.append(s)

    def head(self, s):
        self.say()
        self.say(s)
        self.say("─" * 74)

    def check(self, name, ok, detail=""):
        """통과/실패가 종료 코드에 반영되는 검사."""
        self.checks.append({"name": name, "pass": bool(ok), "detail": detail})
        self.say(f"  {'✓' if ok else '✗'} {name}{('  — ' + detail) if detail else ''}")

    def note(self, name, ok, detail=""):
        """참고 지표. 표본 크기에 좌우되므로 종료 코드에 반영하지 않는다."""
        self.notes.append({"name": name, "pass": bool(ok), "detail": detail})
        self.say(f"  {'✓' if ok else '△'} {name}{('  — ' + detail) if detail else ''}")


def fmt(x, nd=1):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{nd}f}"


def run(turns, equiv_bound):
    rep = Report()
    kept = [t for t in turns if analyzable(t)]
    dropped = len(turns) - len(kept)

    # E2E 고속 모드로 수집된 로그는 조건 범위가 배율만큼 축소되어 있다.
    scales = {float(t.get("delay_scale") or 1.0) for t in kept} or {1.0}
    scale = scales.pop() if len(scales) == 1 else 1.0
    targets = {d: round(ms * scale) for d, ms in TARGET_MS.items()}

    rep.head("0 · 데이터")
    n_practice = sum(1 for t in turns if t.get("practice"))
    n_opener = sum(1 for t in turns if t.get("opener"))
    n_safety = sum(1 for t in turns if t.get("safety_flag") and not t.get("practice"))
    n_undisplayed = sum(1 for t in turns
                        if not t.get("displayed") and not t.get("practice")
                        and not t.get("opener") and not t.get("safety_flag"))
    rep.say(f"  전체 턴 {len(turns)} / 분석 대상 {len(kept)} (제외 {dropped})")
    rep.say(f"    연습 {n_practice} · 오프너 {n_opener} · 안전 경로 {n_safety} · 미표시(중단) {n_undisplayed}"
            f" · 기타 {dropped - n_practice - n_opener - n_safety - n_undisplayed}")
    if n_undisplayed:
        rep.say("    ※ 미표시 턴은 세션이 중간에 끝난 흔적이다. 논문에 이탈로 보고한다.")
    rep.data["excluded"] = {"practice": n_practice, "opener": n_opener,
                            "safety": n_safety, "undisplayed": n_undisplayed}
    by_cond = defaultdict(list)
    for t in kept:
        by_cond[t["condition"]].append(t)
    for c in CONDITIONS:
        rep.say(f"    {CONDITION_KO[c]:<4} {len(by_cond[c]):>5} 턴")
    parts = sorted({t.get("participant_id", "?") for t in kept})
    rep.say(f"  참가자 {len(parts)}명: {', '.join(parts[:12])}{' …' if len(parts) > 12 else ''}")
    if len(scales) > 0:
        rep.check("delay_scale이 로그 전체에서 단일", False, "여러 배율이 섞여 있다 — 합쳐서 분석하면 안 된다")
    elif scale != 1.0:
        rep.say(f"  ⚠ delay_scale = {scale} — 지연이 축소된 로그다. 검증용이며 본 실험 데이터가 아니다.")
    rep.data["delay_scale"] = scale
    rep.data["n_turns"] = len(kept)
    rep.data["n_participants"] = len(parts)

    # ★ 테스트 패널(?test=1)로 만든 로그가 섞이면 분석이 오염된다.
    #   그 패널은 설문을 자동으로 채우고 세션을 자동주행한다. 서버가 그런
    #   세션의 모든 레코드에 test_mode 를 찍으므로(app/server.py), 여기서
    #   거른다. 주석으로 "실세션에서는 쓰지 않는다"고 적어 두는 것만으로는
    #   산출된 JSONL 이 진짜 참가자 로그와 구분되지 않는다.
    n_test = sum(1 for t in turns if t.get("test_mode"))
    test_sessions = sorted({t.get("session_id", "?") for t in turns if t.get("test_mode")})
    rep.check("테스트 패널로 만든 턴 없음", n_test == 0,
              f"{n_test}턴 · 세션 {', '.join(test_sessions[:5])}"
              f"{' …' if len(test_sessions) > 5 else ''} — 분석에서 빼야 한다" if n_test else "")
    rep.data["test_mode_turns"] = n_test

    if not kept:
        rep.check("분석 가능한 턴 존재", False, "0턴")
        return rep

    # ── 1 · 독립성 ──
    rep.head("1 · 입력 길이 ↔ 부과 지연 독립성  ★")

    # ★ 조건 내 부분상관이 주 지표다.
    #   전체 상관은 조건 간 지연 차이(1.5s / 8.5s / 18s)가 분산을 지배하므로
    #   구현이 틀려도 0 근처로 나온다. --demo-broken 으로 확인할 수 있다.
    def partial_r(cells):
        """cells: 턴 -> 그룹 키. 그룹 평균으로 중심화한 뒤 상관을 낸다."""
        groups = defaultdict(list)
        for t in kept:
            groups[cells(t)].append(t)
        gx, gy = [], []
        used = 0
        for g in groups.values():
            if len(g) < 3:
                continue
            used += 1
            mc = mean([t["user_input_chars"] for t in g])
            md = mean([t["imposed_delay_ms"] for t in g])
            gx += [t["user_input_chars"] - mc for t in g]
            gy += [t["imposed_delay_ms"] - md for t in g]
        n_eff = max(4, len(gx) - max(0, used - 1))
        return pearson(gx, gy), n_eff, len(gx), used

    # ★ 조건뿐 아니라 턴 위치도 통제한다.
    #   참가자는 턴 위치에 따라 체계적으로 길게/짧게 쓸 수 있고(워밍업, 피로),
    #   표본이 작으면 D의 난수 추출이 우연히 턴 위치와 정렬될 수 있다.
    #   두 가지가 겹치면 조건만 통제한 부분상관에 허위 상관이 뜬다.
    r, n_eff, n_used, n_cells = partial_r(lambda t: (t["condition"], t.get("turn_index")))
    r_cond, n_eff_c, _, _ = partial_r(lambda t: t["condition"])
    lo, hi = fisher_ci(r, n_eff)
    inside = (not math.isnan(lo)) and lo > -equiv_bound and hi < equiv_bound
    rep.say(f"  ★ 조건 × 턴 위치 통제 부분상관:  r = {fmt(r, 3)}"
            f"   95% CI [{fmt(lo, 3)}, {fmt(hi, 3)}]   N = {n_used} ({n_cells}개 칸)")
    lc, hc = fisher_ci(r_cond, n_eff_c)
    rep.say(f"    (조건만 통제:  r = {fmt(r_cond, 3)}   95% CI [{fmt(lc, 3)}, {fmt(hc, 3)}])")
    if not math.isnan(r) and not math.isnan(r_cond) and abs(r_cond) - abs(r) > 0.10:
        rep.say("    ※ 턴 위치를 통제하면 상관이 크게 줄었다. 입력 길이가 턴 위치를")
        rep.say("      따라 체계적으로 변한다는 뜻이다 — 조작 실패가 아니라 발화 패턴이다.")
    rep.data["within_condition_r"] = None if math.isnan(r) else round(r, 4)
    rep.data["condition_only_r"] = None if math.isnan(r_cond) else round(r_cond, 4)
    rep.data["within_condition_ci"] = [None if math.isnan(lo) else round(lo, 4),
                                       None if math.isnan(hi) else round(hi, 4)]

    rep.say()
    rep.say("  조건별:")
    per_cond_r = {}
    for c in CONDITIONS:
        sub = by_cond[c]
        if len(sub) >= 4:
            rc = pearson([t["user_input_chars"] for t in sub],
                         [t["imposed_delay_ms"] for t in sub])
            l2, h2 = fisher_ci(rc, len(sub))
            per_cond_r[c] = None if math.isnan(rc) else round(rc, 4)
            flag = "  ←" if (not math.isnan(rc) and abs(rc) >= equiv_bound) else ""
            rep.say(f"    {CONDITION_KO[c]:<4}  r = {fmt(rc, 3)}   95% CI [{fmt(l2, 3)}, {fmt(h2, 3)}]"
                    f"   n = {len(sub)}{flag}")
    rep.data["per_condition_r"] = per_cond_r

    r_all = pearson([t["user_input_chars"] for t in kept], [t["imposed_delay_ms"] for t in kept])
    rep.say()
    rep.say(f"  (참고) 조건을 무시한 전체 상관: r = {fmt(r_all, 3)}   N = {len(kept)}")
    rep.say("        ※ 이 값은 보고하지 않는다. 조건 간 지연 차이가 분산을 지배해")
    rep.say("          구현이 틀려도 0 근처로 나온다 — --demo-broken 으로 재현된다.")
    rep.data["overall_r_do_not_report"] = None if math.isnan(r_all) else round(r_all, 4)

    # 조건별 검사와 같은 논리를 쓴다. |r| ≥ 한계만으로 실패시키면 표본이 작을 때
    # 오경보가 난다 (N=60이면 SE ≈ .15). 신뢰구간이 0을 배제할 때만 실패로 본다.
    pooled_noisy = math.isnan(lo) or (lo <= 0 <= hi)
    pooled_bad = (not math.isnan(r)) and abs(r) >= equiv_bound and not pooled_noisy
    rep.check(f"조건 × 턴 통제 부분상관 |r| < {equiv_bound} (95% CI가 0을 배제하는 경우만 실패)",
              not pooled_bad,
              f"r = {fmt(r, 3)}  CI [{fmt(lo, 3)}, {fmt(hi, 3)}]" if pooled_bad else
              (f"r = {fmt(r, 3)}이지만 CI가 0을 포함 — 표본오차" if not math.isnan(r) and abs(r) >= equiv_bound
               else f"r = {fmt(r, 3)}"))
    # 조건별 n은 전체의 1/3이라 표본오차가 크다 (n=240이면 SE ≈ .065).
    # |r| ≥ 한계만으로 실패시키면 우연히 튄 조건에서 오경보가 난다.
    # 3개 조건을 보므로 99% CI를 쓰고, CI가 0을 배제할 때만 실패로 본다.
    bad = []
    for c, v in per_cond_r.items():
        if v is None or abs(v) < equiv_bound:
            continue
        l3, h3 = fisher_ci(v, len(by_cond[c]), conf=0.99)
        if not math.isnan(l3) and (l3 > 0 or h3 < 0):
            bad.append(c)
        else:
            rep.say(f"    ({CONDITION_KO[c]} r = {fmt(v, 3)}는 한계를 넘지만 99% CI가 0을 포함 — 표본오차)")
    rep.check(f"모든 조건에서 |r| < {equiv_bound} (99% CI가 0을 배제하는 경우만 실패)", not bad,
              f"초과: {', '.join(CONDITION_KO[c] + ' ' + fmt(per_cond_r[c], 3) for c in bad)}" if bad else "")
    need = required_n_for_ci(r, equiv_bound)
    detail = "" if inside else (
        f"이 r에서 CI를 (−{equiv_bound}, {equiv_bound}) 안에 넣으려면 턴 N ≈ {need} 필요 (현재 {len(kept)})"
        if need else "표본이 부족하거나 r이 한계에 가깝다")
    rep.note(f"95% CI ⊂ (−{equiv_bound}, {equiv_bound})", inside, detail)
    rep.data["n_required_for_ci"] = need
    if not inside:
        rep.say("    ※ D는 입력을 보기 전에 난수로 뽑히므로 모상관은 설계상 정확히 0이다.")
        rep.say("       이 검사는 등가성 '입증'이 아니라 구현 검증이다 — 계획서 §1 참조.")
    if pooled_bad or bad:
        rep.say()
        rep.say("  ⚠ 구현을 의심할 것 (계획서 §1):")
        rep.say("    1) D를 전송 직후에 뽑는가, LLM 응답 후에 뽑는가")
        rep.say("    2) 대기 시작점 t0가 전송 시각인가")
        rep.say("    3) stream 이 꺼져 있는가")
        rep.say("    4) 표시 시각을 t0 + D 로 계산하는가")

    # ── 2 · 충실도 ──
    rep.head("2 · 조작 충실도")

    # (a) 세 조건의 대화당 총 지연이 같아야 한다 — 이 설계의 전제
    rep.say("  대화별 총 목표 지연 (세 조건에서 같아야 한다)")
    by_conv = defaultdict(list)
    for t in kept:
        by_conv[(t.get("session_id"), t.get("conversation_index"))].append(t)
    totals = defaultdict(set)
    for (sid, ci), rows in sorted(by_conv.items()):
        if len(rows) < 9:
            continue
        totals[rows[0]["condition"]].add(sum(r["target_delay_ms"] for r in rows))
    for c in CONDITIONS:
        if totals.get(c):
            vals = sorted(totals[c])
            rep.say(f"    {CONDITION_KO[c]:<8} {', '.join(f'{v/1000:.1f}초' for v in vals[:4])}")
    allv = {v for vs in totals.values() for v in vs}
    rep.check("세 조건의 대화당 총 지연이 동일", len(allv) <= 1,
              f"서로 다른 값 {sorted(allv)}" if len(allv) > 1 else f"{(allv.pop()/1000 if allv else 0):.1f}초")

    # (a2) 대화별 목표지연 총합의 절대값 — 각 대화 target 합이 78000*scale ±250ms
    expected_total = TOTAL_PER_CONVERSATION_MS * scale
    bad_totals = []
    for (sid, ci), rows in sorted(by_conv.items()):
        if len(rows) < 9:
            continue
        total = sum(r["target_delay_ms"] for r in rows)
        if abs(total - expected_total) > DISPLAY_TOLERANCE_MS:
            bad_totals.append(f"{sid} 대화{ci}={total}ms")
    rep.check(f"대화별 목표지연 총합이 {TOTAL_PER_CONVERSATION_MS}×{scale:g} ±{DISPLAY_TOLERANCE_MS}ms",
              not bad_totals,
              f"기대 {expected_total:g}ms에서 벗어남: {', '.join(bad_totals[:6])}"
              + (f" 외 {len(bad_totals) - 6}건" if len(bad_totals) > 6 else "")
              if bad_totals else f"기대 {expected_total:g}ms")
    rep.data["conversation_total_expected_ms"] = expected_total

    # (b) 깊이 → 지연 배치가 조건 규칙대로인가
    rep.say()
    rep.say("  깊이별 평균 목표 지연 (조건이 정의하는 배치)")
    rep.say(f"    {'조건':<10}{'깊음':>10}{'보통':>10}{'얕음':>10}")
    placement_ok = True
    for c in CONDITIONS:
        rows = [t for t in kept if t["condition"] == c]
        if not rows:
            continue
        m = {}
        for d in DEPTHS:
            v = [t["target_delay_ms"] for t in rows if t.get("depth") == d]
            m[d] = mean(v) if v else float("nan")
        rep.say(f"    {CONDITION_KO[c]:<10}{fmt(m['deep'], 0):>10}{fmt(m['medium'], 0):>10}{fmt(m['shallow'], 0):>10}")
        if c == "R1" and not (m["shallow"] < m["medium"] < m["deep"]):
            placement_ok = False
        if c == "R2" and not (m["deep"] < m["medium"] < m["shallow"]):
            placement_ok = False
    rep.check("R1은 깊을수록 길게 · R2는 그 반대", placement_ok)

    r3 = [t for t in kept if t["condition"] == "R3"]
    if r3:
        spread = {}
        for d in DEPTHS:
            v = [t["target_delay_ms"] for t in r3 if t.get("depth") == d]
            spread[d] = sd(v) if len(v) > 1 else 0.0
        rep.say(f"    R3 깊이별 지연 표준편차: " +
                " · ".join(f"{DEPTH_KO[d]} {fmt(spread[d], 0)}" for d in DEPTHS))
        # ★ 깊이별 표준편차로도, Fisher CI 로도 판정하면 안 된다.
        #
        #   R3의 대화 하나는 9턴(깊이마다 3턴)이고, 같은 지연 묶음
        #   {15,15,15,8,8,8,3,3,3}초를 깊이와 무관하게 섞는다.
        #     · 표준편차 임계값 → 어떤 깊이에 같은 값 3개가 우연히 몰리는 일이
        #       대화당 10.4% (시드 2000개 × 대화 3개 실측 10.38%). 정상
        #       데이터인데 참가자 10명 중 1명꼴로 "조작 실패"가 뜬다.
        #     · Fisher CI → 지연 묶음이 고정된 이산 설계라 n=9에서 정규 근사가
        #       성립하지 않는다. 우연히 단조로 배열된 대화 하나에 CI가
        #       0을 벗어난다.
        #
        #   이 설계의 귀무가설은 "대화 안에서 지연을 섞는다"이다. 그 섞기를
        #   그대로 재현하는 순열검정이 정확한 기준이고, 표본 크기와 무관하게
        #   오경보율이 임계값 그대로다.
        rank = {"shallow": 0, "medium": 1, "deep": 2}
        conv_rows = defaultdict(list)
        for t in r3:
            if t.get("depth") in rank:
                conv_rows[(t.get("session_id"), t.get("conversation_index"))].append(t)

        def pooled_r(assign):
            xs, ys = [], []
            for key, rows in conv_rows.items():
                for t, ms in zip(rows, assign[key]):
                    xs.append(rank[t["depth"]])
                    ys.append(ms)
            return pearson(xs, ys)

        observed = {k: [t["target_delay_ms"] for t in rows] for k, rows in conv_rows.items()}
        r3_r = pooled_r(observed)
        rng = random.Random(20260918)           # 재현 가능하게 고정
        trials, extreme = 5000, 0
        if not math.isnan(r3_r):
            for _ in range(trials):
                shuffled = {}
                for k, ms in observed.items():
                    v = list(ms)
                    rng.shuffle(v)              # ★ 대화 안에서만 섞는다 = 설계의 귀무가설
                    shuffled[k] = v
                rv = pooled_r(shuffled)
                if not math.isnan(rv) and abs(rv) >= abs(r3_r):
                    extreme += 1
            pval = (extreme + 1) / (trials + 1)
        else:
            pval = 1.0
        # 임계값 0.001 — 정상 데이터에서 실패할 확률이 0.1% 다 (p<0.01 이면 1.0%,
        # 실측 400회에서 4회). 배치가 깊이에 완전히 묶이면 r=1 이라 p 가 최소값
        # 0.0002 로 떨어지므로, 조여도 잡을 것은 그대로 잡는다.
        rep.check("R3은 깊이와 지연이 묶여 있지 않다", pval >= 0.001,
                  f"깊이 순위 ↔ 지연 r={fmt(r3_r, 3)} "
                  f"순열검정 p={fmt(pval, 3)} (대화 {len(conv_rows)}개 · 턴 {len(r3)}) "
                  f"— p<0.001이면 배치에 규칙이 있는 것이다")
        rep.data["r3_depth_delay_r"] = None if math.isnan(r3_r) else r3_r
        rep.data["r3_permutation_p"] = pval

    # (c) 표시 오차
    rep.say()
    within = sum(1 for t in kept if abs(t["display_error_ms"]) <= DISPLAY_TOLERANCE_MS)
    worst = max(abs(t["display_error_ms"]) for t in kept)
    early = [t for t in kept if t["display_error_ms"] < -DISPLAY_TOLERANCE_MS]
    rep.check(f"표시 오차 ≤ {DISPLAY_TOLERANCE_MS}ms", within == len(kept),
              f"{within}/{len(kept)} 턴, 최대 {worst}ms")
    rep.check("마감 전에 표시된 턴 없음", not early,
              f"{len(early)}턴이 마감보다 일찍 표시됨 — 조작 실패" if early else "")

    overrun = [t for t in kept if t["llm_latency_ms"] > t["target_delay_ms"]]
    pct = 100 * len(overrun) / len(kept)
    rep.say(f"  LLM 초과 턴(생성 시간 > 목표 지연): {len(overrun)}/{len(kept)} ({pct:.1f}%)")
    for d in DEPTHS:
        rows = [t for t in kept if t.get("depth") == d]
        if rows:
            o = sum(1 for t in rows if t["llm_latency_ms"] > t["target_delay_ms"])
            rep.say(f"    {DEPTH_KO[d]:<4} 초과 {o:>3}/{len(rows):<4}"
                    f"  LLM 생성 M {fmt(mean([t['llm_latency_ms'] for t in rows]), 0)}ms")
    rep.check("LLM 초과 턴 < 5%", pct < 5.0, f"{pct:.1f}%")
    rep.data["overrun_pct"] = round(pct, 2)

    rep.say()
    rep.say("  턴 번호별 LLM 생성 시간 (이력이 쌓이면 늘어난다)")
    by_turn = defaultdict(list)
    for t in kept:
        if t.get("turn_index"):
            by_turn[int(t["turn_index"])].append(t["llm_latency_ms"])
    for ti in sorted(by_turn):
        lat = by_turn[ti]
        rep.say(f"    턴 {ti}  M {fmt(mean(lat), 0):>7}ms  max {max(lat):>7}ms  n {len(lat)}")

    # ── 3 · 응답 내용 통제 ──
    rep.head("3 · 응답 내용 통제")
    rep.say("  ① AI 응답 길이 (깊이 구성이 같으므로 조건 간 차이가 없어야 함)")
    lens = {}
    for c in CONDITIONS:
        sub = by_cond[c]
        if sub:
            L = [t["ai_response_chars"] for t in sub]
            lens[c] = L
            rep.say(f"    {CONDITION_KO[c]:<4} M {fmt(mean(L))}자  SD {fmt(sd(L))}  n {len(L)}")
    if len(lens) >= 2:
        ms = [mean(v) for v in lens.values()]
        pooled = mean([sd(v) for v in lens.values()])
        raw = max(ms) - min(ms)
        spread = raw / pooled if pooled else 0.0
        # SD가 매우 작으면 표준화 차이가 과장된다. 원 단위로도 무시할 만하면 통과.
        trivial = raw < 0.05 * mean(ms)
        rep.check("조건 간 평균 응답 길이 차 < 0.3 SD", spread < 0.3 or trivial,
                  f"최대차 {fmt(raw)}자 = {fmt(spread, 2)} SD"
                  + ("  (원 단위로는 평균의 5% 미만 — 무시)" if trivial and spread >= 0.3 else ""))

    rng = random.Random(20260401)
    truncated = [t for t in kept if str(t.get("finish_reason", "")).lower() == "length"]
    if any("finish_reason" in t for t in kept):
        rep.check("잘린 응답 없음 (finish_reason != length)", not truncated,
                  f"{len(truncated)}턴이 잘렸다 — max_tokens를 올릴 것" if truncated else "")
    else:
        rep.note("finish_reason 로깅", False, "필드 없음 — 잘림을 감지할 수 없다")

    rep.say()
    rep.say("  ②③ AI 응답 유사도 (문자 바이그램 Jaccard — 파일럿 점검용)")
    within_cond = {}
    for c in CONDITIONS:
        sub = by_cond[c]
        if len(sub) >= 2:
            within_cond[c] = mean_pairwise_similarity([t["ai_response_text"] for t in sub], rng=rng)
            rep.say(f"    조건 내 {CONDITION_KO[c]:<4} {fmt(within_cond[c], 3)}")
    by_depth = defaultdict(list)
    for t in kept:
        by_depth[str(t.get("depth", "?"))].append(t["ai_response_text"])
    if all(d in by_depth for d in DEPTHS):
        within = {d: mean_pairwise_similarity(by_depth[d], rng=rng) for d in DEPTHS}
        cross = mean_cross_similarity(by_depth["deep"], by_depth["shallow"], rng=rng)
        rep.say("    깊이 내 " + " / ".join(f"{DEPTH_KO[d]} {fmt(within[d], 3)}" for d in DEPTHS))
        rep.say(f"    깊이 간 깊음↔얕음 {fmt(cross, 3)}")
        rep.check("깊음과 얕음의 응답이 서로 다르다 (깊이 조작 성공)",
                  (not math.isnan(cross)) and cross < min(within["deep"], within["shallow"]),
                  f"깊음↔얕음 {fmt(cross, 3)}")
        lens = {d: mean([len(x) for x in by_depth[d]]) for d in DEPTHS}
        rep.say(f"    깊이별 응답 길이 M: " + " · ".join(f"{DEPTH_KO[d]} {fmt(lens[d], 0)}자" for d in DEPTHS))
        rep.check("깊을수록 응답이 길다", lens["shallow"] < lens["medium"] < lens["deep"],
                  f"{fmt(lens['shallow'],0)} / {fmt(lens['medium'],0)} / {fmt(lens['deep'],0)}자")
    rep.say("    ※ 논문 수치는 문장 임베딩 코사인 유사도로 다시 계산할 것")

    rep.say()
    rep.say("  ⑤ 프롬프트 규칙 위반 (금지어 포함 — 목표 0건)")
    if response_rules is None:
        rep.note("규칙 검사기 사용 가능", False, "prompts/response_rules.py를 불러오지 못했다")
    else:
        variant = next((t["empathy_variant"] for t in kept if t.get("empathy_variant")), "B")
        viol = defaultdict(int)
        bad_turns = 0
        for t in kept:
            vs = response_rules.check(t.get("ai_response_text", ""),
                                      context=str(t.get("context", "a")),
                                      empathy_variant=str(variant),
                                      expect_safety=bool(t.get("safety_flag")))
            if vs:
                bad_turns += 1
                for x in vs:
                    viol[x["rule"]] += 1
        rep.say(f"    맥락 B 공감 변형: {variant}")
        if viol:
            for rule, cnt in sorted(viol.items(), key=lambda kv: -kv[1]):
                rep.say(f"    {rule:<20} {cnt:>5}건")
        rep.check("프롬프트 규칙 위반 0건", bad_turns == 0,
                  f"{bad_turns}/{len(kept)}턴에서 위반 — "
                  f"자세히: python3 prompts/response_rules.py --jsonl <로그>" if bad_turns else "")
        rep.data["rule_violation_turns"] = bad_turns
        rep.data["rule_violations"] = dict(viol)

    rep.say()
    rep.say("  ④ 사용자 입력 길이 · 응답 지연 (조건 간 달라도 됨 — 결과일 수 있음)")
    rep.say(f"    {'조건':<6}{'입력 길이 M(SD)':>20}{'다음 입력까지 M(SD) ms':>28}")
    for c in CONDITIONS:
        sub = by_cond[c]
        if not sub:
            continue
        ic = [t["user_input_chars"] for t in sub]
        ul = [t["user_response_latency_ms"] for t in sub if t["user_response_latency_ms"] is not None]
        ul_s = f"{fmt(mean(ul), 0)} ({fmt(sd(ul), 0)})" if ul else "n/a"
        rep.say(f"    {CONDITION_KO[c]:<6}{fmt(mean(ic)) + ' (' + fmt(sd(ic)) + ')':>20}{ul_s:>28}")

    # ── 4 · 프롬프트 동일성 ──
    rep.head("4 · 프롬프트 동일성")
    bases = {t.get("base_prompt_sha256") for t in kept if t.get("base_prompt_sha256")}
    versions = {t.get("prompt_version") for t in kept if t.get("prompt_version")}
    if not bases:
        rep.check("base_prompt_sha256 로깅", False, "필드 없음 — 앱에 추가할 것")
    else:
        for h in sorted(bases):
            rep.say(f"    기반 {h}")
        rep.check("기반 프롬프트가 R1/R2/R3에서 동일", len(bases) == 1,
                  "★ 조건이 프롬프트를 바꿨다 — 즉시 중단 사유" if len(bases) > 1 else "")

    by_depth_hash = defaultdict(set)
    for t in kept:
        if t.get("prompt_sha256"):
            by_depth_hash[t.get("depth")].add(t["prompt_sha256"])
    for d in DEPTHS:
        if by_depth_hash.get(d):
            rep.say(f"    {DEPTH_KO[d]:<4} 해시 {len(by_depth_hash[d])}종")
    stable = all(len(v) == 1 for v in by_depth_hash.values())
    distinct = len({next(iter(v)) for v in by_depth_hash.values() if len(v) == 1}) == len(by_depth_hash)
    rep.check("깊이 안에서 프롬프트가 일정", stable)
    rep.check("세 깊이의 프롬프트가 서로 다름", distinct and len(by_depth_hash) == 3)
    rep.check("prompt_version 단일", len(versions) <= 1, f"{sorted(versions) or '없음'}")

    return rep


# ─────────────────────────────── 합성 데이터 ───────────────────────────────

def make_demo(broken=False, seed=7):
    """합성 로그. broken=True면 'LLM 응답이 온 뒤부터 D초 대기'하는 잘못된 구현.

    지연 배치 설계: 참가자마다 대화 3개(R1/R2/R3) × 9턴(깊음3·보통3·얕음3).
    """
    rng = random.Random(seed)
    base_hash = "b" * 64
    depth_hash = {"deep": "d" * 64, "medium": "m" * 64, "shallow": "s" * 64}
    resp_len = {"deep": 110, "medium": 62, "shallow": 28}
    out, now = [], 1_800_000_000_000
    for p in range(1, 25):
        pid = f"P{p:02d}"
        order = CONDITIONS[:]
        rng.shuffle(order)
        for conv, cond in enumerate(order, 1):
            depths = ["deep"] * 3 + ["medium"] * 3 + ["shallow"] * 3
            rng.shuffle(depths)
            if cond == "R1":
                delays = [TARGET_MS[d] for d in depths]
            elif cond == "R2":
                flip = {"deep": TARGET_MS["shallow"], "medium": TARGET_MS["medium"],
                        "shallow": TARGET_MS["deep"]}
                delays = [flip[d] for d in depths]
            else:
                delays = [TARGET_MS[d] for d in ("deep", "medium", "shallow") for _ in range(3)]
                rng.shuffle(delays)
            for turn, (depth, target) in enumerate(zip(depths, delays), 1):
                chars = max(5, int(rng.gauss(70, 40)))
                start = now
                submit = start + chars * rng.randint(90, 160)
                req = submit + rng.randint(20, 60)
                llm = 240 + chars * rng.randint(1, 2) + rng.randint(0, 150)
                resp = req + llm
                display = (resp + target) if broken else max(resp, submit + target)
                nxt = display + rng.randint(1200, 4200)
                n = resp_len[depth] + rng.randint(-8, 8)
                text = ("그러셨군요. " if depth != "deep" else "말씀하신 그 부분이 마음에 남습니다. ")
                text += "".join(rng.choice("가나다라마바사아자차") for _ in range(n))
                text += " 어떤 부분이 제일 걸리셨나요?"
                out.append({
                    "session_id": f"{pid}-1", "participant_id": pid,
                    "group": "adhd" if p % 2 else "comparison",
                    "conversation_index": conv, "condition": cond, "depth": depth,
                    "turn_index": turn, "practice": False,
                    "user_input_start_ts": start, "user_input_submit_ts": submit,
                    "user_input_text": "가" * chars, "user_input_chars": chars,
                    "target_delay_ms": target,
                    "llm_request_ts": req, "llm_response_ts": resp, "display_ts": display,
                    "ai_response_text": text, "ai_response_chars": len(text),
                    "next_input_start_ts": nxt if turn < 9 else None,
                    "finish_reason": "stop",
                    "safety_flag": False, "manipulation_ok": resp <= submit + target,
                    "prompt_version": "v0.3", "base_prompt_sha256": base_hash,
                    "prompt_sha256": depth_hash[depth],
                    "model": "demo-model", "temperature": 0.6, "max_tokens": 400,
                    "delay_scale": 1.0,
                })
                now = nxt + rng.randint(500, 1500)
    return out


# ─────────────────────────────── main ───────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="*", help="JSONL 로그 (글롭 가능)")
    ap.add_argument("--demo", action="store_true", help="올바른 구현의 합성 데이터로 실행")
    ap.add_argument("--demo-broken", action="store_true",
                    help="'LLM 응답 후 D초 대기'하는 잘못된 구현의 합성 데이터로 실행")
    ap.add_argument("--equivalence-bound", type=float, default=DEFAULT_EQUIV_BOUND,
                    help=f"독립성 등가 한계 |r| (기본 {DEFAULT_EQUIV_BOUND})")
    ap.add_argument("--json", action="store_true", help="JSON으로 출력")
    args = ap.parse_args()

    problems = []
    if args.demo or args.demo_broken:
        turns = make_demo(broken=args.demo_broken)
        banner = "합성 데이터 — 잘못된 구현" if args.demo_broken else "합성 데이터 — 올바른 구현"
    elif args.logs:
        paths = sorted({p for pat in args.logs for p in glob.glob(pat)} or set())
        if not paths:
            print("로그 파일을 찾지 못했습니다.", file=sys.stderr)
            return 2
        turns, problems = load(paths)
        banner = f"{len(paths)}개 파일"
    else:
        ap.print_help()
        return 2

    for t in turns:
        derive(t)
    rep = run(turns, args.equivalence_bound)

    if args.json:
        json.dump({"source": banner, "checks": rep.checks, "notes": rep.notes,
                   "summary": rep.data, "problems": problems},
                  sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        print("=" * 74)
        print(f"조작 점검 · {banner}")
        print("=" * 74)
        if problems:
            print("\n[로그 문제]")
            for p in problems[:20]:
                print(f"  ! {p}")
            if len(problems) > 20:
                print(f"  … 외 {len(problems) - 20}건")
        print("\n".join(rep.lines))
        failed = [c for c in rep.checks if not c["pass"]]
        open_notes = [c for c in rep.notes if not c["pass"]]
        print()
        print("=" * 74)
        print(f"검사 {len(rep.checks)}건 중 통과 {len(rep.checks) - len(failed)} / 실패 {len(failed)}"
              f"   (참고 지표 {len(rep.notes)}건 중 미달 {len(open_notes)})")
        for c in failed:
            print(f"  ✗ {c['name']}" + (f"  — {c['detail']}" if c["detail"] else ""))
        for c in open_notes:
            print(f"  △ {c['name']}" + (f"  — {c['detail']}" if c["detail"] else ""))
        print("=" * 74)

    return 1 if any(not c["pass"] for c in rep.checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
