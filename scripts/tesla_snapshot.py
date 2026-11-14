"""data/tesla_snapshot.json — 마지막으로 조회에 성공한 테슬라 슈퍼차저 목록.

supercharge.info는 느리거나 응답하지 않을 때가 있어서, 조회(fetch_tesla.py,
GitHub 호스팅 러너)와 지도 반영(fetch_chargers.py, 집 러너)을 이 파일로
분리한다. 조회가 실패해도 이 파일은 그대로 남아 지도의 슈퍼차저가 사라지지
않는다.
"""
import json
import os

SNAPSHOT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "tesla_snapshot.json")
)


def load_snapshot():
    """{"updatedAt": ..., "stations": [...]} 또는 파일이 없으면 None."""
    if not os.path.exists(SNAPSHOT_PATH):
        return None
    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(stations, updated_at):
    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    snapshot = {"source": "supercharge.info", "updatedAt": updated_at, "stations": stations}
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)
        f.write("\n")
