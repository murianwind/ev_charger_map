#!/usr/bin/env python3
"""
일일 지역 갱신 진입점.

한국환경공단 getChargerInfo API로 전국을 한 번에 스캔하면 하루 API 호출
한도(1,000건)를 계속 위태롭게 넘나들게 된다. 그래서 16개 시도를 7일에
나눠 담당한다(gov_charger_api.DAY_ZCODE_GROUPS). 매일 그날 담당 지역만
새로 받아 기존 데이터에서 그 지역만 교체하고, 나머지 지역은 지난 회차
데이터를 그대로 유지한다 — 일주일이면 전국이 한 바퀴 갱신된다.

처음 배포하거나 데이터를 통째로 새로 채워넣고 싶을 때는 환경변수
FULL_SEED=1로 실행한다 (모든 지역을 한 번에 순회 — 호출량이 크므로 수동
1회용, 평소 자동 실행에는 절대 안 쓴다).

지역 단위로 "완전히 받은 지역만" 교체한다. 응답이 비었거나 중간에 끊긴
지역, 한도초과로 못 받은 지역은 기존 데이터를 그대로 두고 메타의
pendingRegions(재시도 대기)에 기록한 뒤 종료 코드 1로 끝난다 — 워크플로
실행이 빨간색으로 보이게 하기 위함이다. 대기 지역은 다음 정기 실행이 함께
받고, 그 전에도 환경변수 RETRY_PENDING=1 실행(홈 스케줄러가 매시간
dispatch)이 대기 지역만 골라 다시 받는다.

테슬라 슈퍼차저는 여기서 조회하지 않는다. fetch_tesla.py가 따로 갱신해둔
스냅샷(data/tesla_snapshot.json)을 지도에 합치기만 한다.
"""
import os
import sys
from datetime import datetime

from geojson_store import (
    GEOJSON_PATH,
    clear_pending,
    count_region,
    load_geojson,
    mark_pending,
    pending_regions,
    prune_orphaned,
    replace_regions,
    replace_tesla,
    save_geojson,
)
from gov_charger_api import ALL_ZCODES, DAY_ZCODE_GROUPS, ZCODE_NAMES, fetch_region
from kst_time import KST, now_kst_iso
from tesla_snapshot import load_snapshot


def get_service_key():
    key = os.environ.get("EV_SERVICE_KEY")
    if not key:
        print("EV_SERVICE_KEY 환경변수가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)
    return key


WEEKDAY_LABELS = ["월", "화", "수", "목", "금", "토", "일"]  # datetime.weekday() 순서와 동일


def scheduled_zcodes():
    """이번 실행이 원래 담당하는 지역(전체 시드 / 지정 요일 / 오늘 요일)."""
    if os.environ.get("FULL_SEED") == "1":
        print("FULL_SEED=1: 전체 지역을 한 번에 갱신합니다 (호출량 큼)")
        return [z for group in DAY_ZCODE_GROUPS for z in group]

    # 수동 실행 시 "오늘 담당 지역"이 아니라 특정 요일의 담당 지역을 직접
    # 고를 수 있게 한다(예: 화요일 갱신이 실패해서 놓쳤을 때, 목요일에
    # 수동으로 "화" 담당 지역만 다시 돌리고 싶은 경우). 자동 실행(cron이
    # 매일 새벽 dispatch)이나 값이 "오늘"이면 실제 오늘 요일 그대로 쓴다.
    # 워크플로 드롭다운 옵션엔 "월(경기·충남·제주)"처럼 지역명이 같이
    # 붙어있어서(어느 지역인지 보이게), 맨 앞 글자만 요일로 파싱한다.
    region_day = (os.environ.get("REGION_DAY") or "").strip()
    if region_day and region_day != "오늘":
        weekday_char = region_day[0]
        if weekday_char not in WEEKDAY_LABELS:
            print(f"경고: REGION_DAY={region_day!r}를 알 수 없어 오늘 담당 지역으로 대체합니다.")
        else:
            weekday = WEEKDAY_LABELS.index(weekday_char)
            print(f"REGION_DAY={region_day}: 해당 요일의 담당 지역을 갱신합니다")
            return DAY_ZCODE_GROUPS[weekday]

    weekday = datetime.now(KST).weekday()  # 월=0 ... 일=6
    return DAY_ZCODE_GROUPS[weekday]


def select_zcodes(pending):
    """이번 실행에서 받을 지역. 재시도 실행(RETRY_PENDING=1)은 대기 지역만,
    그 외에는 담당 지역에 대기 지역을 더한다."""
    if os.environ.get("RETRY_PENDING") == "1":
        return list(pending)
    scheduled = scheduled_zcodes()
    return scheduled + [z for z in pending if z not in scheduled]


def apply_region_result(geojson, result, now):
    """수집 결과를 geojson에 반영하고 (갱신된 zcode, 실패한 zcode, 갱신 개소 수)를 반환한다."""
    updated, failed, station_count = [], list(result.failed_zcodes), 0
    region_updates = geojson.setdefault("meta", {}).setdefault("regionUpdates", {})

    for zcode, stations in result.stations_by_zcode.items():
        name = ZCODE_NAMES.get(zcode, zcode)
        existing_count = count_region(geojson, zcode)
        if not stations and existing_count > 0:
            # 수집은 끝까지 됐는데 필터를 통과한 충전소가 0개 — 원래 데이터가
            # 있던 지역에선 정상적인 결과라기엔 의심스러워서 교체하지 않는다.
            print(f"경고: {name}({zcode}) 결과가 0개소인데 기존에 {existing_count}개소가 있었습니다 — 교체하지 않습니다.")
            failed.append(zcode)
            continue
        replace_regions(geojson, [zcode], stations)
        # 지역(zcode)별로 "실제로 언제 갱신됐는지"를 누적 기록한다 —
        # 프론트엔드가 지역별 상세를 보여주는 데 쓴다(이번에 안 건드린
        # 지역은 그대로 유지됨).
        region_updates[zcode] = {"name": name, "time": now, "count": len(stations)}
        updated.append(zcode)
        station_count += len(stations)

    clear_pending(geojson, updated)
    mark_pending(geojson, failed, now)
    return updated, failed, station_count


def update_status(result, updated, failed):
    if not failed:
        return "ok"
    if result.quota_exceeded and not updated:
        return "skipped_quota_exceeded"
    return "partial_failure" if updated else "failed"


def merge_tesla_snapshot(geojson):
    """스냅샷의 슈퍼차저 목록으로 지도의 슈퍼차저를 교체하고 개소 수를 반환한다.
    스냅샷이 없거나 비어 있으면 지도에 있던 것을 그대로 둔다(None 반환)."""
    snapshot = load_snapshot()
    if not snapshot or not snapshot.get("stations"):
        print("슈퍼차저 스냅샷이 없어 지도의 슈퍼차저를 그대로 둡니다.")
        return None
    replace_tesla(geojson, snapshot["stations"])
    geojson.setdefault("meta", {})["lastTeslaUpdate"] = {
        "time": snapshot.get("updatedAt"),
        "count": len(snapshot["stations"]),
    }
    return len(snapshot["stations"])


def run(service_key):
    """지역 갱신을 실행한다. 받으려던 지역을 전부 갱신했으면 True."""
    geojson = load_geojson()

    # zcode가 유효한 16개 지역 코드 어디에도 없는 오래된/잘못된 항목(예: 초기
    # 개발 중 넣어뒀던 샘플 데이터)을 정리한다. 이런 항목은 어느 요일
    # 로테이션에도 안 걸려서 replace_regions만으로는 영원히 안 지워진다.
    geojson, pruned_count = prune_orphaned(geojson, ALL_ZCODES)
    if pruned_count:
        print(f"정리: 유효 지역 코드가 없는 오래된 항목 {pruned_count}건 제거")

    zcodes = select_zcodes([z for z in pending_regions(geojson) if z in ALL_ZCODES])
    if not zcodes:
        print("재시도 대기 중인 지역이 없습니다 — 할 일 없음.")
        return True
    print(f"이번 실행 대상 지역(zcode): {zcodes}")

    result = fetch_region(service_key, zcodes)
    now = now_kst_iso()
    updated, failed, station_count = apply_region_result(geojson, result, now)

    last_update = {
        "time": now,
        "zcodes": zcodes,
        "count": station_count,
        "status": update_status(result, updated, failed),
    }
    if failed:
        last_update["failedZcodes"] = failed
    geojson["meta"]["lastFullUpdate"] = last_update

    tesla_count = merge_tesla_snapshot(geojson)
    save_geojson(geojson)

    tesla_text = f"{tesla_count}개소" if tesla_count is not None else "변경 없음"
    print(
        f"완료: 지역 {updated} {station_count}개소 갱신, 테슬라 {tesla_text}, "
        f"전체 {len(geojson['features'])}개소 저장 → {GEOJSON_PATH}"
    )
    if failed:
        names = ", ".join(f"{ZCODE_NAMES.get(z, z)}({z})" for z in failed)
        print(f"실패: {names} — 기존 데이터를 유지하고 재시도 대기로 남깁니다.", file=sys.stderr)
    return not failed


if __name__ == "__main__":
    try:
        succeeded = run(get_service_key())
    except Exception as err:  # 최상위 예외 캐치: 트레이스백 대신 명확한 메시지로 종료
        print(f"오류: 지역 갱신 실패 — {err}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if succeeded else 1)
