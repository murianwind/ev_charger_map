"""한국 주소 텍스트 전용 유틸리티. 특정 API를 전혀 모른다 — 주소 문자열과
좌표만 다루는 순수 로직이라 다른 데이터 소스에도 재사용 가능하다.

- split_addr_and_location: "주소+시설명"이 붙어서 오는 원본 텍스트를 분리
- coordinates_plausible: 주소가 말하는 지역과 좌표가 대략 맞는지 확인
"""
import re

# 원본 addr에 "주소+시설명"이 붙어서 오는 경우가 있다
# (예: "대전광역시 유성구 전민동 346-3전민동 제2공영주차장 -",
#      "대전광역시 유성구 북유성대로 172죽동 공영주차장 2~3층",
#      "대전광역시 유성구 덕명동 595학하지구 제5공영주차장",
#      "대전광역시 유성구 엑스포로 55 (도룡동, 기초과학연구원 본원)").
# 먼저 끝에 괄호로 묶인 부가설명이 있으면 떼어내 시설명 후보로 챙겨두고,
# 남은 주소에서 주소가 끝나는 지점을 우선순위대로 찾는다:
#   1. 지번(숫자-숫자, 예: 346-3) — 하이픈이 있어서 "제2공영주차장" 같은
#      시설명 속 숫자와 헷갈리지 않는다.
#   2. 도로명(로/길 + 숫자, 예: "북유성대로 172") — 도로명주소는 하이픈이
#      없는 경우가 많아서 별도로 잡는다.
#   3. (1·2 둘 다 없을 때 최후 수단) "제N"(서수) 형태가 아니면서 숫자
#      뒤에 (공백이 있든 없든) 한글이 나오는 지점 — 하이픈도 "로/길"도
#      없는 순수 지번(예: "덕명동 595", "봉산동299 공영주차장")을 잡기
#      위함. 이건 오탐 위험이 있어서 1·2가 하나도 안 잡혔을 때만 쓰고,
#      제일 먼저 나오는 지점만 쓴다(뒤쪽 시설명 안에도 이 패턴에 걸리는
#      숫자가 또 있을 수 있어서).
_LOT_NUMBER_PATTERN = re.compile(r"\d+-\d+")
_ROAD_NUMBER_PATTERN = re.compile(r"(?:로|길)\s*\d+(?:-\d+)?")
_BARE_NUMBER_GLUED_PATTERN = re.compile(r"(?<!제)\d+(?=\s*[가-힣])")
_TRAILING_PAREN_PATTERN = re.compile(r"\s*\(([^()]*)\)\s*$")


def split_addr_and_location(addr):
    """(정리된 주소, 뒤에 붙어있던 시설명) 튜플을 반환한다.
    분리할 게 없으면 시설명은 빈 문자열이다.
    """
    addr = addr or ""

    paren_text = ""
    paren_match = _TRAILING_PAREN_PATTERN.search(addr)
    if paren_match:
        paren_text = paren_match.group(1).strip()
        addr = addr[: paren_match.start()].strip()

    strong_matches = list(_LOT_NUMBER_PATTERN.finditer(addr)) + list(_ROAD_NUMBER_PATTERN.finditer(addr))
    if strong_matches:
        split_at = max(m.end() for m in strong_matches)
    else:
        weak_matches = list(_BARE_NUMBER_GLUED_PATTERN.finditer(addr))
        if not weak_matches:
            return addr.strip(" -"), paren_text
        split_at = weak_matches[0].end()

    address_part = addr[:split_at].strip()
    rest = addr[split_at:].strip(" -").strip()
    combined_rest = " ".join(p for p in (rest, paren_text) if p)
    return address_part, combined_rest


# addr가 이 지역명으로 시작하면, 좌표가 대략 이 사각 범위(lat_min, lat_max,
# lng_min, lng_max) 안에 있어야 한다. 정확한 행정경계가 아니라 넉넉한 사각형
# 근사치라, 경계 지역에서는 드물게 오탐/누락이 있을 수 있다 — 그래도 "대전
# 주소인데 좌표는 이천"처럼 완전히 딴 지역으로 튄 경우는 확실히 잡아낸다.
PROVINCE_BOUNDS = {
    "서울": (37.42, 37.70, 126.76, 127.18),
    "인천": (37.20, 37.75, 126.05, 126.80),
    "경기": (36.90, 38.30, 126.35, 127.85),
    "강원": (37.00, 38.65, 127.55, 129.40),
    "충청북도": (36.00, 37.20, 127.30, 128.30),
    "충북": (36.00, 37.20, 127.30, 128.30),
    "충청남도": (35.90, 37.05, 126.10, 127.55),
    "충남": (35.90, 37.05, 126.10, 127.55),
    "대전": (36.20, 36.50, 127.25, 127.55),
    "세종": (36.42, 36.72, 127.10, 127.40),
    "전라북도": (35.55, 36.15, 126.40, 127.75),
    "전북": (35.55, 36.15, 126.40, 127.75),
    "전라남도": (33.90, 35.55, 125.90, 127.85),
    "전남": (33.90, 35.55, 125.90, 127.85),
    "광주": (35.00, 35.30, 126.65, 127.00),
    "경상북도": (35.65, 37.15, 128.15, 129.65),
    "경북": (35.65, 37.15, 128.15, 129.65),
    "대구": (35.65, 36.05, 128.35, 128.75),
    "경상남도": (34.75, 35.85, 127.50, 129.30),
    "경남": (34.75, 35.85, 127.50, 129.30),
    "부산": (34.85, 35.40, 128.75, 129.35),
    "울산": (35.35, 35.75, 129.10, 129.55),
    "제주": (33.10, 33.60, 126.05, 126.98),
}


def coordinates_plausible(addr, lat, lng):
    """addr가 말하는 지역과 실제 (lat, lng)가 대략 맞는지 확인한다.
    addr에서 알아볼 수 있는 지역명이 없으면 판단하지 않고 통과시킨다
    (틀렸다고 확신할 근거가 없을 때 잘못 걸러내지 않기 위함).
    """
    # 시도명은 주소 맨 앞에만 온다. 주소 어디에든 있으면 맞는 걸로 보면
    # "부산광역시 해운대구"가 "대구"로, "월드컵경기장로"가 "경기"로 잘못
    # 읽혀서 멀쩡한 충전소가 불일치로 걸러진다.
    addr = (addr or "").lstrip()
    for province, (lat_min, lat_max, lng_min, lng_max) in PROVINCE_BOUNDS.items():
        if addr.startswith(province):
            return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max
    return True
