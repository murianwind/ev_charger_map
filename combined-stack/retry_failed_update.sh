#!/bin/bash
# 직전 지역 갱신(update-chargers.yml)이 실패로 끝났을 때만 재시도 실행을
# dispatch한다. 재시도 실행은 대기 지역이 없으면 아무 일도 안 하고 끝나지만,
# 그래도 Actions 목록에는 실행이 하나씩 쌓인다 — 평소에 그런 빈 실행이
# 생기지 않도록, 실패했을 때만 요청을 보낸다.
#
# 실행 결과를 조회하지 못하면(네트워크 문제 등) 실패를 놓치지 않도록 그냥
# 요청을 보낸다 — 대기 지역이 없으면 빈 실행 하나로 끝날 뿐이다.
set -e

: "${ACCESS_TOKEN:?ACCESS_TOKEN 환경변수가 필요합니다}"
: "${REPO_URL:?REPO_URL 환경변수가 필요합니다}"

WORKFLOW_FILE="update-chargers.yml"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_PATH="${REPO_URL#https://github.com/}"

# 최근에 끝난 실행 몇 개를 최신순으로 받는다. 대기 중에 취소된 실행
# (cancelled/skipped)은 실제로 돌지 않은 것이라 건너뛰고, 실제로 돈 가장
# 최근 실행의 결과를 본다.
RESPONSE=$(curl -s --max-time 30 -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${REPO_PATH}/actions/workflows/${WORKFLOW_FILE}/runs?status=completed&per_page=10") || RESPONSE=""

LAST_CONCLUSION=$(echo "$RESPONSE" | jq -r '
  [.workflow_runs[].conclusion | select(. != "cancelled" and . != "skipped")] | first // "unknown"
' 2> /dev/null) || LAST_CONCLUSION="unknown"

if [ "$LAST_CONCLUSION" = "success" ]; then
  echo "[retry] 직전 지역 갱신 성공 — 재시도 요청 생략"
  exit 0
fi

echo "[retry] 직전 지역 갱신 결과: ${LAST_CONCLUSION:-unknown} — 재시도를 요청합니다"
exec "${SCRIPT_DIR}/trigger_workflow.sh" "$WORKFLOW_FILE" '{"retry_pending":"true"}'
