#!/bin/bash
set -e

# 원본 이미지의 entrypoint.sh는 러너 설치 폴더(WORKDIR)에서 ./config.sh처럼
# 상대경로로 실행되게 되어 있다. 지금 있는 위치(=컨테이너 시작 시 WORKDIR)를
# 기억해뒀다가, cron을 띄운 뒤 다시 그 자리로 돌아가서 원본 entrypoint를 이어받는다.
RUNNER_HOME="$(pwd)"

: "${ACCESS_TOKEN:?ACCESS_TOKEN 환경변수가 필요합니다 (러너 등록 + 워크플로 dispatch 겸용)}"
: "${REPO_URL:?REPO_URL 환경변수가 필요합니다 (예: https://github.com/Murianwind/ev_charger_map)}"
: "${RUNNER_NAME:?RUNNER_NAME 환경변수가 필요합니다 (헬스체크가 이 이름으로 러너 상태를 조회함)}"

# cron은 로그인 셸이 아니라 환경변수를 못 물려받는다. trigger_workflow.sh와
# check_runner_health.sh가 GitHub API를 호출할 때 필요한 값들을 파일로
# 내보내둔다. DISCORD_WEBHOOK_URL은 선택 사항(비어있으면 알림 생략).
{
  echo "ACCESS_TOKEN=${ACCESS_TOKEN}"
  echo "REPO_URL=${REPO_URL}"
  echo "RUNNER_NAME=${RUNNER_NAME}"
  echo "DISCORD_WEBHOOK_URL=${DISCORD_WEBHOOK_URL:-}"
} > /etc/environment

echo "[scheduler] cron 시작 (10분마다 상태 갱신 워크플로 dispatch, 매일 04:00 KST 지역 갱신 워크플로 dispatch + 04:35~09:35 매시 35분 실패 지역 재시도, 5분마다 러너 헬스체크)"
cron

echo "[runner] GitHub Actions 러너 시작"
cd "$RUNNER_HOME"
# derskythe/github-runner 베이스는 entrypoint.sh를 절대경로(/entrypoint.sh)가
# 아니라 WorkingDir(/actions-runner) 기준 상대경로로 둔다 — myoung34에서
# 옮기면서 여기만 절대경로 -> 상대경로로 바꿨다(나머지는 동일).
exec ./entrypoint.sh ./run.sh
