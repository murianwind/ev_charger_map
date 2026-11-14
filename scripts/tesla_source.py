"""supercharge.info(테슬라 슈퍼차저 커뮤니티 오픈 데이터베이스) 전용 로직."""
import os

from http_client import fetch_json
from kakao_geocoder import find_nearby_place_name, reverse_geocode
from operating_hours import parse_use_time

SUPERCHARGE_INFO_URL = "https://supercharge.info/service/supercharge/allSites"

# 전 세계 사이트를 한 번에 내려주는 큰 응답이고 서버가 느릴 때가 많아서,
# 다른 API보다 훨씬 길게 기다리고 압축 전송을 요청한다.
SUPERCHARGE_INFO_TIMEOUT_SEC = 240

# supercharge.info의 stalls 필드 키 -> 표시 라벨
STALL_VERSION_LABELS = {
    "v2": "V2",
    "v3": "V3",
    "v4": "V4",
    "urban": "Urban",
    "other": "기타",
}

# 카카오맵에 등록된 실제 장소명은 "OO 수퍼차저(DC콤보) 전기차충전소 (테슬라전용)"처럼
# "수퍼차저" 뒤에 커넥터 타입/카테고리 설명이 덧붙는 경우가 많다. 이 정보는
# 팝업에 이미 배지/커넥터 목록으로 따로 나오니 중복이라, "수퍼차저"까지만
# 남기고 뒤는 잘라낸다. 표기가 "슈퍼차저"인 경우도 대비해둔다.
_SUPERCHARGER_KEYWORDS = ("수퍼차저", "슈퍼차저")


def _truncate_after_supercharger(name):
    for keyword in _SUPERCHARGER_KEYWORDS:
        idx = name.find(keyword)
        if idx != -1:
            return name[: idx + len(keyword)]
    return name


def fetch_tesla_superchargers():
    """한국 소재의 '운영중(OPEN)' 슈퍼차저만 받아와 station dict 리스트로 반환한다.
    조회에 실패하면 None을 반환한다 — 빈 리스트("한국에 슈퍼차저가 없다")와
    구분해야 호출부가 기존 목록을 지울지 말지 판단할 수 있다.

    한국환경공단 API에는 테슬라(busiId=TE) 데이터가 실질적으로 없어서
    (실측 결과 전국 0건) 이 별도 소스로 보강한다. 정부 API 일일 한도와
    무관한 외부 무료 API라 매일 갱신해도 부담이 없다.

    이 API는 실시간 사용 가능 여부를 제공하지 않아서, 개별 커넥터 상태를
    지어내지 않고 "충전기 N개 (V3 N개) 최대 250kW" 형태의 요약 텍스트
    (teslaSummary)만 만들어 저장한다.

    주소도 영문(state/city/street)만 제공돼서, KAKAO_REST_API_KEY가 설정돼
    있으면 좌표를 한글 주소로 변환(reverse geocoding)해 표시한다. 키가 없거나
    변환에 실패하면 기존 영문 state/city를 그대로 쓴다.

    사이트 이름("Daejeon - DCC" 같은 영문 축약 이름)은 번역이 아니라, 카카오맵에
    그 좌표 근처 "테슬라"로 등록된 장소가 있는지 검색해서 있으면 그 한글
    이름으로 대체한다. 등록된 장소가 없으면(아직 카카오맵에 안 올라온 경우)
    기존 영문 이름을 정리해서 그대로 쓴다.

    V2 전용(또는 버전 구분이 없어 확인 불가한) 슈퍼차저는 제외한다 — V3 이상만
    보여달라는 요청에 따른 것이다.
    """
    try:
        sites = fetch_json(SUPERCHARGE_INFO_URL, timeout_sec=SUPERCHARGE_INFO_TIMEOUT_SEC, compressed=True)
    except Exception as err:  # noqa: BLE001 - 어떤 이유로 실패하든 "조회 실패"로 통일한다
        print(f"경고: 테슬라 슈퍼차저(supercharge.info) 조회 실패: {err}")
        return None

    stations = []
    skipped_below_v3 = 0
    korean_addr_count = 0
    for site in sites:
        addr = site.get("address") or {}
        country = addr.get("country") or ""
        if "korea" not in country.lower():
            continue
        if site.get("status") != "OPEN":
            continue

        gps = site.get("gps") or {}
        lat, lng = gps.get("latitude"), gps.get("longitude")
        if lat is None or lng is None:
            continue

        stalls = site.get("stalls") or {}

        # V3 미만(V2 전용 등)은 지도에서 뺀다. v3/v4 스톨이 하나도 없으면
        # 제외한다 — 구버전만 있거나, stalls에 버전 구분이 아예 없어서
        # V3 이상인지 확인이 안 되는 경우 둘 다 안전하게 걸러낸다.
        if not (stalls.get("v3") or stalls.get("v4")):
            skipped_below_v3 += 1
            continue

        total_stalls = site.get("stallCount") or sum(
            v for k, v in stalls.items() if k in STALL_VERSION_LABELS
        )
        version_parts = [
            f"{STALL_VERSION_LABELS[k]} {stalls[k]}개"
            for k in STALL_VERSION_LABELS
            if stalls.get(k)
        ]
        power_kw = site.get("powerKilowatt")

        summary = f"충전기 {total_stalls}개"
        if version_parts:
            summary += f" ({', '.join(version_parts)})"
        if power_kw:
            summary += f" 최대 {power_kw}kW"

        raw_name = site.get("name") or "테슬라 수퍼차저"
        cleaned_english_name = raw_name.replace(", South Korea", "").replace("South Korea - ", "")
        korean_place_name = find_nearby_place_name(lat, lng, "테슬라")
        station_name = _truncate_after_supercharger(korean_place_name) if korean_place_name else cleaned_english_name

        # supercharge.info의 주소는 영문(state/city/street)뿐이라, 이미 갖고
        # 있는 좌표로 카카오 좌표->한글주소 변환(reverse geocoding)을 해서
        # 한글 주소로 바꿔 보여준다. 카카오 키가 없거나 실패하면 기존처럼
        # 영문 state/city를 그대로 쓴다.
        korean_addr = reverse_geocode(lat, lng)
        if korean_addr:
            korean_addr_count += 1
            display_addr = korean_addr
            display_addr_detail = ""
        else:
            display_addr = " ".join(filter(None, [addr.get("state"), addr.get("city")]))
            display_addr_detail = addr.get("street") or ""

        display_use_time = site.get("hours") or "24시간 이용가능"

        stations.append(
            {
                "statId": f"TESLA-{site.get('id')}",
                "statNm": station_name,
                "addr": display_addr,
                "addrDetail": display_addr_detail,
                "location": site.get("facilityName") or "",
                "useTime": display_use_time,
                "hours": parse_use_time(display_use_time),
                "busiNm": "테슬라",
                "busiCall": "",
                "parkingFree": "Y",
                "limitYn": "N",
                "limitDetail": "",
                "floorType": "",
                "floorNum": "",
                "isTesla": True,
                "teslaSummary": summary,
                "lat": lat,
                "lng": lng,
                "chargers": [],
            }
        )

    if stations and korean_addr_count == 0 and os.environ.get("KAKAO_REST_API_KEY"):
        # 카카오 키가 있는데 한글 주소가 하나도 안 나왔다 — 카카오 API에 못
        # 닿은 것이다. 이대로 저장하면 이름/주소가 전부 영문으로 덮어써지므로
        # 조회 실패로 취급한다.
        print("경고: 카카오 좌표→주소 변환이 전부 실패했습니다 — 슈퍼차저 조회를 실패로 처리합니다.")
        return None

    print(f"[supercharge.info] 한국 내 운영중 슈퍼차저 {len(stations)}개소 (V3 미만 제외: {skipped_below_v3}건)")
    return stations
