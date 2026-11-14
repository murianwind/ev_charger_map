#!/bin/bash
# GitHub Actions workflow_dispatch를 API로 호출한다. 실제 작업(스크립트 실행,
# git commit/push)은 이 컨테이너가 아니라 self-hosted 러너로 그 워크플로를
# 받아서 처리한다 — 그래서 매 실행이 GitHub Actions 탭에 정상적인 실행
# 기록(로그, 성공/실패, 소요시간)으로 남는다.
#
# 사용법: trigger_workflow.sh <workflow-file.yml> [inputs JSON]
#   예) trigger_workflow.sh update-chargers.yml '{"retry_pending":"true"}'
set -e

: "${ACCESS_TOKEN:?ACCESS_TOKEN 환경변수가 필요합니다}"
: "${REPO_URL:?REPO_URL 환경변수가 필요합니다}"

WORKFLOW_FILE="$1"
INPUTS_JSON="${2:-}"  # 워크플로 inputs (없으면 빈 객체)
[ -n "$INPUTS_JSON" ] || INPUTS_JSON='{}'
if [ -z "$WORKFLOW_FILE" ]; then
  echo "사용법: trigger_workflow.sh <workflow-file.yml> [inputs JSON]" >&2
  exit 1
fi

REPO_PATH="${REPO_URL#https://github.com/}"  # OWNER/REPO 형태로 정리

# 연결 자체가 안 되면 curl이 0이 아닌 코드로 끝난다. set -e로 조용히 죽지
# 않고 아래에서 "dispatch 실패"로 로그가 남도록 여기서는 넘어간다.
: > /tmp/trigger_response.json
HTTP_STATUS=$(curl -s --max-time 30 -o /tmp/trigger_response.json -w "%{http_code}" \
  -X POST \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${REPO_PATH}/actions/workflows/${WORKFLOW_FILE}/dispatches" \
  -d "{\"ref\":\"main\",\"inputs\":${INPUTS_JSON}}") || true

if [ "$HTTP_STATUS" = "204" ]; then
  echo "[trigger] ${WORKFLOW_FILE} dispatch 성공"
else
  echo "[trigger] ${WORKFLOW_FILE} dispatch 실패 (HTTP ${HTTP_STATUS})"
  cat /tmp/trigger_response.json
  exit 1
fi
