#!/usr/bin/env bash
# 워크플로 공용: 지정한 파일의 변경을 커밋해서 main에 push한다.
#
# 사용법: commit_and_push.sh "<커밋 메시지>" "<충돌 시 다시 돌릴 명령>" <파일>...
#
# push가 거부되면(그 사이 다른 실행이 main에 먼저 push한 경우) 먼저
# rebase로 우리 커밋을 최신 main 위에 올려 다시 push한다 — 서로 다른 파일을
# 고친 경우엔 이걸로 끝나고, API를 다시 호출할 필요가 없다.
# 같은 파일을 고쳐 rebase가 충돌할 때만, 최신 main에서 "다시 돌릴 명령"을
# 처음부터 실행해 새로 커밋한다(우리 파일을 그대로 밀어넣으면 다른 실행이
# 반영한 내용을 덮어써버리기 때문).
#
# 종료 코드: push까지 끝났으면(또는 변경이 없으면) 0. 다시 돌린 명령이
# 실패했으면 결과는 push하되 그 명령의 종료 코드를 그대로 돌려준다.
set -uo pipefail

MESSAGE="$1"
RERUN_COMMAND="$2"
shift 2
FILES=("$@")
MAX_PUSH_ATTEMPTS=3

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

# 변경이 있으면 커밋한다. 커밋할 게 없으면 1을 반환한다.
commit_changes() {
  git add -- "${FILES[@]}"
  if git diff --cached --quiet; then
    return 1
  fi
  git commit -q -m "$MESSAGE"
}

if ! commit_changes; then
  echo "변경 없음, 커밋 생략"
  exit 0
fi

rerun_status=0
for attempt in $(seq 1 "$MAX_PUSH_ATTEMPTS"); do
  if git push origin HEAD:main; then
    exit "$rerun_status"
  fi
  echo "push 거부됨(${attempt}/${MAX_PUSH_ATTEMPTS}) — 최신 main 위로 rebase 후 다시 시도합니다."

  if git pull --rebase origin main; then
    continue
  fi

  echo "rebase 충돌 — 최신 main에서 처음부터 다시 실행합니다: ${RERUN_COMMAND}"
  git rebase --abort || true
  git fetch origin main
  git reset --hard origin/main
  bash -c "$RERUN_COMMAND"
  rerun_status=$?
  if ! commit_changes; then
    echo "다시 실행한 결과 변경 없음, 커밋 생략"
    exit "$rerun_status"
  fi
done

echo "오류: ${MAX_PUSH_ATTEMPTS}번 시도했지만 push하지 못했습니다." >&2
exit 1
