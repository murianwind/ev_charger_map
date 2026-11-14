#!/usr/bin/env python3
"""테슬라 슈퍼차저 스냅샷 갱신 진입점.

supercharge.info에서 한국 내 슈퍼차저 목록을 받아 data/tesla_snapshot.json을
갱신한다. 지도(chargers.geojson)는 건드리지 않는다 — 지도 반영은
fetch_chargers.py가 스냅샷을 읽어서 한다. 정부 API가 필요 없어서 집 러너가
아니라 GitHub 호스팅 러너에서 돈다(느린 조회가 집 러너의 상태 갱신을 막지
않도록).

조회에 실패하거나 결과가 의심스러우면 스냅샷을 그대로 두고 종료 코드 1로
끝난다.
"""
import sys

from kst_time import now_kst_iso
from tesla_snapshot import SNAPSHOT_PATH, load_snapshot, save_snapshot
from tesla_source import fetch_tesla_superchargers

# 새 목록이 기존의 이 비율 미만으로 줄면 정상적인 변화가 아니라 이상 응답으로 본다.
MIN_KEEP_RATIO = 0.5


def run():
    """스냅샷이 최신 상태가 되면 True, 갱신하지 못했으면 False."""
    stations = fetch_tesla_superchargers()
    if stations is None:
        print("슈퍼차저 조회 실패 — 기존 스냅샷을 유지합니다.")
        return False

    previous = load_snapshot()
    previous_stations = previous["stations"] if previous else []
    if not stations or len(stations) < len(previous_stations) * MIN_KEEP_RATIO:
        print(
            f"경고: 슈퍼차저가 {len(previous_stations)}개소에서 {len(stations)}개소로 줄었습니다. "
            "이상 응답으로 판단해 기존 스냅샷을 유지합니다."
        )
        return False

    if stations == previous_stations:
        print(f"슈퍼차저 {len(stations)}개소 — 변경 없음.")
        return True

    save_snapshot(stations, now_kst_iso())
    print(f"슈퍼차저 {len(stations)}개소 저장 → {SNAPSHOT_PATH}")
    return True


if __name__ == "__main__":
    try:
        succeeded = run()
    except Exception as err:  # 최상위 예외 캐치: 트레이스백 대신 명확한 메시지로 종료
        print(f"오류: 슈퍼차저 갱신 실패 — {err}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if succeeded else 1)
