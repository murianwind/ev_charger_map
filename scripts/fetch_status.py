#!/usr/bin/env python3
"""
getChargerStatus는 'period'(분) 안에 상태가 바뀐 충전기만 돌려주는 델타 피드다.
그래서 이 스크립트는 전국 데이터를 통째로 다시 받지 않고, 최근 변경분만 받아
기존 docs/data/chargers.geojson에 병합한다 (호출 수를 적게 유지하기 위함).

API 문서 기준 period는 1~10(분)만 유효하다(기본값 5). 그래서 이 스크립트는
반드시 10분 이내 주기로 돌려야 한다 — 더 뜸하게 돌리면 그 사이 변경분을
놓칠 수 있다. (지역별로 나눠 돌리는 건 이 엔드포인트엔 안 맞는다: 한 지역을
자주 못 볼수록 그 지역 확인 간격이 10분을 넘어버려서 오히려 결손이 생긴다.
전체를 자주(10분 이내) 보는 지금 방식이 이 API 제약 안에서는 최선이다.)

이미 chargers.geojson에 없는 statId/chgerId(= fetch_chargers.py의 필터에
안 걸린 충전기, 또는 아직 이번 주 로테이션 차례가 안 돌아온 지역)는
그대로 무시한다.

API가 오류 응답만 주면(다시 요청해도 계속되면) 파일을 건드리지 않고 종료
코드 1로 끝난다 — "변경 0건"으로 저장하면 그 구간의 변경분을 잃은 사실이
드러나지 않기 때문이다.

일일 호출 한도를 초과하면 gov_charger_api가 다음날 자정까지 쿨다운을
걸어두고 None을 반환한다 — 이 경우 API를 더 이상 건드리지 않고, "이번엔
건너뜀"만 메타 정보에 남기고 종료한다.
"""
import os
import sys

from geojson_store import load_geojson, save_geojson
from gov_charger_api import STAT_NAMES, fetch_status_delta
from kst_time import now_kst_iso

STATUS_PERIOD_MIN = int(os.environ.get("EV_STATUS_PERIOD_MIN", "10"))


def get_service_key():
    key = os.environ.get("EV_SERVICE_KEY")
    if not key:
        print("EV_SERVICE_KEY 환경변수가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)
    return key


def build_lookup(geojson):
    """(statId, chgerId) -> charger dict 참조를 만든다 (in-place 수정용)."""
    lookup = {}
    for feature in geojson["features"]:
        stat_id = feature["properties"]["statId"]
        for charger in feature["properties"]["chargers"]:
            lookup[(stat_id, charger["chgerId"])] = charger
    return lookup


def main():
    service_key = get_service_key()
    geojson = load_geojson()
    if not geojson["features"]:
        print("chargers.geojson이 비어 있습니다. 먼저 fetch_chargers.py를 실행하세요.", file=sys.stderr)
        sys.exit(1)

    delta_items = fetch_status_delta(service_key, STATUS_PERIOD_MIN)

    if delta_items is None:
        geojson.setdefault("meta", {})["lastStatusUpdate"] = {
            "time": now_kst_iso(),
            "status": "skipped_quota_exceeded",
        }
        save_geojson(geojson)
        print("한도 초과로 상태 갱신을 건너뛰었습니다.")
        return

    lookup = build_lookup(geojson)
    updated = 0
    ignored = 0
    for item in delta_items:
        key = (item.get("statId"), item.get("chgerId"))
        charger = lookup.get(key)
        if charger is None:
            # 우리 필터 밖이거나, 아직 이번 주 로테이션 차례가 안 돌아온 지역
            ignored += 1
            continue
        charger["stat"] = item.get("stat", charger.get("stat"))
        charger["statName"] = STAT_NAMES.get(item.get("stat"), item.get("stat"))
        charger["statUpdDt"] = item.get("statUpdDt", charger.get("statUpdDt"))
        updated += 1

    geojson.setdefault("meta", {})["lastStatusUpdate"] = {
        "time": now_kst_iso(),
        "updated": updated,
        "status": "ok",
    }
    save_geojson(geojson)
    print(f"상태 변경 반영: {updated}건 / 무시: {ignored}건 — 저장 완료")


if __name__ == "__main__":
    try:
        main()
    except Exception as err:  # 최상위 예외 캐치: 트레이스백 대신 명확한 메시지로 종료
        print(f"오류: 상태 갱신 실패 — {err}", file=sys.stderr)
        sys.exit(1)
