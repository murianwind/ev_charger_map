FROM derskythe/github-runner:latest
# myoung34/github-runner에서 여기로 교체(2026-08 기준). 같은 계보(포크)라
# 환경변수 인터페이스(ACCESS_TOKEN, REPO_URL, RUNNER_NAME 등)는 동일하지만,
# entrypoint.sh 경로가 절대경로(/entrypoint.sh)가 아니라 WorkingDir 기준
# 상대경로(./entrypoint.sh)라서 wrapper-entrypoint.sh도 같이 고쳤다.
# 압축 이미지 크기: myoung34 807MB -> derskythe 488MB (약 40% 감소).

# python3/git은 이제 이 컨테이너가 아니라 GitHub Actions 워크플로(러너가
# 받아서 실행) 쪽에서 setup-python/checkout이 알아서 준비해준다.
# 이 컨테이너는 cron으로 워크플로를 dispatch하고 러너 상태를 확인만 하면
# 되므로 curl(API 호출)과 jq(JSON 파싱)만 있으면 된다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron curl jq \
    && rm -rf /var/lib/apt/lists/*

COPY crontab /etc/cron.d/ev-charger-cron
COPY trigger_workflow.sh /repo_scripts/trigger_workflow.sh
COPY check_runner_health.sh /repo_scripts/check_runner_health.sh
COPY retry_failed_update.sh /repo_scripts/retry_failed_update.sh
COPY wrapper-entrypoint.sh /wrapper-entrypoint.sh

RUN chmod +x /repo_scripts/trigger_workflow.sh /repo_scripts/check_runner_health.sh /repo_scripts/retry_failed_update.sh /wrapper-entrypoint.sh \
    && chmod 0644 /etc/cron.d/ev-charger-cron \
    && touch /var/log/cron.log

ENTRYPOINT ["/wrapper-entrypoint.sh"]
