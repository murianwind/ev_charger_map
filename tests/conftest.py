"""BDD 시나리오 공용 준비물과 스텝 정의.

실제 네트워크는 절대 쓰지 않는다 — 정부 API와 supercharge.info는 가짜로
바꿔 끼우고, geojson/스냅샷/쿨다운 파일은 테스트마다 임시 폴더에 둔다.
"""
import json
import sys
import urllib.parse
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, then, when

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import address_parser  # noqa: E402
import fetch_chargers  # noqa: E402
import fetch_status  # noqa: E402
import fetch_tesla  # noqa: E402
import geojson_store  # noqa: E402
import gov_charger_api  # noqa: E402
import http_client  # noqa: E402
import kakao_geocoder  # noqa: E402
import operating_hours  # noqa: E402
import quota_cooldown  # noqa: E402
import tesla_snapshot  # noqa: E402
import tesla_source  # noqa: E402

# 지역명 -> (zcode, 주소 앞부분, 위도, 경도). 좌표는 address_parser의 대략적인
# 시도 사각형 검증을 통과하는 값이어야 한다.
REGIONS = {
    "부산": ("26", "부산광역시 사상구 테스트로", 35.10, 129.00),
    "대전": ("30", "대전광역시 유성구 테스트로", 36.35, 127.40),
    "인천": ("28", "인천광역시 남동구 테스트로", 37.45, 126.70),
}
NAME_BY_ZCODE = {zcode: name for name, (zcode, *_rest) in REGIONS.items()}


def gov_item(region, index):
    zcode, addr, lat, lng = REGIONS[region]
    return {
        "statId": f"NEW-{zcode}-{index}",
        "statNm": f"{region} 새 충전소 {index}",
        "chgerId": "01",
        "chgerType": "02",
        "addr": f"{addr} {index}",
        "lat": str(lat),
        "lng": str(lng),
        "limitYn": "N",
        "delYn": "N",
        "kindDetail": "B001",
        "stat": "2",
        "zcode": zcode,
    }


GATEWAY_ERROR = {
    "OpenAPI_ServiceResponse": {
        "cmmMsgHeader": {"errMsg": "SERVICE ERROR", "returnAuthMsg": "HTTP_ERROR", "returnReasonCode": "04"}
    }
}


def page(items, total):
    return {"resultCode": "00", "totalCount": total, "items": {"item": items}}


class FakeGovApi:
    """gov_charger_api.fetch_json 자리에 끼우는 가짜. 지역(zcode)별로
    "몇 번째 호출의 몇 페이지에 무엇을 줄지"를 함수로 등록해둔다."""

    def __init__(self):
        self.region_plans = {}
        self.status_plan = None
        self.quota_exceeded = False
        self.calls = []

    def __call__(self, url, quota_marker=None, **_kwargs):
        parsed = urllib.parse.urlparse(url)
        operation = parsed.path.rsplit("/", 1)[-1]
        query = urllib.parse.parse_qs(parsed.query)
        page_no = int(query["pageNo"][0])
        zcode = query.get("zcode", [None])[0]
        self.calls.append((operation, zcode, page_no))

        if self.quota_exceeded:
            raise http_client.QuotaExceededError("한도 초과(테스트)")
        if operation == "getChargerStatus":
            return self.status_plan(page_no)
        call_index = sum(1 for c in self.calls if c[1] == zcode)
        return self.region_plans[zcode](page_no, call_index)

    def queried_zcodes(self):
        return {zcode for _op, zcode, _page in self.calls if zcode}


class World:
    """시나리오 한 개가 진행되는 동안 스텝들이 공유하는 상태."""

    def __init__(self):
        self.gov_api = FakeGovApi()
        self.tesla_sites = None  # None이면 조회 실패
        self.result = None
        self.status_items = None


@pytest.fixture
def world(tmp_path, monkeypatch):
    world = World()
    monkeypatch.setattr(geojson_store, "GEOJSON_PATH", str(tmp_path / "chargers.geojson"))
    monkeypatch.setattr(tesla_snapshot, "SNAPSHOT_PATH", str(tmp_path / "tesla_snapshot.json"))
    monkeypatch.setattr(quota_cooldown, "COOLDOWN_FILE", str(tmp_path / "cooldown"))
    monkeypatch.setattr(gov_charger_api, "fetch_json", world.gov_api)
    monkeypatch.setattr(gov_charger_api.time, "sleep", lambda _sec: None)

    def fake_tesla_fetch(_url, **_kwargs):
        if world.tesla_sites is None:
            raise http_client.FetchError("시간 초과(테스트)")
        return world.tesla_sites

    monkeypatch.setattr(tesla_source, "fetch_json", fake_tesla_fetch)
    for name in ("KAKAO_REST_API_KEY", "FULL_SEED", "REGION_DAY", "RETRY_PENDING"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(kakao_geocoder, "_consecutive_failures", 0, raising=False)
    world.monkeypatch = monkeypatch
    return world


# ---------- 지도(geojson) 준비/확인 ----------

def _existing_feature(region, index):
    zcode, addr, lat, lng = REGIONS[region]
    return geojson_store.station_to_feature(
        {
            "statId": f"OLD-{zcode}-{index}",
            "statNm": f"{region} 기존 충전소 {index}",
            "addr": addr,
            "zcode": zcode,
            "isTesla": False,
            "lat": lat,
            "lng": lng,
            "chargers": [{"chgerId": "01", "stat": "2"}],
        }
    )


def _tesla_station(index):
    return {
        "statId": f"TESLA-{index}",
        "statNm": f"테슬라 수퍼차저 {index}",
        "isTesla": True,
        "lat": 37.5,
        "lng": 127.0,
        "chargers": [],
    }


def _load_map():
    return geojson_store.load_geojson()


def _save_map(geojson):
    geojson_store.save_geojson(geojson)


def _count(predicate):
    return sum(1 for f in _load_map()["features"] if predicate(f["properties"]))


@given(parsers.parse("지도에 부산 {busan:d}개소와 대전 {daejeon:d}개소가 있다"))
def map_has_busan_and_daejeon(world, busan, daejeon):
    features = [_existing_feature("부산", i) for i in range(busan)]
    features += [_existing_feature("대전", i) for i in range(daejeon)]
    _save_map({"type": "FeatureCollection", "features": features})


@given("지도에 슈퍼차저가 없다")
def map_has_no_tesla(world):
    assert _count(lambda p: p.get("isTesla")) == 0


@given(parsers.parse("지도에 슈퍼차저 {count:d}개소가 있다"))
def map_has_tesla(world, count):
    geojson = _load_map()
    geojson["features"] += [geojson_store.station_to_feature(_tesla_station(i)) for i in range(count)]
    _save_map(geojson)


@given(parsers.parse("{region}이 재시도 대기 중이다"))
def region_is_pending(world, region):
    geojson = _load_map()
    geojson_store.mark_pending(geojson, [REGIONS[region][0]], "2026-11-13T04:02:20+09:00")
    _save_map(geojson)


@then(parsers.parse("지도의 슈퍼차저는 {count:d}개소다"))
def map_tesla_count(world, count):
    assert _count(lambda p: p.get("isTesla")) == count


@then(parsers.parse("지도의 {region}은 {count:d}개소다"))
@then(parsers.parse("지도의 {region}는 {count:d}개소다"))
def map_region_count(world, region, count):
    zcode = REGIONS[region][0]
    assert _count(lambda p: p.get("zcode") == zcode) == count


@then("재시도 대기 지역은 없다")
def no_pending(world):
    assert geojson_store.pending_regions(_load_map()) == []


@then(parsers.parse("재시도 대기 지역은 {regions}이다"))
def pending_is(world, regions):
    expected = sorted(REGIONS[name.strip()][0] for name in regions.split(","))
    assert geojson_store.pending_regions(_load_map()) == expected


# ---------- 정부 API 가짜 응답 ----------

def _items(region, count):
    return [gov_item(region, i) for i in range(count)]


@given(parsers.parse("정부 API가 {region}은 {count:d}개소를 정상 응답한다"))
def gov_normal(world, region, count):
    items = _items(region, count)
    world.gov_api.region_plans[REGIONS[region][0]] = lambda page_no, _call: page(
        items if page_no == 1 else [], count
    )


@given(parsers.parse("정부 API가 {region}은 계속 빈 응답을 준다"))
def gov_always_empty(world, region):
    world.gov_api.region_plans[REGIONS[region][0]] = lambda _page_no, _call: page([], 0)


@given(
    parsers.parse(
        "정부 API가 {region}은 전체 {total:d}건 중 첫 페이지 {first:d}건만 주고 이후 빈 응답을 준다"
    )
)
def gov_truncated(world, region, total, first):
    items = _items(region, first)
    world.gov_api.region_plans[REGIONS[region][0]] = lambda page_no, _call: page(
        items if page_no == 1 else [], total
    )


@given(
    parsers.parse(
        "정부 API가 {region}은 처음 한 번만 빈 응답을 주고 다음엔 {count:d}개소를 정상 응답한다"
    )
)
def gov_empty_once(world, region, count):
    items = _items(region, count)
    world.gov_api.region_plans[REGIONS[region][0]] = lambda page_no, call: (
        page([], 0) if call == 1 else page(items if page_no == 1 else [], count)
    )


@given("정부 API가 일일 호출 한도 초과를 응답한다")
def gov_quota_exceeded(world):
    world.gov_api.quota_exceeded = True


@given(parsers.parse("상태 API가 전체 {total:d}건이라 알리고 {given_count:d}건만 준 뒤 빈 응답을 준다"))
def status_drift(world, total, given_count):
    items = [{"statId": f"S{i}", "chgerId": "01", "stat": "3"} for i in range(given_count)]
    world.gov_api.status_plan = lambda page_no: page(items if page_no == 1 else [], total)


@then(parsers.parse("정부 API는 {region}만 조회했다"))
def gov_queried_only(world, region):
    assert world.gov_api.queried_zcodes() == {REGIONS[region][0]}


@then("정부 API는 한 번도 호출되지 않았다")
def gov_never_called(world):
    assert world.gov_api.calls == []


# ---------- 실행 ----------

@when("수요일 담당 지역 갱신을 실행하면")
def run_wednesday(world):
    world.monkeypatch.setenv("REGION_DAY", "수(부산·대전)")
    world.result = fetch_chargers.run("test-key")


@when("재시도 실행을 하면")
def run_retry(world):
    world.monkeypatch.setenv("RETRY_PENDING", "1")
    world.result = fetch_chargers.run("test-key")


@when("상태 변경분을 조회하면")
def fetch_status_items(world):
    world.status_items = gov_charger_api.fetch_status_delta("test-key", 10)


@then("실행 결과는 성공이다")
def result_ok(world):
    assert world.result is True


@then("실행 결과는 실패다")
def result_failed(world):
    assert world.result is False


@then(parsers.parse("상태 변경 {count:d}건을 받는다"))
def status_count(world, count):
    assert len(world.status_items) == count


# ---------- 테슬라 ----------

def _supercharge_site(index):
    return {
        "id": index,
        "name": f"Seoul - Site {index}",
        "status": "OPEN",
        "address": {"country": "South Korea", "state": "Seoul", "city": "Seoul"},
        "gps": {"latitude": 37.5, "longitude": 127.0},
        "stalls": {"v3": 8},
        "stallCount": 8,
        "powerKilowatt": 250,
    }


def _snapshot_stations():
    with open(tesla_snapshot.SNAPSHOT_PATH, encoding="utf-8") as f:
        return json.load(f)["stations"]


@given(parsers.parse("슈퍼차저 스냅샷에 {count:d}개소가 있다"))
def snapshot_has(world, count):
    tesla_snapshot.save_snapshot([_tesla_station(i) for i in range(count)], "2026-11-13T04:02:20+09:00")


@given("슈퍼차저 스냅샷이 없다")
def snapshot_missing(world):
    assert tesla_snapshot.load_snapshot() is None


@given("supercharge.info 조회가 실패한다")
def tesla_fails(world):
    world.tesla_sites = None


@given(parsers.parse("supercharge.info가 한국 내 운영중 V3 이상 {count:d}개소를 응답한다"))
def tesla_responds(world, count):
    world.tesla_sites = [_supercharge_site(i) for i in range(count)]


@when("슈퍼차저 스냅샷 갱신을 실행하면")
def run_tesla(world):
    world.result = fetch_tesla.run()


@then(parsers.parse("슈퍼차저 스냅샷은 {count:d}개소다"))
def snapshot_count(world, count):
    assert len(_snapshot_stations()) == count


@then("슈퍼차저 갱신 결과는 성공이다")
def tesla_ok(world):
    assert world.result is True


@then("슈퍼차저 갱신 결과는 실패다")
def tesla_failed(world):
    assert world.result is False


# ---------- 필터 / 좌표 검증 ----------

@given(parsers.re(r'이용제한 사유가 "(?P<limit_detail>.*)"이고 이용시간이 "(?P<use_time>.*)"인 충전소가 있다'))
def station_with_texts(world, limit_detail, use_time):
    world.item = {**gov_item("부산", 0), "limitDetail": limit_detail, "useTime": use_time}


@when("지도에 올릴 충전소인지 판정하면")
def judge_filter(world):
    world.result = gov_charger_api.passes_filter(world.item)


@then("그 충전소는 제외된다")
def station_excluded(world):
    assert world.result is False


@then("그 충전소는 포함된다")
def station_included(world):
    assert world.result is True


@when(parsers.re(r'주소 "(?P<addr>.*)"와 좌표 (?P<lat>[\d.]+), (?P<lng>[\d.]+)가 맞는지 확인하면'))
def check_coordinates(world, addr, lat, lng):
    world.result = address_parser.coordinates_plausible(addr, float(lat), float(lng))


@then(parsers.parse("좌표 검증 결과는 {expected}이다"))
def coordinates_result(world, expected):
    assert world.result is (expected == "일치")


# ---------- 이용시간 ----------

def _clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@when(parsers.re(r'이용시간 "(?P<text>.*)"를 해석하면'))
def parse_hours(world, text):
    world.result = operating_hours.parse_use_time(text)


@then(parsers.parse("운영 형태는 {kind}이고 시각은 {times}이다"))
def hours_result(world, kind, times):
    hours = world.result
    assert hours["kind"] == kind
    actual = []
    if "start" in hours:
        actual.append(f"{_clock(hours['start'])}~{_clock(hours['end'])}")
    if "weekendStart" in hours:
        actual.append(f"{_clock(hours['weekendStart'])}~{_clock(hours['weekendEnd'])}")
    assert (" ".join(actual) or "-") == times


# ---------- 호출 횟수 ----------

@then(parsers.parse("정부 API는 {count:d}번 호출되었다"))
def gov_call_count(world, count):
    assert len(world.gov_api.calls) == count


# ---------- 카카오 ----------

@given("카카오 API 키가 설정되어 있다")
def kakao_key(world):
    world.monkeypatch.setenv("KAKAO_REST_API_KEY", "test-kakao-key")
    world.kakao_calls = 0


def _install_kakao(world, failures_before_success):
    def fake_fetch(_url, **_kwargs):
        world.kakao_calls += 1
        if failures_before_success is None or world.kakao_calls <= failures_before_success:
            raise http_client.FetchError("시간 초과(테스트)")
        return {"documents": [{"x": "127.0", "y": "37.5"}]}

    world.monkeypatch.setattr(kakao_geocoder, "fetch_json", fake_fetch)


@given("카카오 API가 계속 실패한다")
def kakao_always_fails(world):
    _install_kakao(world, None)


@given(parsers.parse("카카오 API가 {count:d}번 실패한 뒤 정상 응답한다"))
def kakao_recovers(world, count):
    _install_kakao(world, count)


@when(parsers.parse("주소 {count:d}개의 좌표를 차례로 조회하면"))
def geocode_many(world, count):
    world.result = [kakao_geocoder.geocode_address(f"서울특별시 종로구 테스트로 {i}") for i in range(count)]


@then(parsers.parse("카카오 API는 {count:d}번만 호출되었다"))
@then(parsers.parse("카카오 API는 {count:d}번 호출되었다"))
def kakao_call_count(world, count):
    assert world.kakao_calls == count


@then("좌표 조회 결과는 모두 없음이다")
def geocode_all_none(world):
    assert all(r is None for r in world.result)


# ---------- 게이트웨이 오류 ----------

@given(parsers.parse("정부 API가 {region}은 계속 게이트웨이 오류를 응답한다"))
def gov_gateway_error(world, region):
    world.gov_api.region_plans[REGIONS[region][0]] = lambda _page_no, _call: GATEWAY_ERROR


@given(
    parsers.parse(
        "정부 API가 {region}은 3페이지에 걸쳐 6개소를 주되 페이지마다 첫 요청은 게이트웨이 오류를 응답한다"
    )
)
def gov_error_once_per_page(world, region):
    items = _items(region, 6)
    seen_pages = set()

    def plan(page_no, _call):
        if page_no not in seen_pages:
            seen_pages.add(page_no)
            return GATEWAY_ERROR
        return page(items[(page_no - 1) * 2 : page_no * 2], 6)

    world.gov_api.region_plans[REGIONS[region][0]] = plan


@given("상태 API가 계속 게이트웨이 오류를 응답한다")
def status_gateway_error(world):
    world.gov_api.status_plan = lambda _page_no: GATEWAY_ERROR


@given("상태 API가 한 번 게이트웨이 오류를 준 뒤 기존 부산 충전소 1건의 상태 변경을 응답한다")
def status_recovers(world):
    change = {"statId": "OLD-26-0", "chgerId": "01", "stat": "3", "statUpdDt": "20261114103000"}
    world.gov_api.status_plan = lambda _page_no: (
        GATEWAY_ERROR if len(world.gov_api.calls) == 1 else page([change], 1)
    )


@when("상태 갱신을 실행하면")
def run_status(world):
    world.monkeypatch.setenv("EV_SERVICE_KEY", "test-key")
    try:
        fetch_status.main()
        world.result = True
    except gov_charger_api.ApiResponseError:
        world.result = False


@then("상태 갱신 결과는 성공이다")
def status_ok(world):
    assert world.result is True


@then("상태 갱신 결과는 실패다")
def status_failed(world):
    assert world.result is False


@then("지도에 마지막 상태 갱신 기록이 없다")
def no_status_record(world):
    assert "lastStatusUpdate" not in _load_map().get("meta", {})


@then(parsers.parse("지도의 마지막 상태 갱신은 {count:d}건 반영이다"))
def status_record(world, count):
    assert _load_map()["meta"]["lastStatusUpdate"]["updated"] == count
