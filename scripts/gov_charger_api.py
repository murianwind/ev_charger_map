"""한국환경공단 EvCharger API 전용 로직.

- 페이지네이션(전체 item 수집)
- 필터 조건 (완속/NACS 타입, 이용제한, 학교/아파트 제외 등)
- 시도(zcode) 요일별 그룹 — 하루에 전국을 다 스캔하는 대신 7일에 나눠 담당
- 원본 item -> station dict 변환 (GeoJSON 변환은 geojson_store의 책임)
- 지역별 수집 완전성 판정 (빈/잘린 응답을 정상 결과로 취급하지 않는다)

주소 텍스트 파싱/좌표 검증은 address_parser로, 한도초과 쿨다운 상태 관리는
quota_cooldown으로, 좌표 검증/보정 자체는 kakao_geocoder로, 이용시간 텍스트
파싱은 operating_hours로 각각 분리돼 있다 — 이 모듈은 그것들을 조합해
"환경공단 API에서 어떻게 데이터를 받아 station dict로 만드는가"만 담당한다.
"""
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass, field

from address_parser import coordinates_plausible, split_addr_and_location
from http_client import FetchError, QuotaExceededError, fetch_json
from kakao_geocoder import verify_and_correct
from operating_hours import parse_use_time
from quota_cooldown import in_cooldown, start_cooldown

API_BASE = "https://apis.data.go.kr/B552584/EvCharger"
QUOTA_MARKER = "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"

ALLOWED_CHGER_TYPES = {"02", "09", "10"}  # AC완속 / NACS / DC콤보+NACS

CHGER_TYPE_NAMES = {
    "01": "DC차데모",
    "02": "AC완속",
    "03": "DC차데모+AC3상",
    "04": "DC콤보",
    "05": "DC차데모+DC콤보",
    "06": "DC차데모+AC3상+DC콤보",
    "07": "AC3상",
    "08": "DC콤보(완속)",
    "09": "NACS",
    "10": "DC콤보+NACS",
}

STAT_NAMES = {
    "0": "알수없음",
    "1": "통신이상",
    "2": "충전대기",
    "3": "충전중",
    "4": "운영중지",
    "5": "점검중",
    "6": "예약중",
    "9": "상태미확인",
}

# kindDetail(충전소 구분 상세 코드) 가이드 문서 47개 전체 중, 일반인이
# 자유롭게 접근하기 어려운 곳들을 제외한다.
# - H0(공동주택시설) 전체 + G005(오피스텔)/G006(단독주택): 거주자 전용일
#   가능성이 높은 주거시설 (H001 아파트를 뺐던 것과 같은 이유로 확장)
# - H003(사업장/사옥): 특정 회사 직원 전용일 가능성이 높음
# - G001(군부대): 민간인 접근 불가
# - I001(병원)/I002(종교시설)/I004(경찰서)/I006(복지관)/I007(수련원)/
#   I008(금융기관): 소속/방문객 전용이거나 일반 주차 목적이 아닌 경우가 많음
# - J001(학교)/J002(교육원)/J003(학원): 재학생/수강생 전용일 가능성이 높음
# - E004(골프장/CC): 회원 전용인 경우가 많음
# - F001(서비스센터)/F002(정비소): 정비 고객 전용, 일반 주차 목적이 아님
EXCLUDED_KIND_DETAILS = {
    "H001",  # 아파트
    "H002",  # 빌라
    "H003",  # 사업장(사옥)
    "H004",  # 기숙사
    "H005",  # 연립주택
    "G001",  # 군부대
    "G005",  # 오피스텔
    "G006",  # 단독주택
    "I001",  # 병원
    "I002",  # 종교시설
    "I004",  # 경찰서
    "I006",  # 복지관
    "I007",  # 수련원
    "I008",  # 금융기관
    "J001",  # 학교
    "J002",  # 교육원
    "J003",  # 학원
    "E004",  # 골프장(CC)
    "F001",  # 서비스센터
    "F002",  # 정비소
}

# 환경공단 API의 kindDetail 분류가 실제 시설과 다르게 잘못 찍혀있는 경우가
# 있다(예: "하나로마트 양재점"이 마트(E001)가 아니라 사업장/사옥(H003)으로
# 잘못 분류된 사례가 실제로 발견됨). 잘 알려진 대형 마트/소매 체인은 이름
# 자체가 "공공 이용 가능한 곳"이라는 강력한 증거이므로, kindDetail이 뭐라고
# 나오든 카테고리 제외만 무시하고 강제로 포함시킨다 — 이용제한(limitYn 등)
# 같은 다른 안전장치는 그대로 다 적용된다.
FORCE_INCLUDE_NAME_KEYWORDS = ("하나로마트", "이마트", "롯데마트", "홈플러스", "코스트코", "GS더프레시", "메가마트", "킴스클럽", "노브랜드", "트레이더스")

# "대학교"는 위와 다르게 카테고리 무관 강제포함이 아니라, "학교로 분류된
# 경우에 한해서만" 강제 포함한다. "건국대학교병원"처럼 이름에 "대학교"가
# 들어있어도 실제로는 병원(I001)으로 분류돼 있으면 병원은 그대로 제외해야
# 하는데, 카테고리 무관 방식이면 병원까지 같이 열려버려서 별도로 분리했다.
SCHOOL_KIND_DETAILS = {"J001", "J002", "J003"}
FORCE_INCLUDE_SCHOOL_KEYWORDS = ("대학교",)

# limitDetail 또는 useTime에 이 문구가 있으면 limitYn=N/kindDetail 분류와 무관하게 제외
# (아파트 등이 kindDetail로 정확히 분류 안 돼 있는 경우가 있어 텍스트로 한 번 더 거른다).
# - "불가"/"금지": "사용불가"/"이용불가"/"출입금지" 등을 폭넓게 잡는다(오탐 위험 낮음).
# - "거주자"/"입주민"/"외부인": 가이드 공식 예시("거주자외")와 실제 관측 문구
#   ("외부인 사용불가", "입주민만 사용가능 거주자 외출입제한")를 커버한다.
# - "제한될 수": "시설 상황에 따라 이용이 제한될 수 있음"처럼 조건부/불확실한
#   제한 문구를 커버한다 (실제로 이용 불가했던 사례에서 발견).
# - "내방객": "내방객 전용"처럼 특정 시설 방문객만 쓸 수 있는 경우를 커버한다.
# - "제한됨": "출입이 제한됨"처럼 조건부가 아니라 확정적으로 제한된 경우를
#   커버한다("제한될 수"와는 별개 — 조건부/확정 둘 다 잡는다).
# - "보안구역": "제한" 표현 없이 "보안구역"이라고만 적힌 경우를 대비한다.
# - "전용"/"직원"/"고객만"/"비공개"/"출입 제한": "임직원 전용", "호텔이용객전용",
#   "업소고객전용", "비공개(관용차 전용)", "직원 및 업무용차량 출입 제한",
#   "코스트코 고객만 가능"처럼 특정 대상만 쓸 수 있다는 문구를 커버한다
#   (전부 limitYn=N으로 내려와 지도에 올라가 있던 실제 사례).
NON_OPEN_KEYWORDS = ( "비개방", "불가", "금지", "외부인", "입주민", "거주자", "제한될 수", "제한됨", "내방객", "보안구역", "방문객 외", "출입제한", "출입 제한", "관계자 외", "전용", "직원", "고객만", "비공개")

# 이용시간이 통째로 "미개방"인 곳은 개방하지 않는 충전소다. "미개방"을 위
# 키워드처럼 아무 위치에서나 찾으면 "07:00~20:00(일요일 미개방)"처럼 일부
# 요일만 닫는 곳까지 빠지므로, 이용시간이 이 말로 시작할 때만 제외한다.
NOT_OPENED_USE_TIME_PREFIX = "미개방"

# 교도소/구치소 등은 kindDetail 47개 카테고리에 대응하는 항목이 아예 없어서
# (가이드 문서 기준), kindDetail로는 못 걸러낸다. 이런 시설은 보통 시설명에
# 직접 이름이 들어있으니, statNm(충전소명) 텍스트로 따로 거른다.
# "경찰서"는 kindDetail(I004)로도 이미 걸러지지만, API가 분류를 잘못
# 매기는 경우("파출소"처럼 경찰서 하위 기관이라 다르게 분류될 수 있음)에
# 대한 보험 차원에서 이름으로도 한 번 더 잡는다.
RESTRICTED_NAME_KEYWORDS = ("세무서", "교도소", "구치소", "소년원", "보호관찰소", "경찰서", "파출소", "장례", "119", "병무청", "소방서")

# 가이드 문서 공식 zcode(시도 코드) 표. 전국을 매번 한 번에 스캔하면 하루 API
# 호출 한도(1,000건)를 계속 위태롭게 넘나들게 되어, 요일별로 나눠 담당한다.
# 인구/충전기 밀집도가 큰 지역과 작은 지역을 섞어 하루 부담을 비슷하게
# 맞췄다 — 정확한 지역별 건수는 실측 후 필요하면 이 매핑만 조정하면 된다.
# 인덱스는 datetime.weekday() 기준 (월=0 ... 일=6).
#
# 지역명(ZCODE_NAMES)은 .github/workflows/update-chargers.yml의 region_day
# 드롭다운 문구를 자동 생성하는 데도 쓰인다(scripts/sync_region_day_options.py).
# 이 매핑을 바꾸면 그 스크립트를 다시 돌려서 워크플로 파일도 같이 갱신해야 한다.
ZCODE_NAMES = {
    "11": "서울", "12": "광주전남", "36": "세종",
    "41": "경기", "44": "충남", "50": "제주",
    "26": "부산", "30": "대전",
    "48": "경남", "43": "충북",
    "28": "인천", "51": "강원",
    "47": "경북", "52": "전북",
    "27": "대구", "31": "울산",
}

DAY_ZCODE_GROUPS = [
    ["41", "44", "50"],  # 월: 경기, 충남, 제주
    ["11", "12", "36"],  # 화: 서울, 전남광주통합, 세종
    ["26", "30"],  # 수: 부산, 대전
    ["48", "43"],  # 목: 경남, 충북
    ["28", "51"],  # 금: 인천, 강원
    ["47", "52"],  # 토: 경북, 전북
    ["27", "31"],  # 일: 대구, 울산
]

# 유효한 지역 코드 16개를 평평한 집합으로도 노출한다(geojson_store.prune_orphaned에서
# "이 zcode 중 어디에도 없으면 정상 데이터가 아니다"를 판단하는 데 쓴다).
ALL_ZCODES = {z for group in DAY_ZCODE_GROUPS for z in group}

# 가이드 문서 기준 numOfRows 최댓값(최소 10, 최대 9999). 한 페이지에 최대한
# 많이 받아와야 pageNo 호출 횟수(=API 호출 횟수)가 줄어든다. 예전엔 1000을
# 썼는데, 부산(3만여 건) 기준 33페이지 -> 4페이지로 거의 8배 줄었다.
MAX_NUM_OF_ROWS = 9999

# 정부 API는 간헐적으로 "정상 JSON인데 item이 비어 있는" 응답을 준다(대량
# 호출 직후에 주로 관측됨). 완전 수집이 필요한 조회에서 이런 빈 페이지를
# 받으면 잠깐 쉬었다가 같은 페이지를 다시 요청한다.
EMPTY_PAGE_RETRIES = 2
EMPTY_PAGE_RETRY_WAIT_SEC = 20


class ApiResponseError(RuntimeError):
    """API가 오류 resultCode를 돌려줬을 때."""


class IncompleteFetchError(RuntimeError):
    """totalCount만큼 다 받지 못했을 때(빈 응답/중간에 끊긴 응답)."""


@dataclass
class RegionFetchResult:
    """fetch_region의 결과. 완전히 수집된 지역만 stations_by_zcode에 담기고,
    나머지는 failed_zcodes로 돌려준다(호출부가 기존 데이터를 지키고 재시도
    대기로 남기도록)."""

    stations_by_zcode: dict = field(default_factory=dict)
    failed_zcodes: list = field(default_factory=list)
    quota_exceeded: bool = False


def _build_url(operation, service_key, page_no, num_of_rows, extra_params):
    params = {
        # serviceKey가 이미 URL 인코딩된 형태(data.go.kr "Encoding" 키)로 저장돼
        # 있으면 urlencode()가 %를 다시 %25로 인코딩해서 키가 깨진다(이중 인코딩).
        # 먼저 unquote로 한 번 풀어두면, 원본(Decoding) 키든 인코딩된 키든
        # urlencode()에서 정확히 한 번만 인코딩되어 항상 같은 결과가 나온다.
        "serviceKey": urllib.parse.unquote(service_key),
        "pageNo": page_no,
        "numOfRows": num_of_rows,
        "dataType": "JSON",
    }
    if extra_params:
        params.update(extra_params)
    return f"{API_BASE}/{operation}?{urllib.parse.urlencode(params)}"


def _fetch_page(operation, service_key, page_no, num_of_rows, extra_params):
    """한 페이지를 받아 (item 리스트, totalCount)로 돌려준다."""
    url = _build_url(operation, service_key, page_no, num_of_rows, extra_params)
    data = fetch_json(url, quota_marker=QUOTA_MARKER)

    result_code = data.get("resultCode")
    if result_code not in (None, "00"):
        raise ApiResponseError(f"API 오류(resultCode={result_code}): {data.get('resultMsg')}")

    page_items = data.get("items", {})
    page_items = page_items.get("item", []) if isinstance(page_items, dict) else []
    if isinstance(page_items, dict):
        page_items = [page_items]

    if not page_items:
        # 빈 응답이 실제로 어떤 모양인지 로그에 남긴다(원인 추적용 — 키 이름과
        # 결과 코드만 찍고 본문/인증키는 찍지 않는다).
        print(
            f"[{operation}]   빈 페이지 {page_no}: resultCode={result_code!r} "
            f"resultMsg={data.get('resultMsg')!r} totalCount={data.get('totalCount')!r} "
            f"keys={sorted(data.keys())}"
        )
    return page_items, int(data.get("totalCount") or 0)


def _fetch_all_pages(operation, service_key, extra_params=None, num_of_rows=MAX_NUM_OF_ROWS,
                     require_complete=False):
    """totalCount에 도달할 때까지 pageNo를 늘려가며 전체 item을 모아 반환한다.

    require_complete=True면 빈 페이지를 "수집 끝"으로 보지 않는다 — 같은
    페이지를 다시 요청해보고, 그래도 totalCount만큼 못 채우면
    IncompleteFetchError를 낸다(일부만 받은 결과로 기존 데이터를 덮어쓰지
    않기 위함). False면 빈 페이지나 덜 찬 페이지(=마지막 페이지)에서 조용히
    멈춘다 — 조회 도중 건수가 계속 변해 totalCount와 실제 건수가 어긋나는
    델타 피드(getChargerStatus)용이다. 덜 찬 페이지에서 멈추지 않으면
    totalCount를 채우려고 있지도 않은 다음 페이지를 한 번 더 요청하게 된다.
    """
    items = []
    page_no = 1
    total_count = None
    empty_retries_left = EMPTY_PAGE_RETRIES if require_complete else 0

    while True:
        page_items, reported_total = _fetch_page(operation, service_key, page_no, num_of_rows, extra_params)
        if total_count is None or (not items and reported_total):
            total_count = reported_total

        if not page_items:
            if not require_complete:
                break
            if empty_retries_left > 0:
                empty_retries_left -= 1
                time.sleep(EMPTY_PAGE_RETRY_WAIT_SEC)
                continue
            raise IncompleteFetchError(
                f"{operation} {extra_params}: {len(items)}/{total_count}건만 수집됨(페이지 {page_no}가 비어 있음)"
            )

        items.extend(page_items)
        # 페이지가 많을 때 로그가 쓸데없이 길어지지 않도록 5페이지마다만 남긴다.
        if page_no == 1 or page_no % 5 == 0:
            print(f"[{operation}]   페이지 {page_no}: 누적 {len(items)}/{total_count}")

        if len(items) >= total_count:
            break
        if not require_complete and len(page_items) < num_of_rows:
            break
        page_no += 1
        time.sleep(0.2)

    print(f"[{operation}] extra_params={extra_params} 총 {len(items)}건 수집")
    return items


def passes_filter(item):
    if item.get("delYn") == "Y":
        return False

    stat_nm = item.get("statNm") or ""
    kind_detail = item.get("kindDetail")

    force_include = any(keyword in stat_nm for keyword in FORCE_INCLUDE_NAME_KEYWORDS)
    if not force_include and kind_detail in SCHOOL_KIND_DETAILS:
        force_include = any(keyword in stat_nm for keyword in FORCE_INCLUDE_SCHOOL_KEYWORDS)

    if not force_include and kind_detail in EXCLUDED_KIND_DETAILS:
        return False

    text_fields = f"{item.get('limitDetail') or ''} {item.get('useTime') or ''}"
    if any(keyword in text_fields for keyword in NON_OPEN_KEYWORDS):
        return False
    if (item.get("useTime") or "").strip().startswith(NOT_OPENED_USE_TIME_PREFIX):
        return False

    if any(keyword in stat_nm for keyword in RESTRICTED_NAME_KEYWORDS):
        return False

    if item.get("limitYn") != "N":
        return False
    if item.get("chgerType") not in ALLOWED_CHGER_TYPES:
        return False
    return True


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_stations(items):
    """필터를 통과한 item들을 statId 기준으로 묶어 station dict 리스트로 만든다.
    (GeoJSON feature 변환은 geojson_store의 책임이라 여기선 순수 데이터만 만든다.)
    """
    stations = OrderedDict()
    skipped_stat_ids = set()  # 좌표 문제로 제외 결정된 충전소(같은 충전소의
                               # 다른 커넥터 item이 뒤에 또 나와도 다시 검사 안 함)
    skipped_bad_coord = 0
    skipped_coord_mismatch = 0
    corrected_by_kakao = 0

    for item in items:
        if not passes_filter(item):
            continue

        stat_id = item.get("statId")
        if stat_id in skipped_stat_ids:
            continue

        lat = to_float(item.get("lat"))
        lng = to_float(item.get("lng"))
        if not stat_id or lat is None or lng is None:
            skipped_bad_coord += 1
            continue

        if stat_id not in stations:
            # 충전소 하나에 커넥터가 여러 개면 같은 statId로 여러 번 나오는데,
            # 주소 정리/지오코딩은 처음 등장할 때 딱 한 번만 한다 (안 그러면
            # 커넥터 개수만큼 같은 주소를 카카오에 중복 조회하게 된다).
            addr, extracted_location = split_addr_and_location(item.get("addr"))

            # 카카오 지오코딩으로 좌표를 검증/보정한다 (KAKAO_REST_API_KEY가
            # 있을 때만 실제로 호출됨). 뒤섞인 원본 대신 정리된 주소를 보내야
            # 인식률이 올라간다. 지오코딩이 실패하면(키 미설정, 주소 인식 불가
            # 등) 기존의 대략적인 도(道) 사각형 검증으로 최소한의 안전망만
            # 적용한다.
            lat, lng, geo_status = verify_and_correct(addr, lat, lng)
            if geo_status == "corrected":
                corrected_by_kakao += 1
                print(f"  안내: 좌표 보정(카카오) — {item.get('statNm', '')} ({addr})")
            elif geo_status == "unavailable" and not coordinates_plausible(addr, lat, lng):
                # 원본 API 데이터 자체의 좌표 오류로 보인다 (예: 주소는 대전인데
                # 좌표는 이천 근방). 필터 텍스트로는 못 잡는 문제라 여기서 걸러낸다.
                skipped_coord_mismatch += 1
                print(f"  경고: 좌표 불일치로 제외 — {item.get('statNm', '')} ({addr})")
                skipped_stat_ids.add(stat_id)
                continue

            # addr에서 분리해낸 시설명(있으면 먼저)과 API 자체 location 값을
            # 둘 다 있으면 이어붙인다 (예: 괄호 부가설명 "기초과학연구원 본원"
            # + API가 준 "지상1층 주차장" -> "기초과학연구원 본원 지상1층 주차장").
            location = " ".join(
                p for p in (extracted_location, (item.get("location") or "").strip()) if p
            )

            stations[stat_id] = {
                "statId": stat_id,
                "statNm": item.get("statNm", ""),
                "addr": addr,
                "addrDetail": item.get("addrDetail", ""),
                "location": location,
                "useTime": item.get("useTime", ""),
                # useTime 원본 텍스트를 프론트엔드가 바로 쓸 수 있게 구조화해둔다
                # (예: {"kind":"weekday_only","start":540,"end":1080}).
                # "지금 몇 시인지"는 브라우저에서 계속 바뀌는 값이라 서버가
                # 미리 "지금 열려있는지"까지 계산해둘 수는 없고, 여기서는
                # 텍스트 파싱까지만 한다.
                "hours": parse_use_time(item.get("useTime", "")),
                "busiNm": item.get("busiNm", ""),
                "busiCall": item.get("busiCall", ""),
                "parkingFree": item.get("parkingFree", ""),
                "limitYn": item.get("limitYn", ""),
                # limitYn=N이어도 limitDetail에 내용이 남아있는 경우가 있어
                # 그대로 보존한다 (추후 필터 조건 보강용 참고 정보).
                "limitDetail": item.get("limitDetail", ""),
                "floorType": item.get("floorType", ""),
                "floorNum": item.get("floorNum", ""),
                # 지역별 부분 교체(geojson_store.replace_regions)를 위해 보존한다.
                "zcode": item.get("zcode", ""),
                "isTesla": False,
                "lat": lat,
                "lng": lng,
                "chargers": [],
            }

        stations[stat_id]["chargers"].append(
            {
                "chgerId": item.get("chgerId", ""),
                "chgerType": item.get("chgerType", ""),
                "chgerTypeName": CHGER_TYPE_NAMES.get(item.get("chgerType"), item.get("chgerType")),
                "stat": item.get("stat", ""),
                "statName": STAT_NAMES.get(item.get("stat"), item.get("stat")),
                "output": item.get("output", ""),
                "method": item.get("method", ""),
                "statUpdDt": item.get("statUpdDt", ""),
            }
        )

    if skipped_bad_coord:
        print(f"좌표 누락/오류로 제외: {skipped_bad_coord}건")
    if skipped_coord_mismatch:
        print(f"주소-좌표 불일치로 제외: {skipped_coord_mismatch}건")
    if corrected_by_kakao:
        print(f"카카오 지오코딩으로 좌표 보정: {corrected_by_kakao}건")

    return list(stations.values())


def fetch_region(service_key, zcodes):
    """주어진 zcode들을 지역별로 받아 RegionFetchResult로 반환한다.

    완전히 수집된 지역만 필터링된 station dict 리스트로 담긴다. 응답이
    비었거나 중간에 끊긴 지역, 요청 자체가 실패한 지역은 failed_zcodes에
    들어간다. 한도초과 쿨다운 중이거나 도중에 한도초과가 감지되면 아직
    못 받은 지역을 전부 실패로 돌리고 quota_exceeded를 표시한다.
    """
    result = RegionFetchResult()
    if in_cooldown():
        print("[gov_charger_api] 오늘 API 한도 초과로 쿨다운 중 — 지역 갱신 건너뜀")
        result.failed_zcodes = list(zcodes)
        result.quota_exceeded = True
        return result

    for index, zcode in enumerate(zcodes):
        try:
            items = _fetch_all_pages("getChargerInfo", service_key, {"zcode": zcode}, require_complete=True)
        except QuotaExceededError:
            until = start_cooldown()
            print(f"[gov_charger_api] API 한도 초과 감지 — {until.isoformat()}까지 쿨다운 시작")
            result.failed_zcodes.extend(zcodes[index:])
            result.quota_exceeded = True
            break
        except (IncompleteFetchError, ApiResponseError, FetchError) as err:
            name = ZCODE_NAMES.get(zcode, zcode)
            print(f"경고: {name}({zcode}) 수집 실패 — 기존 데이터를 유지합니다: {err}")
            result.failed_zcodes.append(zcode)
            continue
        result.stations_by_zcode[zcode] = build_stations(items)
    return result


def fetch_status_delta(service_key, period_min):
    """getChargerStatus 델타 피드를 가공 없이 그대로 반환한다.

    period_min은 API 문서 기준 1~10만 유효하다(기본값 5). 이보다 크게 주면
    그 사이 변경분을 놓칠 수 있어(문서에 명시된 범위를 벗어남), 호출 주기와
    period를 항상 10분/10 이하로 맞춰야 한다.

    한도초과 쿨다운 중이거나, 이번 호출에서 한도초과가 감지되면 None을 반환한다.
    """
    if in_cooldown():
        print("[gov_charger_api] 오늘 API 한도 초과로 쿨다운 중 — 상태 갱신 건너뜀")
        return None
    try:
        return _fetch_all_pages("getChargerStatus", service_key, {"period": period_min})
    except QuotaExceededError:
        until = start_cooldown()
        print(f"[gov_charger_api] API 한도 초과 감지 — {until.isoformat()}까지 쿨다운 시작")
        return None
