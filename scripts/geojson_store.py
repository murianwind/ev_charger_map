"""docs/data/chargers.geojson 파일의 읽기/쓰기, station dict <-> feature 변환,
지역(zcode)/테슬라 단위의 부분 교체, 재시도 대기 지역 기록을 담당한다.

이 모듈은 API를 전혀 모른다 — GeoJSON 파일 자체의 구조만 다룬다.
"""
import json
import os

GEOJSON_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "docs", "data", "chargers.geojson")
)


def load_geojson():
    if not os.path.exists(GEOJSON_PATH):
        return {"type": "FeatureCollection", "features": []}
    with open(GEOJSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_geojson(geojson):
    os.makedirs(os.path.dirname(GEOJSON_PATH), exist_ok=True)
    with open(GEOJSON_PATH, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False)


def station_to_feature(station):
    lat, lng = station["lat"], station["lng"]
    props = {k: v for k, v in station.items() if k not in ("lat", "lng")}
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "properties": props,
    }


def replace_regions(geojson, zcodes, stations):
    """zcodes에 해당하는 기존 feature를 전부 제거하고 새 station 목록으로 교체한다.
    다른 지역의 feature는 건드리지 않는다 (요일별 지역 로테이션의 핵심 동작).
    """
    zcode_set = set(zcodes)
    kept = [f for f in geojson["features"] if f["properties"].get("zcode") not in zcode_set]
    fresh = [station_to_feature(s) for s in stations]
    geojson["features"] = kept + fresh
    return geojson


def replace_tesla(geojson, tesla_stations):
    """isTesla=True인 기존 feature를 전부 제거하고 새 목록으로 교체한다."""
    kept = [f for f in geojson["features"] if not f["properties"].get("isTesla")]
    fresh = [station_to_feature(s) for s in tesla_stations]
    geojson["features"] = kept + fresh
    return geojson


def prune_orphaned(geojson, valid_zcodes):
    """테슬라도 아니고 유효한 지역 코드 목록에도 없는 feature를 제거한다.

    지역별 부분 교체(replace_regions)는 zcode가 일치하는 것만 갈아치우는
    방식이라, zcode 필드 자체가 없는 항목(예: 초기 개발 중 넣어뒀던 샘플
    데이터)은 어느 요일 로테이션에도 안 걸려서 영원히 안 지워지는 문제가
    있었다. zcode가 유효한 16개 지역 코드 중 어디에도 속하지 않으면
    "API로 정상적으로 받은 게 아니다"로 보고 정리한다.
    """
    valid = set(valid_zcodes)
    before = len(geojson["features"])
    geojson["features"] = [
        f
        for f in geojson["features"]
        if f["properties"].get("isTesla") or f["properties"].get("zcode") in valid
    ]
    removed = before - len(geojson["features"])
    return geojson, removed


def count_region(geojson, zcode):
    return sum(1 for f in geojson["features"] if f["properties"].get("zcode") == zcode)


def pending_regions(geojson):
    """갱신에 실패해 재시도를 기다리는 zcode 목록(정렬됨)."""
    return sorted(geojson.get("meta", {}).get("pendingRegions", {}))


def mark_pending(geojson, zcodes, now_iso):
    """zcodes를 재시도 대기로 기록한다. 이미 대기 중이면 처음 실패한 시각은
    그대로 두고 시도 횟수만 늘린다."""
    pending = geojson.setdefault("meta", {}).setdefault("pendingRegions", {})
    for zcode in zcodes:
        entry = pending.setdefault(zcode, {"since": now_iso, "attempts": 0})
        entry["attempts"] += 1


def clear_pending(geojson, zcodes):
    pending = geojson.get("meta", {}).get("pendingRegions", {})
    for zcode in zcodes:
        pending.pop(zcode, None)
