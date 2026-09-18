#!/usr/bin/env bash
#
# app/verify.sh — CONTRACT §8 검증 파이프라인
#
#   1. 단위 검사            python3 -m unittest discover app/tests
#   2. mock 서버 기동       빈 포트 · 준비될 때까지 대기
#   3. E2E 세션 2개         홀수/짝수 참가자 → 블록 순서 두 가지를 모두 밟는다
#   4. 조작 점검            analysis/manipulation_check.py
#   5. 응답 규칙 검사       prompts/response_rules.py --jsonl
#   6. 서버 종료 · 요약 출력 (하나라도 실패하면 0이 아닌 코드로 끝난다)
#
# 사용:
#   bash app/verify.sh
#   bash app/verify.sh --delay-scale 1        # 실제 지연으로 (세션당 5분 정도)
#   bash app/verify.sh --participants "P01 P02 P05 P06"
#
# ★ 지연 배율
#   기본값 0.1은 **세 조건과 연습 지연에 똑같이** 적용된다 (app/config.py
#   scaled_delay_conditions). 로그의 delay_scale 필드에 남고 manipulation_check.py가
#   그 값으로 기대 범위를 되돌린다. 축소된 로그는 본 실험 데이터가 아니다.
#
# ★ 로그 디렉터리
#   기본 logs/verify 를 매 실행 시작에 비운다. 배율이 다른 옛 로그가 섞이면
#   manipulation_check.py가 (정당하게) 실패하기 때문이다.
#   실제 참가자 로그가 있는 logs/ 자체는 건드리지 않는다 — --log-dir 로
#   다른 경로를 넘겨도 마찬가지다. 표식 파일(.verify-scratch)이 없고
#   .jsonl이 이미 있는 디렉터리는 비우지 않고 그 자리에서 중단한다.

set -uo pipefail
set -m            # 각 단계를 독립 프로세스 그룹으로 — 정리할 때 그룹째 종료한다

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

LOG_DIR="$REPO_ROOT/logs/verify"
DELAY_SCALE="0.1"
PARTICIPANTS="P03 P04"
LATENCY_MODE="length"      # ★ 검증용 함정 — 끄지 않는다 (CONTRACT §1)
PORT=""
KEEP_SERVER_LOG=0

while [ $# -gt 0 ]; do
  case "$1" in
    --log-dir)       LOG_DIR="$2"; shift 2 ;;
    --delay-scale)   DELAY_SCALE="$2"; shift 2 ;;
    --participants)  PARTICIPANTS="$2"; shift 2 ;;
    --latency-mode)  LATENCY_MODE="$2"; shift 2 ;;
    --port)          PORT="$2"; shift 2 ;;
    --keep-server-log) KEEP_SERVER_LOG=1; shift ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
  esac
done

SERVER_LOG="$LOG_DIR/server.out"
SERVER_PID=""
CURRENT_PID=""       # 지금 돌고 있는 단계의 자식 프로세스
STEP_NAMES=()
STEP_RESULTS=()

# ── 정리: 어떤 경로로 끝나도 서버·자식 프로세스를 남기지 않는다 ──────
stop_tree() {   # stop_tree <pid> — 프로세스 그룹째 정리한다 (node → chromium 포함)
  local pid="$1"
  [ -n "$pid" ] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.2
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
  fi
  wait "$pid" 2>/dev/null
}

cleanup() {
  stop_tree "$CURRENT_PID"
  CURRENT_PID=""
  stop_tree "$SERVER_PID"
  SERVER_PID=""
}
trap cleanup EXIT
trap 'echo; echo "중단됨 — 서버를 정리합니다."; cleanup; exit 130' INT
trap 'echo; echo "종료 신호 — 서버를 정리합니다."; cleanup; exit 143' TERM

hr() { printf '%s\n' "──────────────────────────────────────────────────────────────────────"; }

record() {   # record <이름> <종료코드>
  STEP_NAMES+=("$1")
  STEP_RESULTS+=("$2")
  if [ "$2" -eq 0 ]; then
    printf '  ✓ %s\n' "$1"
  else
    printf '  ✗ %s  (종료 코드 %s)\n' "$1" "$2"
  fi
}

step() {     # step <이름> <명령...>
  local name="$1"; shift
  echo
  hr
  echo "▶ $name"
  hr
  # 자식으로 띄우고 wait 한다. 그래야 INT/TERM이 즉시 트랩으로 들어오고,
  # 돌고 있던 명령(node·python)까지 함께 정리된다.
  "$@" &
  CURRENT_PID=$!
  wait "$CURRENT_PID"
  local code=$?
  CURRENT_PID=""
  record "$name" "$code"
  return $code
}

echo "======================================================================"
echo "검증 파이프라인 · $(date '+%Y-%m-%d %H:%M:%S')"
echo "  저장소   $REPO_ROOT"
echo "  로그     $LOG_DIR   (매 실행 시작에 비웁니다)"
echo "  지연배율 $DELAY_SCALE · mock 생성 시간 모드 $LATENCY_MODE"
echo "  참가자   $PARTICIPANTS"
echo "======================================================================"

mkdir -p "$LOG_DIR" || exit 1

# ★ 비우기 전에 — 실제 참가자 로그를 지우지 않는다.
#   이 스크립트는 시작할 때 로그 디렉터리의 *.jsonl을 모두 지운다. 기본값
#   (logs/verify)이면 안전하지만 `--log-dir logs` 처럼 실제 데이터가 있는
#   경로를 넘기면 IRB 대상 참가자 데이터가 통째로 사라진다. 되돌릴 수 없고
#   .gitignore 때문에 저장소에도 사본이 없다.
#   그래서 "verify가 만든 디렉터리"라는 표식이 있을 때만 비운다.
MARKER="$LOG_DIR/.verify-scratch"
DEFAULT_LOG_DIR="$REPO_ROOT/logs/verify"
LOG_DIR_ABS="$(cd "$LOG_DIR" && pwd)"
if [ "$LOG_DIR_ABS" = "$DEFAULT_LOG_DIR" ] && [ ! -e "$MARKER" ]; then
  : > "$MARKER"                      # 기본 디렉터리는 언제나 verify 전용이다
fi
if [ ! -e "$MARKER" ] && ls "$LOG_DIR"/*.jsonl >/dev/null 2>&1; then
  echo "중단: $LOG_DIR_ABS 에 verify가 만들지 않은 .jsonl이 있습니다." >&2
  echo "  이 스크립트는 시작할 때 로그 디렉터리를 비웁니다. 실제 참가자 로그를" >&2
  echo "  지우지 않기 위해, 표식($MARKER)이 있는 전용 디렉터리가 아니면 비우지" >&2
  echo "  않습니다. 비어 있는 디렉터리를 --log-dir 로 지정하세요." >&2
  exit 2
fi
printf '%s\n' "app/verify.sh 전용 — 매 실행 시작에 *.jsonl을 비웁니다." > "$MARKER"
rm -f "$LOG_DIR"/*.jsonl "$LOG_DIR"/*.jsonl.tmp 2>/dev/null

# ── 1. 단위 검사 ─────────────────────────────────────────────────────
step "1. 단위 검사 (app/tests)" \
  python3 -m unittest discover app/tests -p 'test_*.py'

# ── 2. mock 서버 ─────────────────────────────────────────────────────
if [ -z "$PORT" ]; then
  PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
fi
BASE_URL="http://127.0.0.1:$PORT"

start_server() {
  python3 app/server.py \
    --port "$PORT" \
    --provider mock \
    --log-dir "$LOG_DIR" \
    --mock-latency-mode "$LATENCY_MODE" \
    --delay-scale "$DELAY_SCALE" > "$SERVER_LOG" 2>&1 &
  SERVER_PID=$!

  local i=0
  while [ $i -lt 100 ]; do                      # 최대 20초 대기
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "서버가 죽었습니다. 출력:" >&2
      sed 's/^/    /' "$SERVER_LOG" >&2
      return 1
    fi
    if python3 - "$BASE_URL" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1] + "/api/health", timeout=1) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
    then
      echo "  준비 완료: $BASE_URL  (pid $SERVER_PID)"
      return 0
    fi
    i=$((i + 1))
    sleep 0.2
  done
  echo "서버가 20초 안에 준비되지 않았습니다." >&2
  sed 's/^/    /' "$SERVER_LOG" >&2
  return 1
}

echo
hr
echo "▶ 2. mock 서버 기동 ($BASE_URL)"
hr
start_server            # ★ SERVER_PID를 부모 셸에 남겨야 하므로 자식으로 띄우지 않는다
SERVER_OK=$?
record "2. mock 서버 기동 ($BASE_URL)" "$SERVER_OK"

# ── 3. E2E 세션 ──────────────────────────────────────────────────────
run_e2e() {
  if ! command -v node >/dev/null 2>&1; then
    echo "node를 찾을 수 없습니다 — E2E를 돌릴 수 없습니다." >&2
    return 127
  fi
  if [ -z "${NODE_PATH:-}" ]; then
    NODE_PATH="$(npm root -g 2>/dev/null)"
    export NODE_PATH
  fi
  local pid="$1" group="$2"
  node app/tests/e2e.js \
    --base-url "$BASE_URL" \
    --participant "$pid" \
    --group "$group" \
    --log-dir "$LOG_DIR"
}

if [ "$SERVER_OK" -eq 0 ]; then
  index=0
  for pid in $PARTICIPANTS; do
    # 홀수 참가자는 맥락 A 먼저, 짝수는 B 먼저 (CONTRACT §4) — 둘 다 밟는다.
    number="$(printf '%s' "$pid" | tr -cd '0-9')"
    if [ $((number % 2)) -eq 1 ]; then group="adhd"; else group="comparison"; fi
    step "3.$((index + 1)) E2E 세션 $pid ($group)" run_e2e "$pid" "$group"
    index=$((index + 1))
  done
else
  record "3. E2E 세션 (서버 없음)" 1
fi

# ── 4. 조작 점검 ─────────────────────────────────────────────────────
step "4. 조작 점검 (analysis/manipulation_check.py)" \
  python3 analysis/manipulation_check.py "$LOG_DIR"/*.turns.jsonl

# ── 5. 응답 규칙 ─────────────────────────────────────────────────────
step "5. 응답 규칙 위반 0건 (prompts/response_rules.py)" \
  python3 prompts/response_rules.py --jsonl "$LOG_DIR"/*.turns.jsonl

# ── 6. 서버 종료 · 요약 ──────────────────────────────────────────────
echo
hr
echo "▶ 6. 서버 종료 (아래 Terminated 알림은 정상입니다)"
hr
cleanup
if [ "$KEEP_SERVER_LOG" -eq 0 ] && [ -f "$SERVER_LOG" ]; then
  rm -f "$SERVER_LOG"
fi

failed=0
echo
echo "======================================================================"
echo "요약"
echo "======================================================================"
i=0
while [ $i -lt ${#STEP_NAMES[@]} ]; do
  if [ "${STEP_RESULTS[$i]}" -eq 0 ]; then
    printf '  ✓ %s\n' "${STEP_NAMES[$i]}"
  else
    printf '  ✗ %s  (종료 코드 %s)\n' "${STEP_NAMES[$i]}" "${STEP_RESULTS[$i]}"
    failed=$((failed + 1))
  fi
  i=$((i + 1))
done
echo "----------------------------------------------------------------------"
echo "  생성된 로그: $LOG_DIR"
ls -1 "$LOG_DIR"/*.turns.jsonl 2>/dev/null | sed 's/^/    /'
if [ "$DELAY_SCALE" != "1" ] && [ "$DELAY_SCALE" != "1.0" ]; then
  echo "  ⚠ 지연배율 $DELAY_SCALE — 검증용 로그입니다. 본 실험 데이터가 아닙니다."
fi
echo "======================================================================"
if [ "$failed" -eq 0 ]; then
  echo "PASS — ${#STEP_NAMES[@]}단계 모두 통과"
  exit 0
fi
echo "FAIL — ${#STEP_NAMES[@]}단계 중 $failed단계 실패"
exit 1
