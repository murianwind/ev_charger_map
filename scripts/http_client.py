"""범용 HTTP 클라이언트: curl 서브프로세스로 JSON을 받아오고,
실패 시 재시도하며, data.go.kr 특유의 한도초과 응답을 감지한다.

이 모듈은 EV 충전소나 테슬라 같은 도메인 지식을 전혀 모른다 —
어떤 JSON API든 재사용 가능한 순수 HTTP 계층이다.
"""
import json
import re
import subprocess
import time

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

MAX_RETRIES = 2
RETRY_WAIT_SEC = 3
CURL_TIMEOUT_SEC = 60

_SECRET_PARAM_PATTERN = re.compile(r"([?&](?:serviceKey|key|token)=)[^&\s]+", re.IGNORECASE)


def redact_secrets(text):
    """URL 안의 serviceKey/key/token 파라미터 값을 ***로 가린다.
    로그(특히 홈 스케줄러의 cron.log는 GitHub Actions처럼 자동 마스킹이
    안 되므로) 어디에도 실제 인증키가 그대로 찍히지 않게 하기 위함이다.
    """
    return _SECRET_PARAM_PATTERN.sub(r"\1***", text)


class QuotaExceededError(RuntimeError):
    """일일 API 호출 한도 초과. 재시도해도 소용없어서 즉시 중단시키기 위한 예외."""


class FetchError(RuntimeError):
    """반복 재시도 후에도 실패했을 때 발생하는, 민감정보가 제거된 에러."""


def fetch_json(url, quota_marker=None, extra_headers=None, timeout_sec=CURL_TIMEOUT_SEC, compressed=False):
    """curl로 url을 요청해 JSON으로 파싱한 결과를 반환한다.

    quota_marker가 주어지고 응답 본문에 그 문자열이 있으면 QuotaExceededError를
    즉시 발생시킨다(재시도 없이) — data.go.kr은 한도초과 시 dataType=JSON을
    요청해도 XML 에러 포맷을 그대로 돌려주기 때문에, 이 케이스만 따로 감지한다.

    extra_headers는 {"Authorization": "..."} 같은 추가 헤더가 필요한 API
    (예: 카카오)를 위한 것이다.

    timeout_sec는 응답이 유난히 느린 서버(예: supercharge.info)에만 길게
    준다. compressed=True면 gzip 등 압축 전송을 요청해(curl --compressed)
    큰 응답의 전송량을 줄인다.
    """
    last_err = "알 수 없는 오류"
    header_args = []
    for name, value in (extra_headers or {}).items():
        header_args += ["-H", f"{name}: {value}"]
    option_args = ["--compressed"] if compressed else []

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = subprocess.run(
                ["curl", "-s", "-A", USER_AGENT, "--max-time", str(timeout_sec), *option_args, *header_args, url],
                capture_output=True,
                timeout=timeout_sec + 5,
                check=True,
            )
            body = result.stdout.decode("utf-8")

            if quota_marker and quota_marker in body:
                raise QuotaExceededError(
                    "API 일일 호출 한도를 초과했습니다. 한도가 갱신될 때까지 기다려야 합니다."
                )

            return json.loads(body)
        except QuotaExceededError:
            raise
        except subprocess.TimeoutExpired:
            last_err = f"요청 시간 초과({timeout_sec}초)"
        except subprocess.CalledProcessError as err:
            # err에는 실행한 curl 명령어 전체(URL 포함)가 들어있어 그대로 출력하면
            # 인증키가 로그에 노출된다. 종료 코드만 남긴다.
            last_err = f"curl 종료 코드 {err.returncode}"
        except json.JSONDecodeError as err:
            last_err = f"응답을 JSON으로 해석하지 못함: {err.msg}"

        print(f"  경고: 요청 실패({attempt}/{MAX_RETRIES}): {last_err} — {redact_secrets(url)}")
        time.sleep(RETRY_WAIT_SEC)

    raise FetchError(f"반복 재시도 후에도 요청이 실패했습니다: {last_err}")
