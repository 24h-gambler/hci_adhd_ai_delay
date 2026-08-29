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

CONDITIONS = ["immediate", "medium", "long"]
CONDITION_KO = {"immediate": "즉시", "medium": "중간", "long": "긺"}
CONTEXT_KO = {"a": "A 일상", "b": "B 고민"}

# prompts/prompts.yaml의 delay_conditions와 일치해야 한다.
TARGET_RANGE_MS = {
    "immediate": (1000, 2000),
    "medium": (8000, 9000),
    "long": (16000, 20000),
}

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
    "condition", "context", "user_input_chars",
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
    t["imposed_delay_ms"] = t["display_ts"] - t["user_input_submit_ts"]
    t["llm_latency_ms"] = t["llm_response_ts"] - t["llm_request_ts"]
    t["display_error_ms"] = t["imposed_delay_ms"] - t["target_delay_ms"]
    nxt = t.get("next_input_start_ts")
    t["user_response_latency_ms"] = (nxt - t["display_ts"]) if nxt else None
    start = t.get("user_input_start_ts")
    t["typing_ms"] = (t["user_input_submit_ts"] - start) if start else None
    if "ai_response_chars" not in t:
        t["ai_response_chars"] = len(t.get("ai_response_text") or "")
    return t


def analyzable(t):
    return (
        not t.get("practice")
        and not t.get("safety_flag")
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

    rep.head("0 · 데이터")
    rep.say(f"  전체 턴 {len(turns)} / 분석 대상 {len(kept)} (연습·안전·조건외 제외 {dropped})")
    by_cond = defaultdict(list)
    for t in kept:
        by_cond[t["condition"]].append(t)
    for c in CONDITIONS:
        rep.say(f"    {CONDITION_KO[c]:<4} {len(by_cond[c]):>5} 턴")
    parts = sorted({t.get("participant_id", "?") for t in kept})
    rep.say(f"  참가자 {len(parts)}명: {', '.join(parts[:12])}{' …' if len(parts) > 12 else ''}")
    rep.data["n_turns"] = len(kept)
    rep.data["n_participants"] = len(parts)

    if not kept:
        rep.check("분석 가능한 턴 존재", False, "0턴")
        return rep

    # ── 1 · 독립성 ──
    rep.head("1 · 입력 길이 ↔ 부과 지연 독립성  ★")

    # ★ 조건 내 부분상관이 주 지표다.
    #   전체 상관은 조건 간 지연 차이(1.5s / 8.5s / 18s)가 분산을 지배하므로
    #   구현이 틀려도 0 근처로 나온다. --demo-broken 으로 확인할 수 있다.
    present = [c for c in CONDITIONS if len(by_cond[c]) >= 3]
    rx, ry = [], []
    for c in present:
        sub = by_cond[c]
        mc = mean([t["user_input_chars"] for t in sub])
        md = mean([t["imposed_delay_ms"] for t in sub])
        rx += [t["user_input_chars"] - mc for t in sub]
        ry += [t["imposed_delay_ms"] - md for t in sub]
    k = len(present)
    n_eff = max(4, len(rx) - (k - 1))          # 조건 평균 k개를 추정한 만큼 df 차감
    r = pearson(rx, ry)
    lo, hi = fisher_ci(r, n_eff)
    inside = (not math.isnan(lo)) and lo > -equiv_bound and hi < equiv_bound
    rep.say(f"  ★ 조건 내 부분상관:  r = {fmt(r, 3)}   95% CI [{fmt(lo, 3)}, {fmt(hi, 3)}]"
            f"   N = {len(rx)} (조건 {k}개 통제)")
    rep.data["within_condition_r"] = None if math.isnan(r) else round(r, 4)
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

    rep.check(f"조건 내 부분상관 |r| < {equiv_bound}",
              (not math.isnan(r)) and abs(r) < equiv_bound, f"r = {fmt(r, 3)}")
    bad = [c for c, v in per_cond_r.items() if v is not None and abs(v) >= equiv_bound]
    rep.check(f"모든 조건에서 |r| < {equiv_bound}", not bad,
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
    if (not math.isnan(r) and abs(r) >= equiv_bound) or bad:
        rep.say()
        rep.say("  ⚠ 구현을 의심할 것 (계획서 §1):")
        rep.say("    1) D를 전송 직후에 뽑는가, LLM 응답 후에 뽑는가")
        rep.say("    2) 대기 시작점 t0가 전송 시각인가")
        rep.say("    3) stream 이 꺼져 있는가")
        rep.say("    4) 표시 시각을 t0 + D 로 계산하는가")

    # ── 2 · 충실도 ──
    rep.head("2 · 조작 충실도")
    rep.say(f"  {'조건':<6}{'n':>5}{'실제 지연 M(SD) ms':>24}{'|오차| M / max ms':>22}")
    ok_range = True
    for c in CONDITIONS:
        sub = by_cond[c]
        if not sub:
            continue
        d = [t["imposed_delay_ms"] for t in sub]
        e = [abs(t["display_error_ms"]) for t in sub]
        lo_r, hi_r = TARGET_RANGE_MS[c]
        out = [t for t in sub if not (lo_r - DISPLAY_TOLERANCE_MS <= t["target_delay_ms"] <= hi_r + DISPLAY_TOLERANCE_MS)]
        ok_range = ok_range and not out
        rep.say(f"  {CONDITION_KO[c]:<6}{len(sub):>5}{fmt(mean(d), 0) + ' (' + fmt(sd(d), 0) + ')':>24}"
                f"{fmt(mean(e), 0) + ' / ' + fmt(max(e), 0):>22}")
        if out:
            rep.say(f"         ⚠ 목표 D가 설정 범위 {lo_r}~{hi_r}ms 밖인 턴 {len(out)}개")

    worst = max(abs(t["display_error_ms"]) for t in kept)
    within = sum(1 for t in kept if abs(t["display_error_ms"]) <= DISPLAY_TOLERANCE_MS)
    rep.check(f"표시 오차 ≤ {DISPLAY_TOLERANCE_MS}ms", within == len(kept),
              f"{within}/{len(kept)} 턴, 최대 오차 {worst}ms")
    rep.check("목표 D가 조건 범위 안", ok_range)

    overrun = [t for t in kept if t["llm_latency_ms"] > t["target_delay_ms"]]
    pct = 100 * len(overrun) / len(kept)
    rep.say(f"  LLM 초과 턴(생성 시간 > 목표 D): {len(overrun)}/{len(kept)} ({pct:.1f}%)")
    for c in CONDITIONS:
        sub = by_cond[c]
        if sub:
            o = sum(1 for t in sub if t["llm_latency_ms"] > t["target_delay_ms"])
            lat = [t["llm_latency_ms"] for t in sub]
            rep.say(f"    {CONDITION_KO[c]:<4} 초과 {o:>3}/{len(sub):<4}"
                    f"  LLM 생성 M {fmt(mean(lat), 0)}ms  max {max(lat)}ms")
    rep.check("LLM 초과 턴 < 5%", pct < 5.0, f"{pct:.1f}%")
    rep.data["overrun_pct"] = round(pct, 2)

    rep.say()
    rep.say("  조건 실행 가능성 (LLM 생성 시간 분포 vs 목표 지연 하한)")
    all_lat = sorted(t["llm_latency_ms"] for t in kept)
    p95 = all_lat[min(len(all_lat) - 1, int(0.95 * len(all_lat)))]
    rep.say(f"    LLM 생성 시간 p50 {all_lat[len(all_lat)//2]}ms · p95 {p95}ms · max {all_lat[-1]}ms")
    infeasible = [c for c in CONDITIONS if by_cond[c] and TARGET_RANGE_MS[c][0] < p95]
    for c in CONDITIONS:
        if by_cond[c]:
            floor = TARGET_RANGE_MS[c][0]
            mark = "✗" if floor < p95 else "✓"
            rep.say(f"    {mark} {CONDITION_KO[c]:<4} 하한 {floor}ms {'<' if floor < p95 else '≥'} p95 {p95}ms")
    rep.check("모든 조건의 목표 지연 하한 ≥ LLM 생성 p95", not infeasible,
              f"실행 불가 조건: {', '.join(CONDITION_KO[c] for c in infeasible)}" if infeasible else "")
    if infeasible:
        rep.say("    ⚠ 해당 조건은 목표 시각에 표시할 수 없다. 하한을 올리거나,")
        rep.say("      max_tokens를 낮추거나, 더 빠른 모델로 바꿔야 한다.")

    # ── 3 · 응답 내용 통제 ──
    rep.head("3 · 응답 내용 통제")
    rep.say("  ① AI 응답 길이 (조건 간 차이가 없어야 함)")
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
        spread = (max(ms) - min(ms)) / pooled if pooled else 0.0
        rep.check("조건 간 평균 응답 길이 차 < 0.3 SD", spread < 0.3,
                  f"최대차 {fmt(max(ms) - min(ms))}자 = {fmt(spread, 2)} SD")

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
    by_ctx = defaultdict(list)
    for t in kept:
        by_ctx[str(t.get("context", "?")).lower()].append(t["ai_response_text"])
    cross = float("nan")
    if "a" in by_ctx and "b" in by_ctx:
        wa = mean_pairwise_similarity(by_ctx["a"], rng=rng)
        wb = mean_pairwise_similarity(by_ctx["b"], rng=rng)
        cross = mean_cross_similarity(by_ctx["a"], by_ctx["b"], rng=rng)
        rep.say(f"    맥락 내 A {fmt(wa, 3)} / B {fmt(wb, 3)}   맥락 간 A↔B {fmt(cross, 3)}")
        rep.check("맥락 간 유사도 < 맥락 내 유사도 (맥락 조작 성공)",
                  (not math.isnan(cross)) and cross < min(wa, wb),
                  f"A↔B {fmt(cross, 3)} vs min(A,B) {fmt(min(wa, wb), 3)}")
        rep.data["similarity"] = {"within_a": wa, "within_b": wb, "cross": cross}
    rep.say("    ※ 논문 수치는 문장 임베딩 코사인 유사도로 다시 계산할 것")

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
    hashes = defaultdict(set)
    versions = set()
    for t in kept:
        h = t.get("prompt_sha256")
        if h:
            hashes[str(t.get("context", "?")).lower()].add(h)
        if t.get("prompt_version"):
            versions.add(t["prompt_version"])
    if not hashes:
        rep.say("  prompt_sha256 필드가 없어 검사를 건너뜁니다.")
        rep.check("prompt_sha256 로깅", False, "필드 없음 — 앱에 추가할 것")
    else:
        for ctx, hs in sorted(hashes.items()):
            rep.say(f"  맥락 {CONTEXT_KO.get(ctx, ctx)}: 해시 {len(hs)}종")
            for h in sorted(hs):
                rep.say(f"      {h}")
        same = all(len(hs) == 1 for hs in hashes.values())
        rep.check("맥락 내 지연 조건 3수준의 프롬프트 동일", same,
                  "" if same else "★ 조작 분리 실패 — 즉시 중단 사유")
        if len(hashes) >= 2:
            allh = [next(iter(hs)) for hs in hashes.values() if len(hs) == 1]
            rep.check("맥락 간 프롬프트 상이", len(set(allh)) == len(allh))
    rep.check("prompt_version 단일", len(versions) <= 1, f"{sorted(versions) or '없음'}")

    return rep


# ─────────────────────────────── 합성 데이터 ───────────────────────────────

def make_demo(broken=False, seed=7):
    """합성 로그. broken=True면 'LLM 응답 후부터 D초 대기'하는 잘못된 구현."""
    rng = random.Random(seed)
    sha = {"a": "a" * 64, "b": "b" * 64}
    out, now = [], 1_800_000_000_000
    for p in range(1, 25):
        pid = f"P{p:02d}"
        order = CONDITIONS * 2
        rng.shuffle(order)
        for conv, cond in enumerate(order, 1):
            ctx = "a" if conv <= 3 else "b"
            for turn in range(1, 6):
                chars = max(5, int(rng.gauss(60, 35)))
                start = now
                submit = start + chars * rng.randint(90, 160)
                lo, hi = TARGET_RANGE_MS[cond]
                target = rng.randint(lo, hi)
                req = submit + rng.randint(20, 60)
                llm = 280 + chars * rng.randint(1, 3) + rng.randint(0, 220)
                resp = req + llm
                display = (resp + target) if broken else max(resp, submit + target)
                nxt = display + rng.randint(1200, 4200) + (900 if cond == "long" else 0)
                stem = ("어떤 장면이 그랬는지 " if ctx == "a" else "그러셨군요 어떤 점이 ")
                out.append({
                    "participant_id": pid, "group": "adhd" if p % 2 else "comparison",
                    "block": 1 if ctx == "a" else 2, "conversation_index": conv,
                    "condition": cond, "context": ctx, "turn_index": turn, "practice": False,
                    "user_input_start_ts": start, "user_input_submit_ts": submit,
                    "user_input_text": "가" * chars, "user_input_chars": chars,
                    "target_delay_ms": target,
                    "llm_request_ts": req, "llm_response_ts": resp, "display_ts": display,
                    "ai_response_text": stem + "".join(rng.choice("가나다라마바사아자차") for _ in range(60)),
                    "ai_response_chars": 60 + len(stem),
                    "next_input_start_ts": nxt if turn < 5 else None,
                    "finish_reason": "stop",
                    "safety_flag": False, "manipulation_ok": resp <= submit + target,
                    "prompt_version": "v0.1", "prompt_sha256": sha[ctx],
                    "model": "demo-model", "temperature": 0.3, "max_tokens": 300,
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
