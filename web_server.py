"""KTX 출발·도착·시간 조회 웹서버.

실행::

    python3 -m uvicorn web_server:app --host 127.0.0.1 --port 8000

브라우저에서 http://localhost:8000 접속 → 폼 입력 → 조회.

/api/settings, /api/watcher/* 등이 인증 없이 로그인 정보·카드번호를 다루므로
반드시 127.0.0.1(로컬)로만 바인딩한다. LAN 공유가 꼭 필요하면 리버스 프록시나
방화벽으로 접근을 제한한 뒤에만 --host 0.0.0.0 을 고려할 것.

기존 ktx_watcher 의 Playwright 검색 모듈을 재사용하지만
필터(좌석 잔여/시간 허용) 는 풀어 두어 해당 날짜의 *모든* 스케줄을 반환한다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import subprocess
import threading
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from ktx_watcher.chrome_launcher import ChromeLauncher
from ktx_watcher.config import KTXAConfig
from ktx_watcher.korail.client import KorailSPAClient
from ktx_watcher.korail import search as ktx_search


LOGGER = logging.getLogger("web_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")

app = FastAPI(title="binjari — KTX 조회 웹서버")

ARTIFACT_ROOT = Path("/tmp/ktx_web/artifacts")
ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)

ENV_PATH = Path(".env.ktx")
ENV_EXAMPLE_PATH = Path(".env.ktx.example")

# /settings 에서 편집을 허용하는 키 (.env.ktx.example 과 동일 집합 유지)
SETTINGS_KEYS = [
    # 자격증명
    "KTXA_USER", "KTXA_PASS",
    # 결제 (카드)
    "PAY_CARD_NUM", "PAY_CARD_MM", "PAY_CARD_YY",
    "PAY_CARD_PW2", "PAY_ID6", "PAY_CARD_COMPANY",
    # 여정
    "KTXA_ORIGIN", "KTXA_DEST", "KTXA_DATE", "KTXA_TIMES",
    "KTXA_TOLERANCE_MIN", "KTXA_TIME_WINDOW", "KTXA_RESERVE_LIMIT",
    "KTXA_PASSENGERS", "KTXA_PASSENGER_TYPE",
    "KTXA_SEAT_CLASS", "KTXA_SEATED_ONLY", "KTXA_TRAIN_NO",
    "KTXA_TRAIN_TYPE", "KTXA_INCLUDE_SRT",
    # 승차권 전달
    "KTXA_TRANSFER_ENABLED", "KTXA_TRANSFER_SEND",
    "KTXA_TRANSFER_MEMBER_NO", "KTXA_TRANSFER_NAME", "KTXA_TRANSFER_PHONE",
    # 워처 동작
    "KTXA_HUMANIZE", "KTXA_MODE", "KTXA_PAYMENT_MODE", "KTXA_ONCE",
    "KTXA_POLL_MIN", "KTXA_POLL_MAX", "KTXA_LOG_DIR", "KTXA_LOG_LEVEL",
    # 브라우저 (CDP)
    "KTXA_CDP_PORT", "KTXA_CDP_USER_DATA_DIR", "KTXA_CDP_STARTUP_TIMEOUT",
    "KTXA_VDESK",
    # Teams 알림
    "TEAMS_ENABLED", "TEAMS_USER_EMAIL", "TEAMS_CHAT_ID",
    "TEAMS_RECIPIENT_NAME", "TEAMS_PREFIX",
    # Azure AD OAuth
    "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID",
    "AZURE_REDIRECT_URI", "AZURE_AUTHORITY", "AZURE_SCOPES", "DB_PATH",
]


def _env_source_lines() -> List[str]:
    """.env.ktx 원본 라인. 없으면 .env.ktx.example 을 초기 양식으로 사용."""
    src = ENV_PATH if ENV_PATH.is_file() else ENV_EXAMPLE_PATH
    if not src.is_file():
        return []
    return src.read_text(encoding="utf-8").splitlines()


def _read_env_values() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in _env_source_lines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip()
    return out


def _write_env_values(updates: Dict[str, str]) -> None:
    """.env.ktx 를 주석·순서 보존한 채 갱신. 없던 키는 말미에 추가."""
    lines = _env_source_lines()
    remaining = dict(updates)
    new_lines: List[str] = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.partition("=")[0].strip()
            if k in remaining:
                line = f"{k}={remaining.pop(k)}"
        new_lines.append(line)
    if remaining:
        new_lines += ["", "# ─ 웹 설정(/settings)에서 추가된 키 ─"]
        new_lines += [f"{k}={v}" for k, v in remaining.items()]
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    try:
        # 로그인 비밀번호·카드번호 등 평문 비밀값이 들어있으므로 소유자만 읽게 제한
        # (Windows 는 os.chmod 가 사실상 no-op — ACL 이 아닌 read-only 비트만 영향)
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        LOGGER.warning(".env.ktx 권한 설정 실패 (무시하고 계속)")


def _validate_env(values: Dict[str, str]) -> Optional[str]:
    """저장된 값으로 KTXAConfig 검증. 문제 있으면 메시지 반환(저장은 이미 완료)."""
    try:
        KTXAConfig.model_validate(values)
        return None
    except Exception as exc:
        return str(exc)

# Playwright sync API 는 동일 프로세스에서 동시 실행이 까다로워 잠금.
_KTX_LOCK = threading.Lock()

# ─ 역 목록 (2026-08-18 korail.com 역 선택 팝업에서 수집한 사이트 표기 그대로) ─
# 주요역: 팝업 '주요역' 탭 45개 (표시 순서 유지)
STATIONS_MAJOR = [
    "서울", "용산", "광명", "수서", "영등포", "수원", "평택", "천안아산",
    "천안", "오송", "조치원", "대전", "서대전", "김천구미", "구미", "동대구",
    "대구", "경주", "울산(통도사)", "포항", "경산", "밀양", "부산", "구포",
    "창원중앙", "평창", "진부(오대산)", "강릉", "익산", "전주", "광주송정", "목포",
    "순천", "청량리", "여수EXPO", "동해", "정동진", "안동", "서원주", "원주",
    "마산", "행신", "나주", "정읍", "남원",
]
# 전체: 팝업 '지역별 > 전체' 281개 (가나다순)
STATIONS_ALL = [
    "가남", "가평", "각계", "감곡장호원", "강경", "강구", "강릉", "강진",
    "강촌", "개포", "경산", "경주", "계룡", "고래불", "고한", "곡성",
    "공주", "광명", "광양", "광주", "광주송정", "광천", "구례구", "구미",
    "구포", "군북", "군산", "군위", "극락강", "근덕", "기성", "기장",
    "김제", "김천", "김천구미", "나전", "나주", "남성현", "남원", "남창",
    "남춘천", "논산", "능주", "다시", "단양", "대곡", "대구", "대야",
    "대전", "대천", "덕소", "도계", "도고온천", "도라산", "동대구", "동백산",
    "동탄", "동해", "둔내", "득량", "마산", "마석", "만종", "매곡",
    "매화", "명봉", "목포", "몽탄", "무안", "묵호", "문경", "문산",
    "물금", "민둥산", "밀양", "반성", "백양리", "백양사", "벌교", "별어곡",
    "보성", "신보성", "봉양", "봉화", "부강", "부발", "부산", "부전",
    "북영천", "북울산", "북천", "분천", "비동", "사릉", "사북", "사상",
    "살미", "삼랑진", "삼례", "삼산", "삼척", "삼척해변", "삼탄", "삽교",
    "상동", "상봉", "상주", "서경주", "서광주", "서대구", "서대전", "서울",
    "서원주", "서정리", "서천", "서화성", "석불", "석포", "선평", "성환",
    "센텀", "송추", "수서", "수안보온천", "수원", "순천", "승부", "신기",
    "신동", "신례원", "신창", "신탄진", "신태인", "신해운대", "심천", "쌍룡",
    "아산", "아우라지", "아화", "안강", "안동", "안양", "안중", "앙성온천",
    "약목", "양동", "양원", "양평", "여수EXPO", "여천", "연산", "연풍",
    "영덕", "영동", "영등포", "영암", "영월", "영주", "영천", "영해",
    "예당", "예미", "예산", "예천", "오근장", "오산", "오송", "오수",
    "옥산", "옥수", "옥원", "옥천", "온양온천", "완사", "왕십리", "왜관",
    "용궁", "용문", "용산", "운천", "울산(통도사)", "울진", "웅천", "원동",
    "원릉", "원주", "월포", "음성", "의성", "의정부", "이양", "이원",
    "익산", "인주", "인천공항T1", "인천공항T2", "일로", "일신", "일영", "임기",
    "임성리", "임실", "임원", "임진강", "장사", "장성", "장항", "장흥",
    "장동", "전의", "전주", "전남장흥", "점촌", "정동진", "정선", "정읍",
    "제천", "조성", "조치원", "주덕", "죽변", "중리", "증평", "지탄",
    "지평", "진례", "진부(오대산)", "진상", "진영", "진주", "창원", "창원중앙",
    "천안", "천안아산", "철암", "청도", "청량리", "청리", "청소", "청주",
    "청주공항", "청평", "추암", "추풍령", "춘양", "춘천", "충주", "태백",
    "태화강", "퇴계원", "판교(경기)", "판교(충남)", "평내호평", "평창", "평택", "평택지제",
    "평해", "포항", "풍기", "하동", "하양", "한림정", "함안", "함열",
    "함창", "함평", "합덕", "해남", "행신", "향남", "현동", "홍성",
    "화명", "화성시청", "화순", "황간", "횡성", "횡천", "효천", "후포",
    "흥부",
]


# ───────────────────────── Request / Response ─────────────────────────

class SearchRequest(BaseModel):
    origin: str = Field(..., description="출발역 (예: 수서, 서울)")
    dest: str = Field(..., description="도착역 (예: 부산)")
    date: str = Field(..., description="YYYY-MM-DD")
    earliest_hour: int = Field(6, ge=0, le=23, description="조회 시작 시각 (시)")
    latest_hour: Optional[int] = Field(None, ge=1, le=24, description="조회 종료 시각 (정각까지 포함, 없으면 제한 없음)")
    train_no: str = Field("", description="열차번호로 필터 (예: 101, 비우면 전체)")
    passengers: int = Field(1, ge=1, le=9)
    force: bool = Field(False, description="true 면 캐시 무시하고 사이트 재조회")
    include_srt: Optional[bool] = Field(None, description="사이트 조회 시 'SRT 함께 보기' (None 이면 .env.ktx 값)")


def _parse_date(s: str) -> _date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date 는 YYYY-MM-DD 형식") from exc


# ───────────────────────── KTX ─────────────────────────

def _build_ktxa_config(req: SearchRequest) -> KTXAConfig:
    """요청 값 + .env.ktx (CDP 포트 등) 를 합쳐 search 모드 config 생성."""
    env = _read_env_values()
    env.update({
        "KTXA_ORIGIN": req.origin,
        "KTXA_DEST": req.dest,
        "KTXA_DATE": req.date,
        "KTXA_TIMES": f"{req.earliest_hour:02d}:00",
        "KTXA_PASSENGERS": str(req.passengers),
        "KTXA_PASSENGER_TYPE": "어른",
        "KTXA_SEAT_CLASS": "",
        "KTXA_SEATED_ONLY": "false",
        "KTXA_TIME_WINDOW": "",
        "KTXA_TOLERANCE_MIN": "1440",
        "KTXA_MODE": "search",
        "TEAMS_ENABLED": "false",
    })
    if req.include_srt is not None:
        env["KTXA_INCLUDE_SRT"] = "true" if req.include_srt else "false"
    return KTXAConfig.model_validate(env)


_TRAIN_NO_RE = re.compile(
    r"(?:KTX[-\w]*|SRT|새마을\w*|무궁화\w*|ITX[-\w]*|누리로|청룡)\s*(\d{2,4})"
)


def _shape_rows(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in raw:
        raw_text = r.get("depart_raw", "")
        hhmm = re.findall(r"\d{2}:\d{2}", raw_text)
        no_m = _TRAIN_NO_RE.search(raw_text)
        out.append({
            "train_type": r.get("train_name", ""),
            "train_no": no_m.group(1) if no_m else "",
            "depart": r.get("depart", ""),
            "arrive": hhmm[1] if len(hhmm) > 1 else "",
            "first": r.get("first_status", ""),
            "general": r.get("general_status", ""),
        })
    return out


def _live_fetch(config: KTXAConfig) -> List[Dict[str, Any]]:
    """사이트 실조회. 간헐적 '열차 없음' 오탐이 있어 0건이면 reload 재시도."""
    launcher = ChromeLauncher(
        port=config.ktxa_cdp_port,
        user_data_dir=config.ktxa_cdp_user_data_dir,
        exe_path=config.ktxa_chrome_exe,
        startup_timeout=config.ktxa_cdp_startup_timeout,
        vdesk=config.ktxa_vdesk,
    )
    cdp_url = launcher.ensure_running()
    # Chrome 은 재사용을 위해 종료하지 않는다 (워처와 동일 정책)
    with KorailSPAClient(cdp_url) as client:
        page = ktx_search.navigate_to_search(client)
        ktx_search.fill_search_form(page, config)
        raw = ktx_search.submit_search(page)
        # 사이트가 방금 채운 조건인데도 '운행하는 열차가 없습니다' 를 내는 일시 상태가
        # 있다 (2026-08-18 CDP 캡쳐로 확인, 새로고침 시 정상). 최대 2회 재시도.
        for attempt in range(2):
            if raw:
                break
            LOGGER.warning("결과 0건 — 새로고침 재시도 %d/2", attempt + 1)
            page.reload(wait_until="domcontentloaded", timeout=20_000)
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
            page.wait_for_timeout(1_500)
            raw = ktx_search._parse_result_rows(page)
        # 사이트는 첫 화면에 일부만 주고 '더보기' 로 이어 붙인다.
        # 하루 전체를 캐시하기 위해 더보기를 끝까지 펼친 뒤 다시 파싱.
        expanded = False
        for _ in range(20):
            btn = page.get_by_role("button", name="더보기").first
            if btn.count() == 0 or not btn.is_visible():
                btn = page.get_by_text("더보기", exact=True).first
                if btn.count() == 0 or not btn.is_visible():
                    break
            try:
                btn.click(timeout=3_000)
            except Exception as e:
                LOGGER.debug("더보기 클릭 중단: %s", e)
                break
            expanded = True
            page.wait_for_timeout(1_200)
        if expanded:
            raw = ktx_search._parse_result_rows(page)
    return _shape_rows(raw)


# ─ 시간표 캐시: (출발|도착|날짜) 별로 사이트 조회 결과 저장, 지난 날짜·시각은 삭제 ─
CACHE_PATH = Path("./runs/timetable_cache.json")
_CACHE_LOCK = threading.Lock()


def _cache_load() -> Dict[str, Any]:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _cache_save(cache: Dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def _dep_minutes(row: Dict[str, Any]) -> Optional[int]:
    m = re.fullmatch(r"(\d{2}):(\d{2})", row.get("depart", ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _cache_purge(cache: Dict[str, Any]) -> None:
    """지난 날짜 엔트리 삭제 + 오늘 엔트리의 이미 출발한 행 삭제."""
    now = datetime.now()
    today = now.date().isoformat()
    for key in list(cache):
        entry = cache[key]
        d = entry.get("date", "")
        if d < today:
            del cache[key]
        elif d == today:
            cur = now.hour * 60 + now.minute
            entry["rows"] = [
                r for r in entry.get("rows", [])
                if (_dep_minutes(r) is None or _dep_minutes(r) >= cur)
            ]


def _ktx_list_schedules(req: SearchRequest) -> Dict[str, Any]:
    """시간표 반환. 캐시가 요청 범위를 덮으면 재조회 없이 캐시 사용 (force 로 무시)."""
    key = f"{req.origin.strip()}|{req.dest.strip()}|{req.date}"

    with _CACHE_LOCK:
        cache = _cache_load()
        _cache_purge(cache)
        entry = cache.get(key)
        usable = (
            not req.force
            and entry is not None
            and entry.get("from_hour", 99) <= req.earliest_hour
        )
        _cache_save(cache)  # purge(지난 날짜·시각 삭제) 반영
        if usable:
            rows_all = entry["rows"]
            fetched_at = entry.get("fetched_at", "")

    if not usable:
        if _watcher_running():
            raise HTTPException(
                status_code=409,
                detail="감시 실행 중에는 사이트 조회를 할 수 없습니다 (같은 Chrome 을 사용) — "
                       "감시를 중지 후 재조회하거나 저장된 시간표 범위로 조회하세요",
            )
        config = _build_ktxa_config(req)
        with _KTX_LOCK:
            rows_all = _live_fetch(config)
        fetched_at = datetime.now().strftime("%H:%M")
        with _CACHE_LOCK:
            cache = _cache_load()
            _cache_purge(cache)
            cache[key] = {
                "origin": req.origin.strip(), "dest": req.dest.strip(), "date": req.date,
                "from_hour": req.earliest_hour, "fetched_at": fetched_at, "rows": rows_all,
            }
            _cache_save(cache)

    # 요청 범위 필터 (시간대·열차번호·오늘이면 지난 시각 제외)
    want_no = req.train_no.strip().lstrip("0")
    now = datetime.now()
    cur = now.hour * 60 + now.minute if req.date == now.date().isoformat() else None
    rows: List[Dict[str, Any]] = []
    for r in rows_all:
        minutes = _dep_minutes(r)
        if minutes is not None:
            if minutes < req.earliest_hour * 60:
                continue
            if req.latest_hour is not None and minutes > req.latest_hour * 60:
                continue
            if cur is not None and minutes < cur:
                continue
        if want_no and r.get("train_no", "").lstrip("0") != want_no:
            continue
        rows.append(r)

    LOGGER.info("KTX 결과 %d건 (%s, 필터: %02d시~%s, 열차번호=%s)",
                len(rows), "캐시" if usable else "실조회", req.earliest_hour,
                f"{req.latest_hour:02d}시" if req.latest_hour is not None else "무제한",
                req.train_no or "전체")
    return {"rows": rows, "cached": usable, "fetched_at": fetched_at}


# ───────────────────────── Web UI ─────────────────────────

APP_CSS = r"""
:root {
  color-scheme: light;
  --bg: #f5f5f7;
  --surface: rgba(255, 255, 255, .88);
  --surface-solid: #ffffff;
  --surface-muted: #f2f2f4;
  --text: #1d1d1f;
  --text-secondary: #6e6e73;
  --text-tertiary: #86868b;
  --line: rgba(0, 0, 0, .09);
  --line-strong: rgba(0, 0, 0, .16);
  --blue: #0071e3;
  --blue-hover: #0077ed;
  --blue-pressed: #006edb;
  --blue-soft: #e8f2ff;
  --green: #248a3d;
  --green-soft: #eaf7ed;
  --orange: #b25000;
  --orange-soft: #fff4e8;
  --red: #d70015;
  --red-soft: #fff0f1;
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, .04), 0 7px 22px rgba(0, 0, 0, .05);
  --shadow-lg: 0 20px 60px rgba(0, 0, 0, .10);
  --radius-sm: 12px;
  --radius-md: 18px;
  --radius-lg: 24px;
  --content: 1440px;
}

* {
  box-sizing: border-box;
}

html {
  min-width: 320px;
  scroll-behavior: smooth;
}

body {
  min-height: 100vh;
  margin: 0;
  background:
    radial-gradient(circle at 12% -8%, rgba(0, 113, 227, .10), transparent 29rem),
    var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text",
    "Apple SD Gothic Neo", "Noto Sans KR", "Segoe UI", sans-serif;
  font-size: 15px;
  line-height: 1.45;
  letter-spacing: -.012em;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

a {
  color: inherit;
}

button,
input,
select,
textarea {
  font: inherit;
}

button,
a,
input,
select,
textarea,
summary {
  -webkit-tap-highlight-color: transparent;
}

button {
  border: 0;
}

:focus-visible {
  outline: 4px solid rgba(0, 113, 227, .24);
  outline-offset: 2px;
}

::selection {
  background: rgba(0, 113, 227, .2);
}

.sr-only {
  width: 1px;
  height: 1px;
  position: absolute;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  clip-path: inset(50%);
}

.site-header {
  position: sticky;
  top: 0;
  z-index: 100;
  border-bottom: 1px solid rgba(0, 0, 0, .07);
  background: rgba(250, 250, 252, .76);
  -webkit-backdrop-filter: saturate(180%) blur(22px);
  backdrop-filter: saturate(180%) blur(22px);
}

.nav-shell {
  width: min(calc(100% - 40px), var(--content));
  min-height: 68px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  gap: 22px;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto;
  color: var(--text);
  text-decoration: none;
}

.brand-mark {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  background: linear-gradient(145deg, #2997ff 0%, #0071e3 54%, #0057b8 100%);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .4), 0 5px 14px rgba(0, 113, 227, .22);
  color: #fff;
  font-size: 19px;
  font-weight: 750;
  letter-spacing: -.06em;
}

.brand-copy {
  display: grid;
  line-height: 1.05;
}

.brand-copy strong {
  font-size: 16px;
  font-weight: 720;
  letter-spacing: -.025em;
}

.brand-copy small {
  margin-top: 4px;
  color: var(--text-tertiary);
  font-size: 10px;
  font-weight: 540;
  letter-spacing: -.01em;
}

.tabs,
.site-nav {
  display: flex;
  align-items: center;
  gap: 3px;
  margin: 0;
  padding: 4px;
  border: 0;
  border-radius: 11px;
  background: rgba(118, 118, 128, .12);
}

.tab,
.navtab {
  min-height: 32px;
  padding: 7px 14px;
  border-radius: 8px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 570;
  line-height: 18px;
  text-decoration: none;
  white-space: nowrap;
  transition: color .18s ease, background .18s ease, box-shadow .18s ease;
}

.tab:hover,
.navtab:hover {
  color: var(--text);
}

.tab.active,
.navtab.on {
  background: rgba(255, 255, 255, .94);
  box-shadow: 0 1px 4px rgba(0, 0, 0, .10);
  color: var(--text);
  font-weight: 650;
}

#watcherBar {
  margin-left: auto;
}

.page-shell {
  width: min(calc(100% - 40px), var(--content));
  margin: 0 auto;
  padding: 42px 0 64px;
}

.hero {
  max-width: 820px;
  margin: 2px 0 34px;
}

.eyebrow {
  margin: 0 0 9px;
  color: var(--blue);
  font-size: 12px;
  font-weight: 720;
  letter-spacing: .08em;
  text-transform: uppercase;
}

.hero h1 {
  margin: 0;
  font-size: clamp(34px, 4.1vw, 56px);
  line-height: 1.05;
  letter-spacing: -.055em;
  font-weight: 730;
}

.hero p:not(.eyebrow) {
  max-width: 650px;
  margin: 15px 0 0;
  color: var(--text-secondary);
  font-size: clamp(16px, 1.6vw, 20px);
  line-height: 1.5;
  letter-spacing: -.022em;
}

.settings-hero {
  grid-column: 1 / -1;
  margin-bottom: 26px;
}

.settings-hero h1 {
  margin: 0;
  font-size: clamp(30px, 3.2vw, 44px);
  line-height: 1.08;
  letter-spacing: -.045em;
}

.settings-hero p:last-child {
  margin: 10px 0 0;
  color: var(--text-secondary);
  font-size: 16px;
}

.layout,
.settings-main {
  display: flex;
  align-items: flex-start;
  gap: 20px;
}

.main,
.cols {
  flex: 1;
  min-width: 0;
}

.card,
.search-card,
#result:not(:empty) {
  border: 1px solid rgba(255, 255, 255, .74);
  border-radius: var(--radius-lg);
  background: var(--surface);
  box-shadow: var(--shadow-sm);
  -webkit-backdrop-filter: blur(18px);
  backdrop-filter: blur(18px);
}

.search-card {
  padding: clamp(22px, 3vw, 34px);
}

.section-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 24px;
}

.section-heading h2,
.result-head h2 {
  margin: 0;
  color: var(--text);
  font-size: 24px;
  line-height: 1.18;
  letter-spacing: -.035em;
}

.section-heading p,
.result-head p {
  margin: 7px 0 0;
  color: var(--text-secondary);
  font-size: 14px;
}

#routeChips:empty {
  display: none;
}

.search-card #routeChips {
  margin: -4px 0 20px;
}

#qform {
  display: grid;
  grid-template-columns: repeat(12, minmax(0, 1fr));
  gap: 18px 12px;
  align-items: end;
}

.form-field,
.field {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.form-field > label,
.field > label {
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 650;
  line-height: 1.25;
}

.origin-field {
  grid-column: span 3;
}

.swap-field {
  grid-column: span 1;
  align-self: center;
  justify-self: center;
}

.destination-field {
  grid-column: span 3;
}

.date-field {
  grid-column: span 3;
}

.passenger-field {
  grid-column: span 2;
}

.time-start-field,
.time-end-field {
  grid-column: span 3;
}

.train-number-field {
  grid-column: span 3;
}

.optional {
  color: var(--text-tertiary);
  font-size: 11px;
  font-weight: 500;
}

input,
select,
textarea {
  width: 100%;
  min-height: 46px;
  padding: 11px 13px;
  border: 1px solid var(--line-strong);
  border-radius: 12px;
  background: rgba(255, 255, 255, .9);
  color: var(--text);
  box-shadow: 0 1px 1px rgba(0, 0, 0, .02);
  font-size: 14px;
  transition: border-color .18s ease, box-shadow .18s ease, background .18s ease;
}

input:hover,
select:hover,
textarea:hover {
  border-color: rgba(0, 0, 0, .28);
}

input:focus,
select:focus,
textarea:focus {
  outline: none;
  border-color: var(--blue);
  background: #fff;
  box-shadow: 0 0 0 4px rgba(0, 113, 227, .13);
}

input::placeholder,
textarea::placeholder {
  color: #a2a2a7;
}

input[type="checkbox"],
input[type="radio"] {
  width: 18px;
  min-height: 18px;
  padding: 0;
  accent-color: var(--blue);
}

.swap-route,
.swap {
  min-width: 44px;
  min-height: 44px;
  padding: 0 13px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface-muted);
  color: var(--text);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .65);
  cursor: pointer;
  font-weight: 650;
  transition: transform .18s ease, background .18s ease, border-color .18s ease;
}

.swap-route {
  width: 44px;
  padding: 0;
  border-radius: 50%;
  color: var(--blue);
  font-size: 19px;
}

.swap-route:hover,
.swap:hover {
  border-color: rgba(0, 113, 227, .22);
  background: var(--blue-soft);
}

.swap-route:active,
.swap:active {
  transform: scale(.96);
}

.btnrow {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 4px;
}

.primary-button,
button.pick,
.savebar button {
  min-height: 46px;
  padding: 11px 21px;
  border-radius: 12px;
  background: var(--blue);
  color: #fff;
  box-shadow: 0 5px 14px rgba(0, 113, 227, .20);
  cursor: pointer;
  font-size: 14px;
  font-weight: 670;
  letter-spacing: -.01em;
  transition: transform .18s ease, background .18s ease, box-shadow .18s ease;
}

.primary-button {
  min-width: 150px;
}

.primary-button:hover,
button.pick:hover,
.savebar button:hover {
  background: var(--blue-hover);
  box-shadow: 0 7px 18px rgba(0, 113, 227, .26);
}

.primary-button:active,
button.pick:active,
.savebar button:active {
  transform: scale(.98);
  background: var(--blue-pressed);
}

button:disabled {
  cursor: not-allowed;
  opacity: .55;
}

#status {
  min-height: 0;
  margin: 14px 2px 0;
  color: var(--text-secondary);
  font-size: 13px;
}

#status:not(:empty) {
  padding: 11px 14px;
  border: 1px solid rgba(0, 113, 227, .12);
  border-radius: 12px;
  background: rgba(232, 242, 255, .72);
}

#status a {
  color: var(--blue);
  font-weight: 620;
}

#status .pick {
  min-height: 30px;
  margin-left: 8px;
  padding: 5px 10px;
  border-radius: 9px;
  box-shadow: none;
  font-size: 11px;
}

#result:not(:empty) {
  margin-top: 20px;
  padding: clamp(18px, 2.5vw, 28px);
}

#result {
  transition: opacity .18s ease;
}

#result.is-loading {
  opacity: .52;
  pointer-events: none;
}

.result-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.result-count {
  color: var(--text-tertiary);
  font-size: 13px;
}

.filterbar {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}

.filter-label {
  margin-right: 3px;
  color: var(--text-tertiary);
  font-size: 12px;
  font-weight: 620;
}

button.tchip {
  min-height: 32px;
  padding: 6px 11px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface-muted);
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 12px;
  font-weight: 590;
  transition: background .18s ease, color .18s ease, border-color .18s ease;
}

button.tchip:hover {
  border-color: rgba(0, 113, 227, .2);
  color: var(--text);
}

button.tchip.on {
  border-color: var(--blue);
  background: var(--blue);
  color: #fff;
}

.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: rgba(255, 255, 255, .72);
}

table {
  width: 100%;
  min-width: 680px;
  border-spacing: 0;
  border-collapse: separate;
  font-size: 13px;
}

th,
td {
  padding: 13px 14px;
  border: 0;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: middle;
}

th {
  background: rgba(245, 245, 247, .84);
  color: var(--text-tertiary);
  font-size: 11px;
  font-weight: 680;
  letter-spacing: .02em;
  white-space: nowrap;
}

th:first-child {
  width: 48px;
  text-align: center;
}

td:first-child {
  text-align: center;
}

tbody tr:last-child td {
  border-bottom: 0;
}

tbody tr {
  transition: background .15s ease;
}

tbody tr:hover td,
tbody tr.is-selected td {
  background: rgba(0, 113, 227, .055);
}

.train-badge,
.pill {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 3px 9px;
  border-radius: 999px;
  background: var(--blue-soft);
  color: #0066cc;
  font-size: 11px;
  font-weight: 680;
  white-space: nowrap;
}

.train-number {
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.time-cell {
  color: var(--text);
  font-size: 15px;
  font-weight: 660;
  font-variant-numeric: tabular-nums;
  letter-spacing: -.02em;
}

.seat-cell {
  color: var(--text-secondary);
}

.empty {
  padding: 34px 20px;
  color: var(--text-tertiary);
  text-align: center;
}

#selBar {
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 12px;
  padding: 12px;
  border-radius: 14px;
  background: var(--blue-soft);
}

.selection-note {
  color: var(--text-secondary);
  font-size: 12px;
}

/* Settings */
.settings-main .cols {
  display: grid;
  grid-template-columns: repeat(12, minmax(0, 1fr));
  gap: 16px;
}

section.card {
  grid-column: 1 / -1;
  margin: 0;
  padding: clamp(20px, 2.4vw, 28px);
}

section.card h2 {
  margin: 0 0 20px;
  padding: 0;
  border: 0;
  color: var(--text);
  font-size: 19px;
  line-height: 1.2;
  letter-spacing: -.03em;
}

.card-passengers {
  grid-column: span 4;
}

.card-seat {
  grid-column: span 8;
}

.card-login {
  grid-column: span 5;
}

.card-payment {
  grid-column: span 7;
}

.row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 13px 16px;
  margin-bottom: 14px;
}

.row:last-child {
  margin-bottom: 0;
}

input.short {
  width: 96px;
}

input.mid {
  width: 170px;
}

input.long {
  width: min(100%, 320px);
}

.settings-main select {
  width: auto;
  min-width: 92px;
}

.journey-divider {
  margin-top: 6px;
  padding-top: 18px;
  border-top: 1px solid var(--line);
}

.action-cluster {
  display: flex;
  align-items: center;
  gap: 8px;
}

.swap.primary-inline {
  border-color: var(--blue);
  background: var(--blue);
  color: #fff;
  box-shadow: 0 5px 14px rgba(0, 113, 227, .18);
}

.swap.primary-inline:hover {
  background: var(--blue-hover);
}

#timeChips {
  min-height: 42px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 5px 0;
}

.chip {
  display: inline-flex;
  align-items: center;
  min-height: 34px;
  padding: 7px 12px;
  border: 1px solid rgba(0, 113, 227, .14);
  border-radius: 999px;
  background: var(--blue-soft);
  color: #005eb8;
  cursor: pointer;
  font-size: 12px;
  font-weight: 630;
  transition: background .18s ease, border-color .18s ease, color .18s ease;
}

.chip:hover {
  border-color: rgba(215, 0, 21, .18);
  background: var(--red-soft);
  color: var(--red);
}

.chip:hover::after {
  content: none;
}

.chip .chip-remove {
  margin-left: 7px;
  color: currentColor;
  font-size: 15px;
  line-height: 1;
}

.pax {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(104px, 1fr));
  gap: 10px;
}

.pax .field label {
  text-align: left;
}

.pax select {
  width: 100%;
  text-align: left;
}

.radios {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 4px;
  border-radius: 12px;
  background: rgba(118, 118, 128, .12);
}

.radios label {
  position: relative;
  min-height: 36px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 8px 13px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 13px;
  white-space: nowrap;
}

.radios input {
  position: absolute;
  width: 1px;
  height: 1px;
  min-height: 1px;
  opacity: 0;
  pointer-events: none;
}

.radios label:has(input:checked) {
  background: #fff;
  box-shadow: 0 1px 4px rgba(0, 0, 0, .11);
  color: var(--text);
}

.radios input:checked + span {
  color: var(--text);
  font-weight: 650;
}

.radios label:has(input:focus-visible) {
  outline: 4px solid rgba(0, 113, 227, .24);
}

.check {
  min-height: 36px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
  border-radius: 10px;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 13px;
  transition: background .16s ease, color .16s ease;
}

.check:hover {
  background: var(--surface-muted);
  color: var(--text);
}

.pwwrap {
  position: relative;
  display: inline-flex;
}

.pwwrap input {
  padding-right: 42px;
}

.pwwrap button {
  position: absolute;
  top: 50%;
  right: 5px;
  width: 34px;
  height: 34px;
  padding: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}

.pwwrap button:hover {
  background: var(--surface-muted);
}

details {
  margin-top: 15px;
  padding-top: 14px;
  border-top: 1px solid var(--line);
}

details summary {
  width: fit-content;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 13px;
  font-weight: 610;
}

.card-passengers details {
  margin: 0;
  padding: 0;
  border: 0;
}

.summary-title {
  color: var(--text);
  font-size: 19px;
  font-weight: 680;
  letter-spacing: -.03em;
}

.hint {
  color: var(--text-tertiary);
  font-size: 12px;
  line-height: 1.55;
}

#wlogWrap {
  grid-column: 1 / -1;
}

#wlog {
  margin-top: 12px;
  padding: 16px !important;
  border-radius: 14px !important;
  background: #1d1d1f !important;
  color: #d5f5dd !important;
  font-family: "SFMono-Regular", Consolas, monospace;
  line-height: 1.55;
}

.savebar {
  position: sticky;
  bottom: 16px;
  z-index: 40;
  grid-column: 1 / -1;
  min-height: 74px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid rgba(255, 255, 255, .8);
  border-radius: 18px;
  background: rgba(255, 255, 255, .82);
  box-shadow: var(--shadow-lg);
  -webkit-backdrop-filter: saturate(180%) blur(24px);
  backdrop-filter: saturate(180%) blur(24px);
}

.savebar button {
  padding-inline: 22px;
}

#saveStatus {
  min-width: 0;
  color: var(--text-secondary);
  font-size: 12px;
  white-space: pre-wrap;
}

#saveStatus.ok {
  color: var(--green);
}

#saveStatus.warn {
  color: var(--orange);
}

#saveStatus.err {
  color: var(--red);
}

/* Assistant */
body #chatPanel {
  width: 330px;
  height: min(680px, calc(100dvh - 112px));
  min-height: 440px;
  flex: 0 0 330px;
  position: sticky;
  top: 88px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  border: 1px solid rgba(255, 255, 255, .76);
  border-radius: var(--radius-lg);
  background: rgba(255, 255, 255, .86);
  box-shadow: var(--shadow-sm);
  -webkit-backdrop-filter: blur(20px);
  backdrop-filter: blur(20px);
}

body .chat-head {
  position: relative;
  padding: 18px 18px 16px 48px;
  border-bottom: 1px solid var(--line);
  color: var(--text);
  font-size: 14px;
  font-weight: 680;
}

body .chat-head::before {
  content: "";
  position: absolute;
  top: 17px;
  left: 17px;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: radial-gradient(circle at 32% 28%, #b8e2ff 0 13%, #2997ff 31%, #0071e3 68%, #8f5cff 100%);
  box-shadow: 0 3px 9px rgba(0, 113, 227, .24);
}

body .chat-head small {
  display: block;
  margin: 3px 0 0;
  color: var(--text-tertiary);
  font-size: 11px;
  font-weight: 500;
}

body .chat-msgs {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding: 16px;
  font-size: 13px;
  scrollbar-width: thin;
}

body .chat-msg {
  max-width: 90%;
  padding: 9px 11px;
  border-radius: 15px;
  white-space: pre-wrap;
  word-break: keep-all;
  overflow-wrap: anywhere;
  line-height: 1.5;
}

body .chat-msg.user {
  align-self: flex-end;
  border-bottom-right-radius: 5px;
  background: var(--blue);
  color: #fff;
}

body .chat-msg.bot {
  align-self: flex-start;
  border-bottom-left-radius: 5px;
  background: var(--surface-muted);
  color: var(--text);
}

body .chat-msg.err {
  align-self: flex-start;
  background: var(--red-soft);
  color: var(--red);
}

body .chat-msg.note {
  align-self: center;
  padding: 2px;
  background: transparent;
  color: var(--text-tertiary);
  font-size: 11px;
}

body .chat-input {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: end;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid var(--line);
  background: rgba(250, 250, 252, .72);
}

body .chat-input textarea {
  min-height: 46px;
  max-height: 116px;
  resize: vertical;
  border-radius: 14px;
  font-size: 13px;
}

body .chat-input button {
  min-width: 58px;
  height: 46px;
  padding: 0 12px;
  border-radius: 12px;
  background: var(--blue);
  color: #fff;
  cursor: pointer;
  font-size: 12px;
  font-weight: 650;
}

/* Frequent stations and routes */
body .stn-chips {
  max-width: 360px;
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 6px;
}

body .stn-chips button,
body #routeChips button {
  min-height: 26px;
  padding: 4px 9px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: rgba(242, 242, 244, .9);
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 11px;
  font-weight: 570;
  transition: color .16s ease, background .16s ease, border-color .16s ease;
}

body .stn-chips button:hover,
body #routeChips button:hover {
  border-color: rgba(0, 113, 227, .18);
  background: var(--blue-soft);
  color: #0066cc;
}

body .stn-chips button.fav {
  border-color: rgba(255, 159, 10, .24);
  background: var(--orange-soft);
  color: #8a4b00;
}

body .stn-chips button.drag-over {
  outline: 3px solid rgba(0, 113, 227, .2);
}

body #routeChips {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

body #routeChips .rc-label {
  margin-right: 3px;
  color: var(--text-tertiary);
  font-size: 11px;
  font-weight: 620;
}

body #routeChips button {
  min-height: 30px;
  padding-inline: 11px;
  background: rgba(255, 255, 255, .82);
}

.watcher-state {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  padding: 5px 10px;
  border-radius: 999px;
  background: rgba(118, 118, 128, .12);
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 650;
}

.watcher-state.running {
  background: var(--green-soft);
  color: var(--green);
}

.watcher-state.done {
  background: var(--green-soft);
  color: var(--green);
}

.watcher-stop {
  min-height: 28px;
  padding: 5px 10px;
  border-radius: 999px;
  background: var(--red-soft);
  color: var(--red);
  cursor: pointer;
  font-size: 11px;
  font-weight: 650;
}

@media (max-width: 1120px) {
  .layout,
  .settings-main {
    flex-direction: column;
  }

  body #chatPanel {
    width: 100%;
    height: 440px;
    min-height: 440px;
    flex-basis: auto;
    position: static;
  }
}

@media (max-width: 820px) {
  .nav-shell,
  .page-shell {
    width: min(calc(100% - 28px), var(--content));
  }

  .nav-shell {
    min-height: 62px;
    gap: 10px;
  }

  .brand-copy small {
    display: none;
  }

  #watcherBar {
    display: none !important;
  }

  .page-shell {
    padding-top: 30px;
  }

  .hero {
    margin-bottom: 26px;
  }

  #qform {
    grid-template-columns: repeat(6, minmax(0, 1fr));
  }

  .origin-field,
  .destination-field {
    grid-column: span 5;
  }

  .swap-field {
    grid-column: span 1;
  }

  .destination-field {
    grid-column: 1 / span 5;
  }

  .date-field,
  .passenger-field,
  .time-start-field,
  .time-end-field,
  .train-number-field {
    grid-column: span 3;
  }

  .card-passengers,
  .card-seat,
  .card-login,
  .card-payment {
    grid-column: 1 / -1;
  }

  input.mid,
  input.long {
    width: 100%;
  }
}

@media (max-width: 620px) {
  .nav-shell {
    align-items: center;
  }

  .brand-copy {
    display: none;
  }

  .tabs,
  .site-nav {
    margin-left: auto;
    overflow-x: auto;
  }

  .tab,
  .navtab {
    padding-inline: 10px;
    font-size: 12px;
  }

  .page-shell {
    padding: 24px 0 calc(42px + env(safe-area-inset-bottom));
  }

  .hero h1 {
    font-size: 36px;
  }

  .hero p:not(.eyebrow) {
    font-size: 16px;
  }

  .search-card,
  section.card,
  #result:not(:empty) {
    border-radius: 20px;
  }

  #qform {
    grid-template-columns: 1fr 44px;
  }

  .origin-field,
  .destination-field {
    grid-column: 1;
  }

  .swap-field {
    grid-column: 2;
    grid-row: 1 / span 2;
    align-self: center;
  }

  .date-field,
  .passenger-field,
  .time-start-field,
  .time-end-field,
  .train-number-field {
    grid-column: 1 / -1;
  }

  .btnrow {
    justify-content: stretch;
  }

  .primary-button {
    width: 100%;
  }

  .section-heading,
  .result-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .row {
    align-items: stretch;
    flex-direction: column;
  }

  .row > .field,
  .row > .hint,
  .row > .action-cluster {
    width: 100%;
  }

  .settings-main select,
  input,
  select,
  textarea,
  input.short,
  input.mid,
  input.long {
    width: 100%;
    font-size: 16px;
  }

  .radios {
    width: 100%;
  }

  .radios label {
    flex: 1;
  }

  .savebar {
    bottom: calc(8px + env(safe-area-inset-bottom));
    flex-wrap: wrap;
  }

  .savebar button {
    flex: 1;
  }

  #saveStatus {
    width: 100%;
  }
}

@media (prefers-reduced-motion: reduce) {
  html {
    scroll-behavior: auto;
  }

  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    transition-duration: .01ms !important;
    animation-duration: .01ms !important;
    animation-iteration-count: 1 !important;
  }
}
"""


@app.get("/app.css")
def app_css() -> Response:
    return Response(content=APP_CSS, media_type="text/css; charset=utf-8")


# ───────────────────────── HTTP endpoints ─────────────────────────

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    today = _date.today().isoformat()
    return f"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="description" content="원하는 KTX 시간표를 찾고 빈자리를 감시하는 binjari">
  <meta name="theme-color" content="#f5f5f7">
  <title>binjari — KTX 시간표</title>
  <link rel="stylesheet" href="/app.css">
</head>
<body class="page page-search">
<header class="site-header">
  <div class="nav-shell">
    <a class="brand" href="/" aria-label="binjari 홈">
      <span class="brand-mark" aria-hidden="true">b</span>
      <span class="brand-copy"><strong>binjari</strong><small>표가 열리는 순간까지</small></span>
    </a>
    <nav class="tabs" aria-label="주요 메뉴">
      <a class="tab active" href="/" aria-current="page">시간표</a>
      <a class="tab" href="/settings">예매 설정</a>
      <a class="tab" href="/settings?tab=env">환경 설정</a>
    </nav>
    <span id="watcherBar" aria-live="polite"></span>
  </div>
</header>
<main class="page-shell">
  <section class="hero" aria-labelledby="pageTitle">
    <p class="eyebrow">KTX SCHEDULE</p>
    <h1 id="pageTitle">원하는 시간,<br>놓치지 않게.</h1>
    <p>시간표를 한 번에 살펴보고, 마음에 드는 열차를 골라 빈자리 감시까지 이어가세요.</p>
  </section>
  <div class="layout">
    <div class="main">
      <section class="search-card" aria-labelledby="searchTitle">
        <div class="section-heading">
          <div>
            <h2 id="searchTitle">여정 찾기</h2>
            <p>출발지와 날짜를 선택하면 가능한 열차를 모아 보여드려요.</p>
          </div>
        </div>
        <div id="routeChips"></div>
        <form id="qform" onsubmit="event.preventDefault(); search('ktx')">
          <div class="form-field origin-field">
            <label for="q_origin">출발역</label>
            <input name="origin" id="q_origin" value="서울" list="stations" autocomplete="off" required>
            <div class="stn-chips" data-target="q_origin"></div>
          </div>
          <div class="swap-field">
            <button type="button" class="swap-route" onclick="swapQueryStations()"
              aria-label="출발역과 도착역 바꾸기" title="출발역과 도착역 바꾸기">⇄</button>
          </div>
          <div class="form-field destination-field">
            <label for="q_dest">도착역</label>
            <input name="dest" id="q_dest" value="부산" list="stations" autocomplete="off" required>
            <div class="stn-chips" data-target="q_dest"></div>
          </div>
          <datalist id="stations"></datalist>
          <div class="form-field date-field">
            <label for="q_date">가는 날</label>
            <input name="date" id="q_date" type="date" value="{today}" min="{today}" required>
          </div>
          <div class="form-field passenger-field">
            <label for="q_passengers">인원</label>
            <select name="passengers" id="q_passengers">
              {''.join(f'<option value="{n}"{ " selected" if n==1 else "" }>{n}명</option>' for n in range(1, 10))}
            </select>
          </div>
          <div class="form-field time-start-field">
            <label for="q_earliest">출발 시간</label>
            <select name="earliest_hour" id="q_earliest">
              {''.join(f'<option value="{h}"{ " selected" if h==6 else "" }>{h:02d}:00 이후</option>' for h in range(24))}
            </select>
          </div>
          <div class="form-field time-end-field">
            <label for="q_latest">도착 시간 범위</label>
            <select name="latest_hour" id="q_latest">
              <option value="">제한 없음</option>
              {''.join(f'<option value="{h}">{h:02d}:00 이전</option>' for h in range(1, 25))}
            </select>
          </div>
          <div class="form-field train-number-field">
            <label for="q_train_no">열차번호 <span class="optional">선택</span></label>
            <input name="train_no" id="q_train_no" inputmode="numeric" placeholder="예: 101">
          </div>
          <div class="btnrow">
            <button type="submit" class="primary-button" id="searchButton">열차 조회</button>
          </div>
        </form>
      </section>
      <div id="status" role="status" aria-live="polite"></div>
      <section id="result" aria-live="polite" aria-busy="false"></section>
    </div>
    <aside id="chatPanel" aria-label="여정 도우미"></aside>
  </div>
</main>

<script>
function swapQueryStations() {{
  const f = document.getElementById('qform');
  [f.origin.value, f.dest.value] = [f.dest.value, f.origin.value];
  f.origin.dispatchEvent(new Event('change'));
  f.dest.dispatchEvent(new Event('change'));
  f.origin.focus();
}}

function escapeHtml(value) {{
  return String(value ?? '').replace(/[&<>"']/g, ch => ({{
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
  }}[ch]));
}}

function payload() {{
  const f = document.getElementById('qform');
  return {{
    origin: f.origin.value,
    dest:   f.dest.value,
    date:   f.date.value,
    earliest_hour: parseInt(f.earliest_hour.value, 10),
    latest_hour:   f.latest_hour.value === '' ? null : parseInt(f.latest_hour.value, 10),
    train_no:      f.train_no.value.trim(),
    passengers:    parseInt(f.passengers.value, 10),
  }};
}}

async function callOne(rail, body) {{
  const r = await fetch(`/api/search/${{rail}}`, {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify(body),
  }});
  if (!r.ok) {{
    const t = await r.text();
    throw new Error(`${{rail.toUpperCase()}} ${{r.status}}: ${{t}}`);
  }}
  return r.json();
}}

// ── 열차 종류 필터: 기본 KTX(산천 포함)만, 나머지는 토글로 추가 ──
const TYPE_ORDER = ['KTX', 'SRT', 'ITX-새마을', 'ITX-마음', 'ITX-청춘', '무궁화·누리로', '기타'];
let typeFilter = (function () {{
  try {{
    const v = JSON.parse(localStorage.getItem('binjari_types') || '["KTX"]');
    return new Set(Array.isArray(v) && v.length ? v : ['KTX']);
  }} catch (e) {{ return new Set(['KTX']); }}
}})();

function trainGroup(t) {{
  t = (t || '').toUpperCase();
  if (t.startsWith('KTX')) return 'KTX';
  if (t.startsWith('SRT')) return 'SRT';
  if (t.includes('새마을')) return 'ITX-새마을';
  if (t.includes('마음')) return 'ITX-마음';
  if (t.includes('청춘')) return 'ITX-청춘';
  if (t.includes('무궁화') || t.includes('누리로')) return '무궁화·누리로';
  return '기타';
}}

function toggleType(g) {{
  if (typeFilter.has(g)) typeFilter.delete(g); else typeFilter.add(g);
  localStorage.setItem('binjari_types', JSON.stringify([...typeFilter]));
  renderResult();
}}

function renderResult() {{
  const all = window.allRows || [];
  const result = document.getElementById('result');
  if (!all.length) {{
    result.innerHTML = '<div class="empty"><strong>조건에 맞는 열차가 없어요.</strong><br><span>시간 범위를 넓히거나 날짜를 다시 확인해 주세요.</span></div>';
    return;
  }}
  const counts = {{}};
  for (const r of all) {{
    const g = trainGroup(r.train_type);
    counts[g] = (counts[g] || 0) + 1;
  }}
  const bar = TYPE_ORDER.filter(g => counts[g]).map(g => {{
    const on = typeFilter.has(g);
    return `<button type="button" class="tchip${{on ? ' on' : ''}}"
      aria-pressed="${{on}}" onclick="toggleType('${{g}}')">${{on ? '✓ ' : ''}}${{g}} (${{counts[g]}})</button>`;
  }}).join('');
  const rows = all.filter(r => typeFilter.has(trainGroup(r.train_type)));
  result.innerHTML =
    `<div class="result-head">
       <div><p class="eyebrow">AVAILABLE TRAINS</p><h2 id="resultsTitle">열차 ${{rows.length}}편</h2></div>
       <span class="result-count">전체 ${{all.length}}편</span>
     </div>
     <div class="filterbar" aria-label="열차 종류 필터">
       <span class="filter-label">열차 종류</span>${{bar}}</div>`
    + renderRows(rows)
    + (rows.length === 0 ? '<div class="empty">선택한 종류의 열차가 없어요. 다른 열차 종류를 켜보세요.</div>' : '');
  result.setAttribute('aria-labelledby', 'resultsTitle');
  updateSelBar();
}}

function renderRows(rows) {{
  if (!rows || rows.length === 0) return '';
  window.lastRows = rows;
  const head = `<tr>
    <th scope="col"><input type="checkbox" id="selAll" onchange="toggleAll(this.checked)" aria-label="검색 결과 전체 선택"></th>
    <th scope="col">열차</th><th scope="col">번호</th>
    <th scope="col">출발</th><th scope="col">도착</th><th scope="col">특실</th><th scope="col">일반실</th>
  </tr>`;
  const body = rows.map((r, i) => `<tr onclick="rowToggle(event, ${{i}})" style="cursor:pointer;">
    <td><input type="checkbox" class="selrow" data-i="${{i}}" onchange="updateSelBar()"
      aria-label="${{escapeHtml(r.train_type)}} ${{escapeHtml(r.train_no)}} ${{escapeHtml(r.depart)}} 출발 선택"></td>
    <td><span class="train-badge">${{escapeHtml(r.train_type)}}</span></td>
    <td class="train-number">${{escapeHtml(r.train_no)}}</td>
    <td class="time-cell">${{escapeHtml(r.depart)}}</td>
    <td class="time-cell">${{escapeHtml(r.arrive)}}</td>
    <td class="seat-cell">${{escapeHtml(r.first)}}</td>
    <td class="seat-cell">${{escapeHtml(r.general)}}</td>
  </tr>`).join('');
  return `<div class="table-wrap"><table aria-label="조회된 열차 시간표">
      <caption class="sr-only">조회 조건에 맞는 열차 시간표</caption>
      <thead>${{head}}</thead><tbody>${{body}}</tbody>
    </table></div>
    <div id="selBar" style="display:none;">
      <button type="button" class="pick"
        onclick="chooseSelected()">선택한 열차 <span id="selCount">0</span>대 예매 대상으로 저장</button>
      <span class="selection-note">선택한 출발 시각을 빈자리 감시 대상으로 저장합니다.</span>
    </div>`;
}}

function rowToggle(e, i) {{
  if (e.target.tagName === 'INPUT') return;   // 체크박스 직접 클릭은 그대로
  const box = document.querySelector(`.selrow[data-i="${{i}}"]`);
  box.checked = !box.checked;
  updateSelBar();
}}

function toggleAll(on) {{
  document.querySelectorAll('.selrow').forEach(c => {{ c.checked = on; }});
  updateSelBar();
}}

function selectedRows() {{
  return [...document.querySelectorAll('.selrow:checked')]
    .map(c => window.lastRows[parseInt(c.dataset.i, 10)]);
}}

function updateSelBar() {{
  const n = selectedRows().length;
  const bar = document.getElementById('selBar');
  if (!bar) return;
  bar.style.display = n > 0 ? 'flex' : 'none';
  document.getElementById('selCount').textContent = n;
  document.querySelectorAll('.selrow').forEach(c => {{
    c.closest('tr')?.classList.toggle('is-selected', c.checked);
  }});
  const all = document.querySelectorAll('.selrow');
  const allBox = document.getElementById('selAll');
  if (allBox) {{
    allBox.checked = all.length > 0 && n === all.length;
    allBox.indeterminate = n > 0 && n < all.length;
  }}
}}

async function saveTrains(rows) {{
  const f = document.getElementById('qform');
  const times = [...new Set(rows.map(r => r.depart).filter(Boolean))].sort();
  const nos = [...new Set(rows.map(r => r.train_no).filter(Boolean))];
  const label = rows.map(r => `${{r.train_type}} ${{r.train_no}} ${{r.depart}}발`).join('\\n');
  if (!confirm(`아래 ${{rows.length}}대를 예매 대상으로 저장할까요?\\n${{label}}\\n(.env.ktx 의 여정·감시 시각이 이 열차들로 바뀝니다)`)) return;
  const status = document.getElementById('status');
  try {{
    const resp = await fetch('/api/settings', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ values: {{
        KTXA_ORIGIN: f.origin.value,
        KTXA_DEST: f.dest.value,
        KTXA_DATE: f.date.value,
        KTXA_TIMES: times.join(','),
        KTXA_TRAIN_NO: nos.join(','),
        KTXA_PASSENGERS: f.passengers.value,
      }} }}),
    }});
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.status);
    if (window.recordTrip) window.recordTrip(f.origin.value, f.dest.value);
    status.innerHTML = `✅ ${{rows.length}}대(${{times.join(', ')}}) 예매 대상으로 저장됨 → <a href="/settings">⚙ 예매 설정에서 좌석·실행 범위 확인</a>`;
  }} catch (e) {{
    status.textContent = `예매 대상 저장 실패: ${{e.message}}`;
  }}
}}

function chooseSelected() {{
  const rows = selectedRows();
  if (rows.length) saveTrains(rows);
}}

let activeSearch = 0;
async function search(which, force = false) {{
  const status = document.getElementById('status');
  const result = document.getElementById('result');
  const button = document.getElementById('searchButton');
  const body = payload();
  if (!body.origin || !body.dest || !body.date) {{
    status.textContent = '출발역, 도착역, 날짜를 모두 입력해 주세요.';
    document.getElementById(!body.origin ? 'q_origin' : !body.dest ? 'q_dest' : 'q_date').focus();
    return;
  }}
  if (body.origin.trim() === body.dest.trim()) {{
    status.textContent = '출발역과 도착역은 서로 달라야 해요.';
    document.getElementById('q_dest').focus();
    return;
  }}
  body.force = force;
  const requestId = ++activeSearch;
  if (window.recordTrip) window.recordTrip(body.origin, body.dest);
  localStorage.setItem('binjari_query', JSON.stringify({{
    origin: body.origin, dest: body.dest, date: body.date,
    earliest_hour: body.earliest_hour, latest_hour: body.latest_hour, ts: Date.now(),
  }}));
  status.textContent = force
    ? '최신 시간표를 확인하고 있어요. 약 15초 정도 걸릴 수 있습니다.'
    : '시간표를 찾고 있어요. 저장된 정보가 있으면 바로 보여드릴게요.';
  result.setAttribute('aria-busy', 'true');
  result.classList.add('is-loading');
  if (button) {{
    button.disabled = true;
    button.textContent = '조회 중…';
  }}
  const t0 = performance.now();
  try {{
    let data = null;
    if (which === 'ktx') {{
      data = await callOne('ktx', body);
      if (requestId !== activeSearch) return;
      window.allRows = data.rows || [];
      renderResult();
    }}
    const sec = ((performance.now()-t0)/1000).toFixed(1);
    const src = data && data.cached
      ? `저장된 시간표 · ${{escapeHtml(data.fetched_at)}} 수집
         <button type="button" class="pick" onclick="search('ktx', true)">최신 정보로 다시 조회</button>`
      : `코레일 최신 시간표 · ${{escapeHtml(data ? data.fetched_at : '')}} 수집`;
    status.innerHTML = `${{sec}}초 만에 찾았어요 · ${{src}}`;
  }} catch (e) {{
    if (requestId === activeSearch) status.textContent = `조회하지 못했어요. ${{e.message}}`;
  }} finally {{
    if (requestId === activeSearch) {{
      result.setAttribute('aria-busy', 'false');
      result.classList.remove('is-loading');
      if (button) {{
        button.disabled = false;
        button.textContent = '열차 조회';
      }}
    }}
  }}
}}

// ── 시간대 끝 옵션: 시작 시각 이후만 표시 ──
function syncLatestOptions() {{
  const f = document.getElementById('qform');
  const start = parseInt(f.earliest_hour.value, 10);
  const cur = f.latest_hour.value;
  const sel = f.latest_hour;
  sel.innerHTML = '';
  sel.add(new Option('제한 없음', ''));
  for (let h = start + 1; h <= 24; h++) {{
    sel.add(new Option(String(h).padStart(2, '0') + ':00', h));
  }}
  sel.value = (cur !== '' && parseInt(cur, 10) > start) ? cur : '';
}}
document.getElementById('qform').earliest_hour.addEventListener('change', syncLatestOptions);
syncLatestOptions();

// ── 예매 설정과 조회 폼 동기화 ──
// 마지막 조회 조건(localStorage)이 예매 설정 저장보다 최신이면 그걸 복원,
// 아니면 .env.ktx(예매 설정) 여정으로 초기화.
async function initFormFromSettings() {{
  try {{
    const f = document.getElementById('qform');
    let q = null;
    try {{ q = JSON.parse(localStorage.getItem('binjari_query') || 'null'); }} catch (e) {{}}
    const savedTs = parseInt(localStorage.getItem('binjari_saved_ts') || '0', 10);
    if (q && q.ts > savedTs) {{
      if (q.origin) f.origin.value = q.origin;
      if (q.dest) f.dest.value = q.dest;
      if (q.date && q.date >= f.date.value) f.date.value = q.date;
      if (q.earliest_hour !== undefined) {{
        f.earliest_hour.value = String(q.earliest_hour);
        syncLatestOptions();
      }}
      f.latest_hour.value = (q.latest_hour === null || q.latest_hour === undefined) ? '' : String(q.latest_hour);
      return;
    }}
    const r = await fetch('/api/settings');
    const v = (await r.json()).values || {{}};
    const clean = s => (s && !/^<.*>$/.test(s)) ? s : '';
    if (clean(v.KTXA_ORIGIN)) f.origin.value = v.KTXA_ORIGIN;
    if (clean(v.KTXA_DEST)) f.dest.value = v.KTXA_DEST;
    if (v.KTXA_DATE && v.KTXA_DATE >= f.date.value) f.date.value = v.KTXA_DATE;
    const tw = (v.KTXA_TIME_WINDOW || '').split(',');
    const startStr = (tw[0] || (v.KTXA_TIMES || '').split(',')[0] || '').trim();
    const m = /^(\\d{{2}}):/.exec(startStr);
    if (m) {{
      f.earliest_hour.value = String(parseInt(m[1], 10));
      syncLatestOptions();
    }}
    const m2 = /^(\\d{{2}}):(\\d{{2}})$/.exec((tw[1] || '').trim());
    if (m2) {{
      const endH = Math.min(parseInt(m2[1], 10) + (parseInt(m2[2], 10) > 0 ? 1 : 0), 24);
      if (endH > parseInt(f.earliest_hour.value, 10)) f.latest_hour.value = String(endH);
    }}
  }} catch (e) {{ /* 초기값 유지 */ }}
}}
initFormFromSettings();

// ── 채팅 패널 연동 ──
window.CHAT_TAB = 'search';
window.CHAT_GREETING = '조회 조건을 말로 입력해 보세요.\\n예: "내일 아침 8시 이후 서울에서 부산 2명 조회해줘"';
window.applyChatUpdates = function (u, action) {{
  const f = document.getElementById('qform');
  if (u.origin) f.origin.value = u.origin;
  if (u.dest) f.dest.value = u.dest;
  if (u.date) f.date.value = u.date;
  if (u.earliest_hour !== undefined && u.earliest_hour !== '') {{
    f.earliest_hour.value = String(parseInt(u.earliest_hour, 10));
    syncLatestOptions();
  }}
  if (u.latest_hour !== undefined) f.latest_hour.value = u.latest_hour === '' ? '' : String(parseInt(u.latest_hour, 10));
  if (u.train_no !== undefined) f.train_no.value = u.train_no;
  if (u.passengers !== undefined && u.passengers !== '') f.passengers.value = String(parseInt(u.passengers, 10));
  if (action === 'search') search('ktx');
}};

// 자주 쓰는 구간 칩 클릭 → 폼 반영
window.applyRoute = function (o, d) {{
  const f = document.getElementById('qform');
  f.origin.value = o;
  f.dest.value = d;
}};
</script>
<script src="/chat.js"></script>
<script src="/common.js"></script>
</body>
</html>
"""


SETTINGS_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="description" content="binjari KTX 예매와 빈자리 감시 설정">
  <meta name="theme-color" content="#f5f5f7">
  <title>binjari — 예매 설정</title>
  <link rel="stylesheet" href="/app.css">
</head>
<body class="page page-settings">
<header class="site-header">
  <div class="nav-shell">
    <a class="brand" href="/" aria-label="binjari 홈">
      <span class="brand-mark" aria-hidden="true">b</span>
      <span class="brand-copy"><strong>binjari</strong><small>표가 열리는 순간까지</small></span>
    </a>
    <nav class="site-nav" aria-label="주요 메뉴">
      <a href="/" class="navtab">시간표</a>
      <a href="/settings" class="navtab" id="nav-trip">예매 설정</a>
      <a href="/settings?tab=env" class="navtab" id="nav-env">환경 설정</a>
    </nav>
    <span id="watcherBar" aria-live="polite"></span>
  </div>
</header>
<main class="page-shell settings-main">
<div class="cols">
  <section class="settings-hero" aria-labelledby="settingsTitle">
    <p class="eyebrow" id="settingsEyebrow">BOOKING SETUP</p>
    <h1 id="settingsTitle">예매 설정</h1>
    <p id="settingsSubtitle">여정과 좌석을 정하고, 원하는 열차가 열릴 때까지 감시하세요.</p>
  </section>

  <!-- ═══════ 여정 (코레일 홈 스타일) ═══════ -->
  <section class="card sec-trip card-journey">
    <h2>여정</h2>
    <div id="routeChips"></div>
    <div class="row">
      <div class="field"><label>출발역</label>
        <input id="KTXA_ORIGIN" class="mid" list="stations" placeholder="서울">
        <div class="stn-chips" data-target="KTXA_ORIGIN"></div></div>
      <button type="button" class="swap" onclick="swapStations()" title="출발↔도착 바꾸기">⇄</button>
      <div class="field"><label>도착역</label>
        <input id="KTXA_DEST" class="mid" list="stations" placeholder="부산">
        <div class="stn-chips" data-target="KTXA_DEST"></div></div>
      <div class="field"><label>가는 날</label>
        <input id="KTXA_DATE" type="date"></div>
    </div>
    <!-- ─ 시간표 조회 (출발 시각 + N시간, KTX·SRT 기본) ─ -->
    <div class="row journey-divider">
      <div class="field"><label>출발 시각</label>
        <div style="display:flex; gap:6px; align-items:center;">
          <select id="addHour"></select>
          <select id="addMin"><option>00</option><option>10</option><option>20</option>
            <option>30</option><option>40</option><option>50</option></select>
        </div>
      </div>
      <div class="field"><label>＋ 조회 시간</label>
        <select id="TW_HOURS"></select></div>
      <div class="field"><label>열차 종류</label>
        <div class="action-cluster">
          <label class="check" style="padding:0"><input type="checkbox" id="LOAD_KTX" checked>KTX(산천·청룡)</label>
          <label class="check" style="padding:0"><input type="checkbox" id="LOAD_SRT" checked>SRT</label>
          <label class="check" style="padding:0"><input type="checkbox" id="LOAD_ETC">기타(ITX·무궁화)</label>
        </div>
      </div>
      <div class="field"><label>&nbsp;</label>
        <div class="action-cluster">
          <button type="button" class="swap primary-inline" id="btnLoadTrains"
            onclick="loadRangeTrains(false)"
            title="저장본이 있으면 즉시, 없으면 자동으로 사이트에서 조회합니다">시간표 불러오기</button>
          <button type="button" class="swap" id="btnRefreshTrains" onclick="loadRangeTrains(true)"
            title="저장본을 사용하지 않고 코레일에서 다시 조회합니다">최신 정보 조회</button>
        </div>
      </div>
    </div>

    <!-- ─ 예매할 열차 (감시 대상) ─ -->
    <div class="field" style="margin-top:8px;">
      <label>예매할 열차 (클릭하면 제외)</label>
      <div id="timeChips"></div>
    </div>
    <p class="hint" id="rangeHint"></p>

    <!-- ─ 수동 추가 · 허용 오차 (감시 대상과 분리) ─ -->
    <details>
      <summary>수동 시각 추가 · 허용 오차</summary>
      <div class="row" style="margin-top:10px;">
        <div class="field"><label>시각 직접 추가 (시간표에 없어도 됨)</label>
          <div style="display:flex; gap:6px; align-items:center;">
            <select id="manHour"></select>
            <select id="manMin"><option>00</option><option>10</option><option>20</option>
              <option>30</option><option>40</option><option>50</option></select>
            <button type="button" class="swap" onclick="addTime()">＋ 추가</button>
          </div>
        </div>
        <div class="field"><label>허용 오차 (분)</label>
          <input id="KTXA_TOLERANCE_MIN" type="number" class="short" min="0"
                 onchange="renderTimes()"></div>
        <span class="hint">허용 오차: 각 감시 시각 ±N분 안에 출발하는 열차까지 후보로 봅니다.</span>
      </div>
    </details>
    <datalist id="stations"></datalist>  <!-- /common.js 가 실제 코레일 역 목록으로 채움 -->
  </section>

  <!-- ═══════ 승객 (기본 어른 1명, 접힘) ═══════ -->
  <section class="card sec-trip card-passengers">
    <details id="paxWrap" style="border:0; margin:0; padding:0;">
      <summary style="cursor:pointer; list-style-position:outside;">
        <span class="summary-title">승객</span>
        <span id="paxSummary" style="margin-left:10px; font-size:13px; color:#444;">어른 1명</span>
        <span class="hint" style="margin-left:8px;">펼쳐서 변경</span>
      </summary>
      <div class="pax" id="paxGrid" style="margin-top:14px;"></div>
      <p class="hint">코레일 인원선택 팝업과 동일한 7가지 유형. 합계가 KTXA_PASSENGERS / KTXA_PASSENGER_TYPE 으로 저장됩니다.</p>
    </details>
  </section>

  <!-- ═══════ 좌석 / 열차 ═══════ -->
  <section class="card sec-trip card-seat">
    <h2>좌석 · 열차</h2>
    <div class="row">
      <div class="field"><label>좌석 등급</label>
        <div class="radios" id="seatRadios">
          <label><input type="radio" name="seat" value="일반실"><span>일반실</span></label>
          <label><input type="radio" name="seat" value="특실"><span>특실</span></label>
          <label><input type="radio" name="seat" value=""><span>무관</span></label>
        </div>
      </div>
      <div class="field"><label>열차 종류 (KTXA_TRAIN_TYPE)</label>
        <input id="KTXA_TRAIN_TYPE" class="mid" list="trainTypes">
        <datalist id="trainTypes">
          <option>KTX</option><option>전체</option><option>ITX-새마을</option>
          <option>ITX-청춘</option><option>무궁화</option>
        </datalist></div>
      <div class="field"><label>열차번호 지정 (선택)</label>
        <input id="KTXA_TRAIN_NO" class="short" placeholder="예: 101"></div>
    </div>
    <div class="row">
      <label class="check"><input type="checkbox" id="KTXA_SEATED_ONLY">
        좌석으로 한 번에 가는 열차만 (입석+좌석 / 예약대기 제외)</label>
      <label class="check"><input type="checkbox" id="KTXA_INCLUDE_SRT">
        에스알티(SRT) 함께 보기</label>
    </div>
  </section>

  <!-- ═══════ 실행 범위 ═══════ -->
  <section class="card sec-trip card-mode">
    <h2>실행 범위 — 어디까지 자동으로 할까요</h2>
    <div class="radios" id="modeRadios">
      <label><input type="radio" name="runmode" value="search"><span>조회만</span></label>
      <label><input type="radio" name="runmode" value="reserve"><span>예약까지</span></label>
      <label><input type="radio" name="runmode" value="pay"><span>결제·발권까지</span></label>
    </div>
    <div class="row" style="margin-top:12px;">
      <div class="field"><label>확보 목표 (예매할 열차 여러 대 중 몇 건 잡으면 종료)</label>
        <select id="KTXA_RESERVE_LIMIT">
          <option value="1">1건만 — 아무거나 하나 잡히면 종료 (기본)</option>
          <option value="2">2건</option>
          <option value="3">3건</option>
          <option value="4">4건</option>
          <option value="0">선택한 열차 전부</option>
        </select></div>
    </div>
    <p class="hint">예약까지: 취소표가 나오면 예약만 잡아둡니다 (결제는 직접).
      결제·발권까지: 예약 후 등록된 카드로 자동 결제·발권합니다.
      로그인·카드 정보는 ⚙ 환경 설정 탭에서 입력하세요.</p>
  </section>

  <!-- ═══════ 환경설정: 로그인 ═══════ -->
  <section class="card sec-env card-login">
    <h2>코레일 로그인</h2>
    <div class="row">
      <div class="field"><label>회원번호 (KTXA_USER)</label>
        <input id="KTXA_USER" class="mid" autocomplete="off"></div>
      <div class="field"><label>비밀번호 (KTXA_PASS)</label>
        <span class="pwwrap"><input id="KTXA_PASS" type="password" class="mid" autocomplete="off">
        <button type="button" onclick="togglePw('KTXA_PASS')">👁</button></span></div>
    </div>
  </section>

  <!-- ═══════ 환경설정: 카드 ═══════ -->
  <section class="card sec-env card-payment">
    <h2>결제 카드</h2>
    <div class="row">
      <div class="field"><label>카드번호 (하이픈 4등분)</label>
        <input id="PAY_CARD_NUM" class="long" placeholder="9999-9999-9999-9999"
               maxlength="19" oninput="fmtCard(this)"></div>
      <div class="field"><label>유효기간 월</label>
        <select id="PAY_CARD_MM"></select></div>
      <div class="field"><label>유효기간 년</label>
        <select id="PAY_CARD_YY"></select></div>
    </div>
    <div class="row">
      <div class="field"><label>카드 비밀번호 앞 2자리</label>
        <span class="pwwrap"><input id="PAY_CARD_PW2" type="password" class="short" maxlength="2">
        <button type="button" onclick="togglePw('PAY_CARD_PW2')">👁</button></span></div>
      <div class="field"><label>인증번호 (주민번호 앞 6자리)</label>
        <span class="pwwrap"><input id="PAY_ID6" type="password" class="short" maxlength="6">
        <button type="button" onclick="togglePw('PAY_ID6')">👁</button></span></div>
      <div class="field"><label>카드사</label>
        <input id="PAY_CARD_COMPANY" class="mid" list="cardCompanies">
        <datalist id="cardCompanies">
          <option>KB국민</option><option>신한</option><option>삼성</option><option>현대</option>
          <option>롯데</option><option>하나</option><option>우리</option><option>NH농협</option>
          <option>BC</option><option>IBK기업</option><option>씨티</option><option>카카오뱅크</option>
        </datalist></div>
    </div>
  </section>

  <!-- ═══════ 환경설정: 전달 / 워처 ═══════ -->
  <section class="card sec-env card-automation">
    <h2>승차권 전달 · 워처 동작</h2>
    <div class="row">
      <label class="check"><input type="checkbox" id="KTXA_TRANSFER_ENABLED">발권 후 승차권 자동 전달</label>
      <label class="check"><input type="checkbox" id="KTXA_TRANSFER_SEND">실제 전송 (끄면 dry-run)</label>
    </div>
    <div class="row">
      <div class="field"><label>수신자 회원번호</label><input id="KTXA_TRANSFER_MEMBER_NO" class="mid"></div>
      <div class="field"><label>수신자 이름</label><input id="KTXA_TRANSFER_NAME" class="mid"></div>
      <div class="field"><label>수신자 휴대폰</label><input id="KTXA_TRANSFER_PHONE" class="mid" placeholder="01000000000"></div>
    </div>
    <div class="row journey-divider">
      <span class="hint">실행 범위(조회만/예약까지/결제까지)는 예매 설정 탭에서 선택합니다.</span>
      <label class="check"><input type="checkbox" id="KTXA_HUMANIZE">인간화 딜레이 (매크로 감지 회피)</label>
      <label class="check"><input type="checkbox" id="KTXA_ONCE">1회만 실행</label>
    </div>
    <div class="row">
      <div class="field"><label>폴링 최소 (초)</label><input id="KTXA_POLL_MIN" type="number" class="short" min="1"></div>
      <div class="field"><label>폴링 최대 (초)</label><input id="KTXA_POLL_MAX" type="number" class="short" min="1"></div>
      <div class="field"><label>로그 폴더</label><input id="KTXA_LOG_DIR" class="mid"></div>
      <div class="field"><label>로그 레벨</label>
        <select id="KTXA_LOG_LEVEL"><option>DEBUG</option><option>INFO</option>
          <option>WARNING</option><option>ERROR</option></select></div>
    </div>
    <details>
      <summary>브라우저 (CDP) 고급 설정</summary>
      <div class="row" style="margin-top:10px;">
        <div class="field"><label>CDP 포트</label><input id="KTXA_CDP_PORT" type="number" class="short"></div>
        <div class="field"><label>Chrome 프로필 폴더</label><input id="KTXA_CDP_USER_DATA_DIR" class="long"></div>
        <div class="field"><label>기동 타임아웃 (초)</label><input id="KTXA_CDP_STARTUP_TIMEOUT" type="number" class="short"></div>
      </div>
      <div class="row">
        <label class="check" title="조회·예약용 Chrome 창을 'binjari' 가상 데스크톱으로 자동 이동 — 작업 화면을 가리지 않음">
          <input type="checkbox" id="KTXA_VDESK">브라우저를 별도 가상 데스크톱에서 실행</label>
      </div>
    </details>
  </section>

  <!-- ═══════ 환경설정: 알림 ═══════ -->
  <section class="card sec-env card-notifications">
    <h2>Teams 알림</h2>
    <div class="row">
      <label class="check"><input type="checkbox" id="TEAMS_ENABLED">Teams 알림 사용</label>
      <div class="field"><label>보내는 계정 이메일</label><input id="TEAMS_USER_EMAIL" class="long"></div>
    </div>
    <div class="row">
      <div class="field"><label>채팅 ID (비우면 48:notes = 나에게)</label><input id="TEAMS_CHAT_ID" class="long"></div>
      <div class="field"><label>수신자 이름 검색 (선택)</label><input id="TEAMS_RECIPIENT_NAME" class="mid"></div>
      <div class="field"><label>메시지 prefix</label><input id="TEAMS_PREFIX" class="mid"></div>
    </div>
    <details>
      <summary>Azure AD OAuth (Graph API)</summary>
      <div class="row" style="margin-top:10px;">
        <div class="field"><label>Client ID</label><input id="AZURE_CLIENT_ID" class="long" autocomplete="off"></div>
        <div class="field"><label>Client Secret</label>
          <span class="pwwrap"><input id="AZURE_CLIENT_SECRET" type="password" class="long" autocomplete="off">
          <button type="button" onclick="togglePw('AZURE_CLIENT_SECRET')">👁</button></span></div>
      </div>
      <div class="row">
        <div class="field"><label>Tenant ID</label><input id="AZURE_TENANT_ID" class="long"></div>
        <div class="field"><label>Redirect URI</label><input id="AZURE_REDIRECT_URI" class="long"></div>
      </div>
      <div class="row">
        <div class="field"><label>Authority</label><input id="AZURE_AUTHORITY" class="long"></div>
        <div class="field"><label>Scopes</label><input id="AZURE_SCOPES" class="long"></div>
        <div class="field"><label>auth.db 경로</label><input id="DB_PATH" class="long"></div>
      </div>
    </details>
  </section>

  <details id="wlogWrap" style="border:0; margin:0 0 8px; padding:0;">
    <summary style="font-size:12px; color:#888;">감시 로그 보기</summary>
    <pre id="wlog" style="background:#1e2530; color:#cfe0d8; font-size:11px; padding:10px;
      border-radius:8px; max-height:220px; overflow:auto; white-space:pre-wrap;"></pre>
  </details>
  <div class="savebar">
    <button type="button" id="btnSave" onclick="save()">저장</button>
    <span id="saveStatus" role="status" aria-live="polite"></span>
  </div>
</div>
<aside id="chatPanel" aria-label="설정 도우미"></aside>
</main>

<script>
const PAX_TYPES = ['어른','어린이','유아','경로','중증장애인','경증장애인','국가유공자'];
const TEXT_IDS = [
  'KTXA_ORIGIN','KTXA_DEST','KTXA_DATE','KTXA_TOLERANCE_MIN','KTXA_RESERVE_LIMIT',
  'KTXA_TRAIN_TYPE','KTXA_TRAIN_NO',
  'KTXA_USER','KTXA_PASS',
  'PAY_CARD_NUM','PAY_CARD_MM','PAY_CARD_YY','PAY_CARD_PW2','PAY_ID6','PAY_CARD_COMPANY',
  'KTXA_TRANSFER_MEMBER_NO','KTXA_TRANSFER_NAME','KTXA_TRANSFER_PHONE',
  'KTXA_POLL_MIN','KTXA_POLL_MAX','KTXA_LOG_DIR','KTXA_LOG_LEVEL',
  'KTXA_CDP_PORT','KTXA_CDP_USER_DATA_DIR','KTXA_CDP_STARTUP_TIMEOUT',
  'TEAMS_USER_EMAIL','TEAMS_CHAT_ID','TEAMS_RECIPIENT_NAME','TEAMS_PREFIX',
  'AZURE_CLIENT_ID','AZURE_CLIENT_SECRET','AZURE_TENANT_ID','AZURE_REDIRECT_URI',
  'AZURE_AUTHORITY','AZURE_SCOPES','DB_PATH',
];
const BOOL_IDS = [
  'KTXA_SEATED_ONLY','KTXA_INCLUDE_SRT','KTXA_TRANSFER_ENABLED','KTXA_TRANSFER_SEND',
  'KTXA_HUMANIZE','KTXA_ONCE','TEAMS_ENABLED','KTXA_VDESK',
];
let times = [];   // ['09:00', ...]

// ── 초기 위젯 구성 ──
(function initWidgets() {
  for (const id of ['addHour', 'manHour']) {
    const sel = document.getElementById(id);
    for (let h = 0; h < 24; h++) {
      sel.add(new Option(String(h).padStart(2,'0') + '시', String(h).padStart(2,'0')));
    }
    sel.value = '09';
  }
  const tw = document.getElementById('TW_HOURS');
  tw.add(new Option('제한 없음', ''));
  for (let n = 1; n <= 12; n++) tw.add(new Option('+' + n + '시간', n));
  const mm = document.getElementById('PAY_CARD_MM');
  for (let m = 1; m <= 12; m++) mm.add(new Option(String(m).padStart(2,'0')));
  const yy = document.getElementById('PAY_CARD_YY');
  const thisYear = new Date().getFullYear();
  for (let y = thisYear - 1; y <= thisYear + 15; y++) yy.add(new Option(y));
  const grid = document.getElementById('paxGrid');
  for (const t of PAX_TYPES) {
    const d = document.createElement('div');
    d.className = 'field';
    d.innerHTML = `<label>${t}</label><select id="pax_${t}"></select>`;
    grid.appendChild(d);
    const sel = d.querySelector('select');
    for (let n = 0; n <= 9; n++) sel.add(new Option(n + '명', n));
  }
  document.getElementById('pax_어른').value = 1;   // 기본 어른 1명
  grid.addEventListener('change', updatePaxSummary);
  document.querySelectorAll('.field').forEach(field => {
    const label = field.querySelector(':scope > label');
    const control = field.querySelector(':scope > input[id], :scope > select[id], :scope > .pwwrap input[id]');
    if (label && control) label.htmlFor = control.id;
  });
  document.querySelectorAll('.pwwrap button').forEach(button => {
    const input = button.closest('.pwwrap').querySelector('input');
    button.setAttribute('aria-label', input.id + ' 값 보기');
    button.setAttribute('aria-pressed', 'false');
  });
})();

function swapStations() {
  const a = document.getElementById('KTXA_ORIGIN'), b = document.getElementById('KTXA_DEST');
  [a.value, b.value] = [b.value, a.value];
  a.dispatchEvent(new Event('change'));
  b.dispatchEvent(new Event('change'));
  formDirty = true;
}
function togglePw(id) {
  const el = document.getElementById(id);
  el.type = el.type === 'password' ? 'text' : 'password';
  const button = el.closest('.pwwrap').querySelector('button');
  const visible = el.type === 'text';
  button.setAttribute('aria-pressed', String(visible));
  button.setAttribute('aria-label', id + (visible ? ' 값 숨기기' : ' 값 보기'));
}
function fmtCard(el) {
  const d = el.value.replace(/\\D/g, '').slice(0, 16);
  el.value = d.replace(/(.{4})/g, '$1-').replace(/-$/, '');
}
// 감시 시각 칩 주석용 시간표 (캐시에서 로드, depart → row)
let timeInfo = {};

async function annotateTimes() {
  const o = document.getElementById('KTXA_ORIGIN').value.trim();
  const d = document.getElementById('KTXA_DEST').value.trim();
  const dt = document.getElementById('KTXA_DATE').value;
  timeInfo = {};
  if (o && d && dt) {
    try {
      const r = await fetch(`/api/timetable?origin=${encodeURIComponent(o)}&dest=${encodeURIComponent(d)}&date=${dt}`);
      for (const row of (await r.json()).rows || []) {
        if (row.depart) timeInfo[row.depart] = row;
      }
    } catch (e) { /* 캐시 없으면 시각만 표시 */ }
  }
  renderTimes();
}

function chipLabel(t) {
  const r = timeInfo[t];
  if (!r || !r.arrive) return t;
  const [dh, dm] = t.split(':').map(Number);
  const [ah, am] = r.arrive.split(':').map(Number);
  let mins = (ah * 60 + am) - (dh * 60 + dm);
  if (mins < 0) mins += 24 * 60;
  const dur = Math.floor(mins / 60) + '시간' + (mins % 60 ? String(mins % 60) + '분' : '');
  const name = [r.train_type, r.train_no].filter(Boolean).join(' ');
  return `${t}${name ? ' · ' + name : ''} · ${dur}`;
}

function htmlText(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
  }[ch]));
}

function renderTimes() {
  const box = document.getElementById('timeChips');
  box.innerHTML = times.length
    ? times.map(t => `<button type="button" class="chip" onclick="delTime('${t}')"
        aria-label="${htmlText(chipLabel(t))} 감시 대상에서 제외">
        ${htmlText(chipLabel(t))}<span class="chip-remove" aria-hidden="true">×</span></button>`).join('')
    : '<span class="hint">아직 선택한 열차가 없어요. 시간표를 불러오거나 시간표 탭에서 열차를 선택하세요.</span>';
  // 남은 칩의 열차번호를 좌석·열차 섹션(KTXA_TRAIN_NO)에 바로 반영
  const nos = times.map(t => (timeInfo[t] || {}).train_no).filter(Boolean);
  if (nos.length) document.getElementById('KTXA_TRAIN_NO').value = nos.join(',');
  const hint = document.getElementById('rangeHint');
  if (hint) {
    const tol = parseInt(document.getElementById('KTXA_TOLERANCE_MIN').value, 10) || 0;
    hint.textContent = times.length
      ? `각 시각 ±${tol}분 안에 출발하는 열차를 감시합니다` +
        (times.length > 1 ? ` (${times.length}개 시각 각각)` : '') +
        `. 감시 범위 제한을 걸면 첫 시각부터 +N시간 밖의 열차는 제외됩니다.`
      : '';
  }
}
function addTime() {
  const t = document.getElementById('manHour').value + ':' + document.getElementById('manMin').value;
  if (!times.includes(t)) { times.push(t); times.sort(); renderTimes(); formDirty = true; }
}
function delTime(t) { times = times.filter(x => x !== t); renderTimes(); formDirty = true; }

// ── 날짜·출발시각·조회시간 범위의 열차를 불러와 칩으로 나열 (force=사이트 재조회) ──
function loadGroupOk(t) {
  t = (t || '').toUpperCase();
  if (t.startsWith('KTX')) return document.getElementById('LOAD_KTX').checked;
  if (t.startsWith('SRT')) return document.getElementById('LOAD_SRT').checked;
  return document.getElementById('LOAD_ETC').checked;
}

async function loadRangeTrains(force) {
  const o = document.getElementById('KTXA_ORIGIN').value.trim();
  const d = document.getElementById('KTXA_DEST').value.trim();
  const dt = document.getElementById('KTXA_DATE').value;
  if (!o || !d || !dt) { setStatus('err', '출발역·도착역·가는 날을 먼저 입력하세요.'); return; }
  if (o === d) { setStatus('err', '출발역과 도착역은 서로 달라야 해요.'); return; }
  const loadButtons = [
    document.getElementById('btnLoadTrains'),
    document.getElementById('btnRefreshTrains'),
  ];
  if (loadButtons.some(button => button.disabled)) return;
  loadButtons.forEach(button => {
    button.dataset.label = button.textContent;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
  });
  (force ? loadButtons[1] : loadButtons[0]).textContent = '조회 중…';
  const sh = parseInt(document.getElementById('addHour').value, 10);
  const sm = parseInt(document.getElementById('addMin').value, 10);
  const twN = document.getElementById('TW_HOURS').value;
  const startMin = sh * 60 + sm;
  const endMin = twN === '' ? 24 * 60 - 1 : Math.min(startMin + parseInt(twN, 10) * 60, 24 * 60 - 1);
  setStatus('', force ? '사이트 재조회 중... (~30초)' : '열차 조회 중... (저장된 시간표 있으면 즉시, 없으면 ~30초)');
  try {
    const r = await fetch('/api/search/ktx', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        origin: o, dest: d, date: dt,
        earliest_hour: sh,
        latest_hour: twN === '' ? null : Math.min(Math.ceil(endMin / 60), 24),
        passengers: 1,
        force: !!force,
        include_srt: document.getElementById('LOAD_SRT').checked,
      }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.status);
    const rows = (data.rows || []).filter(row => {
      const m = /^(\\d{2}):(\\d{2})$/.exec(row.depart || '');
      if (!m) return false;
      const mins = parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
      return mins >= startMin && mins <= endMin && loadGroupOk(row.train_type);
    });
    timeInfo = {};
    for (const row of rows) timeInfo[row.depart] = row;
    times = [...new Set(rows.map(row => row.depart))].sort();
    renderTimes();
    formDirty = true;
    if (times.length) {
      setStatus('ok', `${times.length}대 불러옴 (${data.cached ? '저장된 시간표 ' + data.fetched_at + ' 수집' : '사이트 조회'}) — 필요 없는 열차 칩을 클릭해 지우고 저장하세요.`);
    } else {
      setStatus('warn', '해당 범위에 열차가 없습니다. 출발 시각·조회 시간·열차 종류를 조정하거나 🔄 사이트 최신 조회를 눌러 보세요.');
    }
  } catch (e) {
    setStatus('err', '열차 불러오기 실패: ' + e.message);
  } finally {
    loadButtons.forEach(button => {
      button.disabled = false;
      button.removeAttribute('aria-busy');
      button.textContent = button.dataset.label;
    });
  }
}
function setRunMode(mode, pay) {
  const v = mode === 'reserve' ? (pay ? 'pay' : 'reserve') : 'search';
  document.querySelector(`#modeRadios input[value="${v}"]`).checked = true;
}
function setSeat(seat) {
  const radio = document.querySelector(`#seatRadios input[value="${seat}"]`)
             || document.querySelector('#seatRadios input[value=""]');
  radio.checked = true;
}
function updatePaxSummary() {
  const parts = PAX_TYPES
    .map(t => [t, parseInt(document.getElementById('pax_' + t).value, 10) || 0])
    .filter(([, n]) => n > 0)
    .map(([t, n]) => `${t} ${n}명`);
  document.getElementById('paxSummary').textContent = parts.length ? parts.join(', ') : '0명 — 선택 필요';
}

function setPax(ptype, total) {
  for (const t of PAX_TYPES) document.getElementById('pax_' + t).value = 0;
  if (ptype.includes(':')) {
    for (const part of ptype.split(',')) {
      const [name, cnt] = part.split(':');
      const el = document.getElementById('pax_' + name.trim());
      if (el) el.value = Math.min(9, parseInt(cnt, 10) || 0);
    }
  } else {
    const el = document.getElementById('pax_' + ptype) || document.getElementById('pax_어른');
    el.value = Math.min(9, total);
  }
  updatePaxSummary();
}

// ── 로드: /api/settings → 폼 ──
async function load() {
  const r = await fetch('/api/settings');
  const data = await r.json();
  const v = data.values;
  const clean = s => (s && !/^<.*>$/.test(s)) ? s : '';   // <자리표시자> 는 빈칸 취급
  for (const id of TEXT_IDS) {
    const el = document.getElementById(id);
    if (el) el.value = clean(v[id] || '');
  }
  for (const id of BOOL_IDS) {
    const el = document.getElementById(id);
    if (el) el.checked = /^(1|true|yes|y|on)$/i.test(v[id] || '');
  }
  // 시각 목록 (+ 캐시된 시간표로 열차·소요시간 주석)
  times = (v.KTXA_TIMES || '').split(',').map(s => s.trim()).filter(Boolean);
  times.sort();
  annotateTimes();
  // 시간창 → '+N시간' 환산
  const tw = (v.KTXA_TIME_WINDOW || '').split(',').map(s => s.trim());
  let twSel = '';
  if (tw.length === 2 && tw[0] && tw[1]) {
    const [sh, sm] = tw[0].split(':').map(Number);
    const [eh, em] = tw[1].split(':').map(Number);
    const hrs = Math.round(((eh * 60 + em) - (sh * 60 + sm)) / 60);
    if (hrs >= 1 && hrs <= 12) twSel = String(hrs);
  }
  document.getElementById('TW_HOURS').value = twSel;
  // 확보 목표: 값 없으면 기본 1건
  const rl = document.getElementById('KTXA_RESERVE_LIMIT');
  if (!rl.value) rl.value = '1';
  // 좌석 등급 / 승객 / 실행 범위
  setSeat(clean(v.KTXA_SEAT_CLASS || ''));
  setPax(clean(v.KTXA_PASSENGER_TYPE) || '어른', parseInt(v.KTXA_PASSENGERS || '1', 10) || 1);
  setRunMode(clean(v.KTXA_MODE) || 'search', /^(1|true|yes|y|on)$/i.test(v.KTXA_PAYMENT_MODE || ''));
  if (!data.exists) {
    setStatus('warn', `${data.path} 가 아직 없습니다 — 저장하면 새로 생성됩니다.`);
  }
  // 조회 탭의 마지막 조회 조건이 마지막 저장보다 최신이면 여정·출발시각·조회시간에 반영
  let q = null;
  try { q = JSON.parse(localStorage.getItem('binjari_query') || 'null'); } catch (e) {}
  const savedTs = parseInt(localStorage.getItem('binjari_saved_ts') || '0', 10);
  if (q && q.ts > savedTs) {
    if (q.origin) document.getElementById('KTXA_ORIGIN').value = q.origin;
    if (q.dest) document.getElementById('KTXA_DEST').value = q.dest;
    if (q.date) document.getElementById('KTXA_DATE').value = q.date;
    if (q.earliest_hour !== undefined && q.earliest_hour !== null) {
      document.getElementById('addHour').value = String(q.earliest_hour).padStart(2, '0');
      document.getElementById('addMin').value = '00';
      if (q.latest_hour !== null && q.latest_hour !== undefined) {
        const n = q.latest_hour - q.earliest_hour;
        if (n >= 1 && n <= 12) document.getElementById('TW_HOURS').value = String(n);
      }
    }
    annotateTimes();
    setStatus('', '시간표 조회 탭의 최근 조회 조건을 여정에 반영했습니다 — 저장해야 확정됩니다.');
  }
}

// ── 저장: 폼 → /api/settings ──
function collect() {
  const out = {};
  for (const id of TEXT_IDS) {
    const el = document.getElementById(id);
    if (el) out[id] = el.value.trim();
  }
  for (const id of BOOL_IDS) {
    out[id] = document.getElementById(id).checked ? 'true' : 'false';
  }
  out['KTXA_TIMES'] = times.join(',');
  const runmode = document.querySelector('#modeRadios input:checked').value;
  out['KTXA_MODE'] = runmode === 'search' ? 'search' : 'reserve';
  out['KTXA_PAYMENT_MODE'] = runmode === 'pay' ? 'true' : 'false';
  // 감시 범위: 첫 시각 + N시간 → KTXA_TIME_WINDOW
  const twN = document.getElementById('TW_HOURS').value;
  if (twN !== '' && times.length) {
    const [h, m] = times[0].split(':').map(Number);
    const endMin = Math.min(h * 60 + m + parseInt(twN, 10) * 60, 23 * 60 + 59);
    const end = String(Math.floor(endMin / 60)).padStart(2, '0') + ':' + String(endMin % 60).padStart(2, '0');
    out['KTXA_TIME_WINDOW'] = `${times[0]},${end}`;
  } else {
    out['KTXA_TIME_WINDOW'] = '';
  }
  out['KTXA_SEAT_CLASS'] = document.querySelector('#seatRadios input:checked').value;
  // 승객 직렬화
  const counts = PAX_TYPES
    .map(t => [t, parseInt(document.getElementById('pax_' + t).value, 10) || 0])
    .filter(([, n]) => n > 0);
  const total = counts.reduce((a, [, n]) => a + n, 0);
  out['KTXA_PASSENGERS'] = String(total || 1);
  if (counts.length === 0) out['KTXA_PASSENGER_TYPE'] = '어른';
  else if (counts.length === 1) out['KTXA_PASSENGER_TYPE'] = counts[0][0];
  else out['KTXA_PASSENGER_TYPE'] = counts.map(([t, n]) => `${t}:${n}`).join(',');
  return out;
}

function setStatus(cls, msg) {
  const el = document.getElementById('saveStatus');
  el.className = cls; el.textContent = msg;
}

async function save() {
  const values = collect();
  if (PAGE_TAB === 'trip' && !times.length) {
    setStatus('err', '감시할 열차를 1개 이상 선택해 주세요.');
    return false;
  }
  if (window.recordTrip) window.recordTrip(values.KTXA_ORIGIN, values.KTXA_DEST);
  setStatus('', '저장 중...');
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ values }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.status);
    if (data.warning) {
      setStatus('warn', `저장됨 (${data.path}) — 검증 경고:\\n${data.warning}`);
    } else {
      setStatus('ok', `저장 완료 → ${data.path} (${data.saved}개 키)`);
    }
    formDirty = false;
    localStorage.setItem('binjari_saved_ts', String(Date.now()));
    return true;
  } catch (e) {
    setStatus('err', '저장 실패: ' + e.message);
    return false;
  }
}

// ── 워처(발권 감시) 시작/중지/상태 ──
let wtimer = null;

async function pollWatcher() {
  // 감시 시작/중지·상태 표시는 플로팅 바(common.js watcherFab)가 전담.
  // 여기서는 감시 로그 뷰만 갱신한다.
  try {
    const s = await (await fetch('/api/watcher/status')).json();
    const wl = document.getElementById('wlog');
    wl.textContent = s.log_tail || '(로그 없음)';
    wl.scrollTop = wl.scrollHeight;
    if (s.running && !wtimer) wtimer = setInterval(pollWatcher, 5000);
    if (!s.running && wtimer) { clearInterval(wtimer); wtimer = null; }
  } catch (e) { /* 서버 재시작 등 — 다음 폴링에서 복구 */ }
}

async function startWatcher() {
  if (!(await save())) return;
  try {
    const r = await fetch('/api/watcher/start', { method: 'POST' });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.status);
    setStatus('ok', `저장 + 감시 시작 (pid ${data.pid}) — 취소표가 나오면 실행 범위 설정대로 진행합니다.`);
    document.getElementById('wlogWrap').open = true;
  } catch (e) {
    setStatus('err', '감시 시작 실패: ' + e.message);
  }
  pollWatcher();
}

async function stopWatcher() {
  try {
    await fetch('/api/watcher/stop', { method: 'POST' });
    setStatus('', '감시를 중지했습니다.');
  } catch (e) {
    setStatus('err', '중지 실패: ' + e.message);
  }
  pollWatcher();
}

pollWatcher();

load().catch(e => setStatus('err', '설정 로드 실패: ' + e.message));

// ── 다른 탭(시간표 조회)에서 '예매 대상 저장' 한 값 자동 반영 ──
// 폼을 건드리지 않은 상태에서 이 탭이 다시 포커스되면 .env.ktx 를 재로드한다.
// 수정 중(미저장 변경 有)이면 덮어쓰지 않는다.
let formDirty = false;
document.addEventListener('change', e => {
  if (!e.target.closest('#chatPanel')) formDirty = true;
}, true);
function refreshIfClean() {
  if (formDirty) return;
  load().then(() => { formDirty = false; }).catch(() => {});
}
window.addEventListener('focus', refreshIfClean);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshIfClean(); });

// 여정(출발/도착/날짜) 이 바뀌면 시각 칩의 열차·소요시간 주석 갱신
for (const id of ['KTXA_ORIGIN', 'KTXA_DEST', 'KTXA_DATE']) {
  document.getElementById(id).addEventListener('change', annotateTimes);
}

// ── 예매 설정 / 환경 설정 탭 분리 (?tab=env) ──
const PAGE_TAB = new URLSearchParams(location.search).get('tab') === 'env' ? 'env' : 'trip';
document.querySelectorAll('.sec-trip').forEach(s => { s.style.display = PAGE_TAB === 'trip' ? '' : 'none'; });
document.querySelectorAll('.sec-env').forEach(s => { s.style.display = PAGE_TAB === 'env' ? '' : 'none'; });
const currentNav = document.getElementById(PAGE_TAB === 'env' ? 'nav-env' : 'nav-trip');
currentNav.classList.add('on');
currentNav.setAttribute('aria-current', 'page');
if (PAGE_TAB === 'env') {
  document.title = 'binjari — 환경 설정';
  document.getElementById('settingsEyebrow').textContent = 'APP PREFERENCES';
  document.getElementById('settingsTitle').textContent = '환경 설정';
  document.getElementById('settingsSubtitle').textContent = '로그인과 결제, 알림 정보를 한곳에서 안전하게 관리하세요.';
}

// 자주 쓰는 구간 칩 클릭 → 여정 폼 반영
window.applyRoute = function (o, d) {
  const origin = document.getElementById('KTXA_ORIGIN');
  const dest = document.getElementById('KTXA_DEST');
  origin.value = o;
  dest.value = d;
  origin.dispatchEvent(new Event('change'));
  dest.dispatchEvent(new Event('change'));
  formDirty = true;
};

// ── 채팅 패널 연동 ──
window.CHAT_TAB = 'settings';
window.CHAT_GREETING = PAGE_TAB === 'env'
  ? '환경설정을 말로 바꿔보세요.\\n예: "결제까지 자동으로 하고 Teams 알림 켜줘"'
  : '설정을 말로 바꿔보세요.\\n예: "다음주 금요일 아침 수서→부산, 어른 2명 특실로 하고 저장해줘"';
window.applyChatUpdates = function (u, action) {
  let paxTouched = false;
  for (const [k, val] of Object.entries(u)) {
    if (k === 'KTXA_TIMES') {
      times = val.split(',').map(s => s.trim()).filter(Boolean);
      times.sort();
      annotateTimes();
    } else if (k === 'KTXA_TIME_WINDOW') {
      const p = val.split(',').map(s => s.trim());
      let sel = '';
      if (p.length === 2 && p[0] && p[1]) {
        const [sh, sm] = p[0].split(':').map(Number);
        const [eh, em] = p[1].split(':').map(Number);
        const hrs = Math.round(((eh * 60 + em) - (sh * 60 + sm)) / 60);
        if (hrs >= 1 && hrs <= 12) sel = String(hrs);
      }
      document.getElementById('TW_HOURS').value = sel;
    } else if (k === 'KTXA_SEAT_CLASS') {
      setSeat(val);
    } else if (k === 'KTXA_PASSENGER_TYPE' || k === 'KTXA_PASSENGERS') {
      paxTouched = true;
    } else if (k === 'KTXA_MODE' || k === 'KTXA_PAYMENT_MODE') {
      const cur = collect();
      setRunMode(u.KTXA_MODE || cur.KTXA_MODE,
                 /^(1|true|yes|y|on)$/i.test(u.KTXA_PAYMENT_MODE !== undefined ? u.KTXA_PAYMENT_MODE : cur.KTXA_PAYMENT_MODE));
    } else if (BOOL_IDS.includes(k)) {
      document.getElementById(k).checked = /^(1|true|yes|y|on)$/i.test(val);
    } else if (TEXT_IDS.includes(k)) {
      const el = document.getElementById(k);
      if (el) el.value = val;
    }
  }
  if (paxTouched) {
    const cur = collect();
    setPax(u.KTXA_PASSENGER_TYPE || cur.KTXA_PASSENGER_TYPE,
           parseInt(u.KTXA_PASSENGERS || cur.KTXA_PASSENGERS, 10) || 1);
  }
  formDirty = true;
  if (action === 'save') save();
};
</script>
<script src="/chat.js"></script>
<script src="/common.js"></script>
</body>
</html>
"""


@app.get("/settings", response_class=HTMLResponse)
def settings_page() -> str:
    return SETTINGS_HTML


class SettingsRequest(BaseModel):
    values: Dict[str, str]


@app.get("/api/timetable")
def api_timetable(origin: str, dest: str, date: str) -> Dict[str, Any]:
    """캐시된 시간표만 반환 (실조회 없음). 설정 페이지의 감시 시각 주석용."""
    with _CACHE_LOCK:
        cache = _cache_load()
    entry = cache.get(f"{origin.strip()}|{dest.strip()}|{date.strip()}") or {}
    return {"rows": entry.get("rows", []), "fetched_at": entry.get("fetched_at", "")}


@app.get("/api/stations")
def api_stations() -> Dict[str, List[str]]:
    return {"major": STATIONS_MAJOR, "all": STATIONS_ALL}


@app.get("/api/settings")
def api_get_settings() -> Dict[str, Any]:
    env = _read_env_values()
    return {
        "path": str(ENV_PATH),
        "exists": ENV_PATH.is_file(),
        "values": {k: env.get(k, "") for k in SETTINGS_KEYS},
    }


@app.post("/api/settings")
def api_save_settings(req: SettingsRequest) -> Dict[str, Any]:
    updates = {k: v.strip() for k, v in req.values.items() if k in SETTINGS_KEYS}
    if not updates:
        raise HTTPException(status_code=400, detail="저장할 설정 키가 없습니다")
    try:
        _write_env_values(updates)
    except OSError as exc:
        LOGGER.exception(".env.ktx 저장 실패")
        raise HTTPException(status_code=500, detail=f".env.ktx 저장 실패: {exc}") from exc
    warning = _validate_env(_read_env_values())
    LOGGER.info(".env.ktx 저장 완료 (%d개 키)%s", len(updates), " — 검증 경고 있음" if warning else "")
    return {"ok": True, "saved": len(updates), "path": str(ENV_PATH), "warning": warning}


# ───────────────────────── 워처 (발권 감시) 실행 관리 ─────────────────────────

WATCHER_LOG = Path("./runs/watcher_web.log")
_WATCHER: Dict[str, Any] = {"proc": None, "started_at": None, "logf": None}


def _watcher_running() -> bool:
    p = _WATCHER.get("proc")
    return p is not None and p.poll() is None


@app.get("/api/watcher/status")
def api_watcher_status() -> Dict[str, Any]:
    p = _WATCHER.get("proc")
    running = _watcher_running()
    tail = ""
    try:
        tail = "\n".join(
            WATCHER_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-14:]
        )
    except OSError:
        pass
    return {
        "running": running,
        "pid": p.pid if running else None,
        "started_at": _WATCHER.get("started_at"),
        "returncode": None if (running or p is None) else p.returncode,
        "log_tail": tail,
    }


def _start_watcher_proc() -> Dict[str, Any]:
    WATCHER_LOG.parent.mkdir(parents=True, exist_ok=True)
    logf = open(WATCHER_LOG, "a", encoding="utf-8")
    logf.write(f"\n===== 웹에서 감시 시작 {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
    logf.flush()
    creation = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "ktx_watcher.main"],
        stdout=logf, stderr=subprocess.STDOUT,
        cwd=str(Path(__file__).resolve().parent),
        creationflags=creation,
    )
    old_logf = _WATCHER.get("logf")
    if old_logf:
        try:
            old_logf.close()
        except OSError:
            pass
    _WATCHER.update(proc=proc, started_at=datetime.now().strftime("%H:%M:%S"), logf=logf)
    threading.Thread(target=_monitor_proc, args=(proc,), daemon=True).start()
    LOGGER.info("워처 시작 (pid=%d)", proc.pid)
    return {"ok": True, "pid": proc.pid}


@app.post("/api/watcher/start")
def api_watcher_start() -> Dict[str, Any]:
    if _watcher_running():
        raise HTTPException(status_code=409, detail="이미 감시가 실행 중입니다")
    _AI["restarts"] = 0
    _WATCHER["stopping"] = False
    return _start_watcher_proc()


@app.post("/api/watcher/stop")
def api_watcher_stop() -> Dict[str, Any]:
    p = _WATCHER.get("proc")
    if not _watcher_running():
        return {"ok": True, "stopped": False, "detail": "실행 중이 아닙니다"}
    _WATCHER["stopping"] = True
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
    LOGGER.info("워처 중지 (pid=%d)", p.pid)
    return {"ok": True, "stopped": True}


# ─ AI 폴백: 워처가 에러로 멈추면 claude CLI(fable → opus)로 진단·복구 ─

AI_MODELS = ("claude-fable-5", "claude-opus-5")
_AI: Dict[str, Any] = {"restarts": 0}
AI_MAX_RESTARTS = 2


def _wlog(msg: str) -> None:
    try:
        WATCHER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(WATCHER_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | [AI 복구] {msg}\n")
    except OSError:
        pass
    LOGGER.info("[AI 복구] %s", msg)


def _cdp_ops(op: str) -> str:
    """워처의 CDP Chrome 에 대해 안전한 조작만 수행. 실패해도 예외 대신 문자열."""
    try:
        from playwright.sync_api import sync_playwright
        port = _read_env_values().get("KTXA_CDP_PORT", "9444")
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
            pages = [pg for ctx in browser.contexts for pg in ctx.pages]
            kor = [pg for pg in pages if "korail.com" in (pg.url or "")]
            pg = kor[0] if kor else (pages[0] if pages else None)
            if pg is None:
                return "(브라우저에 페이지 없음)"
            if op == "snapshot":
                out = [f"url: {pg.url}"]
                for sel in (".ReactModal__Content", "[role=dialog]"):
                    m = pg.locator(sel).first
                    if m.count() > 0 and m.is_visible():
                        out.append(f"모달: {m.inner_text(timeout=1500)[:300]}")
                out.append("본문: " + pg.locator("body").inner_text(timeout=2000)[:600])
                return "\n".join(out)
            if op == "dismiss_modals":
                n = 0
                for _ in range(3):
                    btn = pg.locator("button:has-text('확인')").first
                    if btn.count() == 0 or not btn.is_visible():
                        break
                    btn.click(timeout=2000)
                    pg.wait_for_timeout(700)
                    n += 1
                return f"모달 {n}개 닫음"
            if op == "goto_search":
                pg.goto("https://www.korail.com/ticket/search/general", timeout=30_000)
                return "검색 페이지로 이동"
            return "(알 수 없는 조작)"
    except Exception as e:
        return f"(CDP 조작 실패: {e})"


def _run_claude_fallback(prompt: str) -> tuple:
    last: Optional[Exception] = None
    for model in AI_MODELS:
        try:
            return model, _run_claude(prompt, model=model)
        except Exception as e:
            last = e
            _wlog(f"모델 {model} 호출 실패 → 다음 모델로 폴백: {e}")
    raise RuntimeError(f"모든 모델 실패: {last}")


def _ai_recover(tail: str) -> None:
    if _AI["restarts"] >= AI_MAX_RESTARTS:
        _wlog(f"자동 복구 한도({AI_MAX_RESTARTS}회) 도달 — 수동 확인이 필요합니다")
        return
    snap = _cdp_ops("snapshot")
    prompt = f"""너는 KTX 예매 워처(코레일 브라우저 자동화)의 장애 복구 담당이다.
워처가 비정상 종료했다. 로그와 브라우저 상태를 보고 원인을 진단하고 복구를 결정하라.
반드시 아래 JSON 만 출력 (설명·코드펜스 금지):
{{"diagnosis": "<원인 한두 문장>", "actions": ["dismiss_modals" 또는 "goto_search" 중 필요한 것],
 "restart": true|false, "note": "<사용자 안내 한 문장>"}}
restart 판단 기준: 안내 모달·복호화오류·일시 네트워크 등 재시도로 풀릴 오류면 true.
비밀번호 오류·로그인 5회 실패 경고·사이트 구조 변경·결제 실패·중복 예매 차단 등
사람이 봐야 하는 상황이면 false.

[워처 로그 마지막 부분]
{tail[-3000:]}

[브라우저 현재 상태]
{snap[:1500]}"""
    model, raw = _run_claude_fallback(prompt)
    result = _extract_json(raw)
    _wlog(f"진단({model}): {str(result.get('diagnosis', ''))[:300]}")
    note = str(result.get("note", "")).strip()
    if note:
        _wlog(f"안내: {note[:200]}")
    for action in list(result.get("actions") or [])[:3]:
        if action in ("dismiss_modals", "goto_search"):
            _wlog(f"복구 조치 {action}: {_cdp_ops(action)}")
    if result.get("restart") is True and not _watcher_running():
        _AI["restarts"] += 1
        _wlog(f"워처 자동 재시작 ({_AI['restarts']}/{AI_MAX_RESTARTS})")
        _start_watcher_proc()
    elif not result.get("restart"):
        _wlog("자동 재시작 보류 — 사람 확인 필요로 판단됨")


def _monitor_proc(proc: subprocess.Popen) -> None:
    rc = proc.wait()
    if _WATCHER.get("proc") is not proc:
        return                       # 다른 실행으로 대체됨
    if _WATCHER.get("stopping"):
        return                       # 사용자가 중지한 경우
    try:
        tail = "\n".join(
            WATCHER_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        )
    except OSError:
        tail = ""
    if "감시 종료" in tail:
        return                       # 목표 확보 정상 종료
    if rc == 0 and "❌" not in tail:
        return
    _wlog(f"워처 비정상 종료 감지 (rc={rc}) — AI 진단 시작 (fable→opus 폴백)")
    try:
        _ai_recover(tail)
    except Exception as e:
        _wlog(f"AI 진단 자체가 실패: {e}")


# ───────────────────────── 채팅 (claude CLI headless) ─────────────────────────

# 채팅 프롬프트에 현재값 대신 (설정됨)/(미설정) 만 노출하는 키
SECRET_KEYS = {
    "KTXA_USER", "KTXA_PASS",
    "PAY_CARD_NUM", "PAY_CARD_MM", "PAY_CARD_YY", "PAY_CARD_PW2", "PAY_ID6",
    "KTXA_TRANSFER_MEMBER_NO", "KTXA_TRANSFER_PHONE",
    "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID",
}
SEARCH_FIELDS = {"origin", "dest", "date", "earliest_hour", "latest_hour", "train_no", "passengers"}


class ChatRequest(BaseModel):
    tab: str = Field(..., description="settings | search")
    message: str
    history: List[Dict[str, str]] = Field(default_factory=list)


def _chat_context(tab: str) -> str:
    today = _date.today().isoformat()
    if tab == "search":
        return f"""너는 binjari KTX 시간표 조회 폼 도우미다. 오늘 날짜: {today}
사용자의 자연어 요청을 조회 폼 값으로 바꾼다. 반드시 아래 JSON 만 출력한다 (설명·코드펜스 금지):
{{"reply": "<한국어 한두 문장 답변>", "updates": {{...}}, "action": "none"}}
updates 에 넣을 수 있는 키:
  origin(출발역), dest(도착역), date(YYYY-MM-DD, '내일' 등 상대 날짜는 오늘 기준 환산),
  earliest_hour(시간대 시작 0~23 정수), latest_hour(시간대 끝 1~24 정수·그 정각까지 포함, 제한 없으면 ""),
  train_no(열차번호 필터, 전체면 ""), passengers(인원 1~9 정수)
사용자가 조회/검색 실행까지 원하면 "action": "search".
바꿀 값이 없으면 updates 는 빈 객체."""

    env = _read_env_values()
    cur = []
    for k in SETTINGS_KEYS:
        v = env.get(k, "")
        if k in SECRET_KEYS:
            v = "(설정됨)" if v and not re.fullmatch(r"<.*>", v) else "(미설정)"
        cur.append(f"  {k}={v}")
    return f"""너는 binjari KTX 예매 설정(.env.ktx) 도우미다. 오늘 날짜: {today}
사용자의 자연어 요청을 설정 키 값으로 바꾼다. 반드시 아래 JSON 만 출력한다 (설명·코드펜스 금지):
{{"reply": "<한국어 한두 문장 답변>", "updates": {{"KEY": "value", ...}}, "action": "none"}}
사용자가 저장까지 원하면 "action": "save". 바꿀 값이 없으면 updates 는 빈 객체.
값 형식 규칙:
  KTXA_DATE=YYYY-MM-DD (상대 날짜는 오늘 기준 환산) / KTXA_TIMES=HH:MM 콤마 목록
  KTXA_TIME_WINDOW=HH:MM,HH:MM 또는 빈값 / KTXA_SEAT_CLASS=일반실|특실|빈값(무관)
  KTXA_PASSENGER_TYPE: 단일 유형명(어른/어린이/유아/경로/중증장애인/경증장애인/국가유공자)
    또는 혼합 "어른:1,경로:1" / KTXA_PASSENGERS=총 인원
  불리언 키는 true|false / PAY_CARD_NUM=9999-9999-9999-9999 / KTXA_MODE=search|reserve
현재 설정 (비밀값은 마스킹):
{chr(10).join(cur)}"""


def _find_claude() -> Optional[str]:
    exe = shutil.which("claude")
    if exe:
        return exe
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "claude",
        home / ".local" / "bin" / "claude.exe",
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _run_claude(prompt: str, model: Optional[str] = None) -> str:
    exe = _find_claude()
    if not exe:
        raise HTTPException(status_code=503, detail="claude CLI 를 찾을 수 없습니다 (PATH 확인)")
    cmd = [exe, "-p", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    if exe.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c"] + cmd
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="claude 응답 시간 초과 (180초)") from exc
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=f"claude 실행 실패: {(proc.stderr or '')[-500:]}")
    try:
        return json.loads(proc.stdout).get("result", "")
    except json.JSONDecodeError:
        return proc.stdout


def _extract_json(text: str) -> Dict[str, Any]:
    """모델 응답에서 첫 JSON 객체를 추출. 실패하면 원문을 reply 로."""
    start = text.find("{")
    if start >= 0:
        for end in range(len(text), start, -1):
            try:
                obj = json.loads(text[start:end])
                if isinstance(obj, dict):
                    return obj
                break
            except json.JSONDecodeError:
                continue
    return {"reply": text.strip() or "(빈 응답)", "updates": {}, "action": "none"}


@app.post("/api/chat")
def api_chat(req: ChatRequest) -> Dict[str, Any]:
    if req.tab not in {"settings", "search"}:
        raise HTTPException(status_code=400, detail="tab 은 settings | search")
    parts = [_chat_context(req.tab)]
    if req.history:
        parts.append("\n이전 대화:")
        for m in req.history[-8:]:
            parts.append(f"  [{m.get('role', '?')}] {m.get('text', '')}")
    parts.append(f"\n사용자 요청: {req.message}")
    result = _extract_json(_run_claude("\n".join(parts)))

    updates = result.get("updates") or {}
    if not isinstance(updates, dict):
        updates = {}
    allowed = set(SETTINGS_KEYS) if req.tab == "settings" else SEARCH_FIELDS
    updates = {k: str(v) for k, v in updates.items() if k in allowed}
    action = result.get("action", "none")
    if action not in {"none", "save", "search"}:
        action = "none"
    LOGGER.info("chat[%s]: %d개 필드 변경, action=%s", req.tab, len(updates), action)
    return {"reply": str(result.get("reply", "")), "updates": updates, "action": action}


CHAT_JS = """
(function () {
  const panel = document.getElementById('chatPanel');
  if (!panel) return;
  panel.setAttribute('role', 'region');
  panel.setAttribute('aria-labelledby', 'assistantTitle');
  const style = document.createElement('style');
  style.textContent = `
    #chatPanel { width: 340px; flex-shrink: 0; position: sticky; top: 16px;
      background: #fff; border: 1px solid #e2e6ee; border-radius: 10px;
      display: flex; flex-direction: column; height: calc(100vh - 110px); min-height: 420px; }
    .chat-head { padding: 10px 14px; font-weight: 700; font-size: 14px;
      border-bottom: 1px solid #eee; color: #14458f; }
    .chat-head small { font-weight: 400; color: #999; margin-left: 6px; }
    .chat-msgs { flex: 1; overflow-y: auto; padding: 12px; display: flex;
      flex-direction: column; gap: 8px; font-size: 13px; }
    .chat-msg { padding: 8px 10px; border-radius: 10px; max-width: 92%;
      white-space: pre-wrap; word-break: break-word; line-height: 1.45; }
    .chat-msg.user { align-self: flex-end; background: #e6efff; }
    .chat-msg.bot { align-self: flex-start; background: #f2f4f8; }
    .chat-msg.err { align-self: flex-start; background: #ffe6e6; color: #a02020; }
    .chat-msg.note { align-self: center; background: none; color: #999; font-size: 11px; padding: 0; }
    .chat-input { display: flex; gap: 6px; padding: 10px; border-top: 1px solid #eee; }
    .chat-input textarea { flex: 1; resize: none; height: 46px; padding: 6px 8px;
      font-size: 13px; border: 1px solid #c8cedb; border-radius: 6px; font-family: inherit; }
    .chat-input button { padding: 0 14px; background: #1c5bbd; color: #fff;
      border: 0; border-radius: 6px; cursor: pointer; font-size: 13px; }
    .chat-input button:disabled { background: #9db4d8; }
  `;
  document.head.appendChild(style);

  panel.innerHTML = `
    <div class="chat-head" id="assistantTitle">여정 도우미 <small>말로 입력하면 폼에 바로 반영해 드려요</small></div>
    <div class="chat-msgs" id="chatMsgs" role="log" aria-live="polite" aria-relevant="additions">
      <div class="chat-msg bot">${window.CHAT_GREETING || '무엇을 도와드릴까요?'}</div>
    </div>
    <div class="chat-input">
      <textarea id="chatText" aria-label="여정 도우미에게 요청하기"
        placeholder="예: 내일 오전 서울에서 부산, 어른 2명"></textarea>
      <button type="button" id="chatSend">보내기</button>
    </div>`;

  const msgs = document.getElementById('chatMsgs');
  const text = document.getElementById('chatText');
  const btn = document.getElementById('chatSend');
  const history = [];

  function bubble(cls, s) {
    const d = document.createElement('div');
    d.className = 'chat-msg ' + cls;
    d.textContent = s;
    msgs.appendChild(d);
    msgs.scrollTop = msgs.scrollHeight;
    return d;
  }

  async function send() {
    const q = text.value.trim();
    if (!q || btn.disabled) return;
    text.value = '';
    bubble('user', q);
    history.push({ role: 'user', text: q });
    btn.disabled = true;
    const pending = bubble('note', '생각 중…');
    try {
      const r = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tab: window.CHAT_TAB, message: q, history: history.slice(0, -1) }),
      });
      const data = await r.json();
      pending.remove();
      if (!r.ok) throw new Error(data.detail || r.status);
      bubble('bot', data.reply || '(응답 없음)');
      history.push({ role: 'assistant', text: data.reply || '' });
      const n = Object.keys(data.updates || {}).length;
      if (n && window.applyChatUpdates) {
        window.applyChatUpdates(data.updates, data.action);
        bubble('note', `폼에 ${n}개 항목 반영됨` + (data.action !== 'none' ? ` · ${data.action} 실행` : ''));
      } else if (data.action !== 'none' && window.applyChatUpdates) {
        window.applyChatUpdates({}, data.action);
      }
    } catch (e) {
      pending.remove();
      bubble('err', '오류: ' + e.message);
    } finally {
      btn.disabled = false;
      text.focus();
    }
  }

  btn.addEventListener('click', send);
  text.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
})();
"""


@app.get("/chat.js")
def chat_js() -> Response:
    return Response(content=CHAT_JS, media_type="application/javascript; charset=utf-8")


COMMON_JS = """
// 역 목록(datalist) + 자주 쓰는 역/구간 칩. 사용 페이지 규약:
//   <datalist id="stations"></datalist>               ← 전체 역으로 채워짐
//   <div class="stn-chips" data-target="입력id"></div> ← 자주 쓰는 역 10개 칩
//   <div id="routeChips"></div> + window.applyRoute(o,d) ← 자주 쓰는 구간 칩
//   window.recordTrip(o,d) 를 조회/저장 시점에 호출하면 사용 횟수가 쌓인다.
(function () {
  const style = document.createElement('style');
  style.textContent = `
    .stn-chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 5px; max-width: 340px; }
    .stn-chips button, #routeChips button { border: 1px solid #c8d4ea; background: #f4f7fc;
      color: #2a4d86; border-radius: 12px; padding: 2px 9px; font-size: 12px; cursor: pointer; }
    .stn-chips button:hover, #routeChips button:hover { background: #dce9fb; }
    .stn-chips button.fav { background: #fff3d6; border-color: #e2c47c; color: #7a5b10; }
    .stn-chips button.fav:hover { background: #ffe9b8; }
    .stn-chips button.drag-over { outline: 2px dashed #1c5bbd; outline-offset: 1px; }
    #routeChips { display: flex; flex-wrap: wrap; gap: 6px; margin: 2px 0 12px; align-items: center; }
    #routeChips .rc-label { font-size: 12px; color: #888; margin-right: 2px; }
    #routeChips button { border-radius: 6px; padding: 4px 12px; font-weight: 600; }
  `;
  document.head.appendChild(style);

  let MAJOR = [];
  function load(key) {
    try { return JSON.parse(localStorage.getItem(key) || '{}'); } catch (e) { return {}; }
  }
  function bump(key, name) {
    const m = load(key);
    m[name] = (m[name] || 0) + 1;
    localStorage.setItem(key, JSON.stringify(m));
  }
  function topOf(key, n) {
    return Object.entries(load(key)).sort((a, b) => b[1] - a[1]).slice(0, n).map(e => e[0]);
  }

  // ── 역 즐겨찾기: 칩을 드래그해 목록 앞(즐겨찾기)에 고정, 드래그로 순서 변경,
  //    더블클릭으로 해제. 출발/도착 칩 목록이 같은 즐겨찾기를 공유한다. ──
  let FAVS = (function () {
    try {
      const v = JSON.parse(localStorage.getItem('binjari_fav') || '[]');
      return Array.isArray(v) ? v : [];
    } catch (e) { return []; }
  })();
  function saveFavs() { localStorage.setItem('binjari_fav', JSON.stringify(FAVS)); }
  function pinFav(name, idx) {
    FAVS = FAVS.filter(x => x !== name);
    FAVS.splice(Math.min(idx, FAVS.length), 0, name);
    saveFavs();
    renderStationChips();
  }
  function unpinFav(name) {
    FAVS = FAVS.filter(x => x !== name);
    saveFavs();
    renderStationChips();
  }

  function chipStations() {
    const list = [...FAVS];
    const cap = Math.max(10, FAVS.length);
    for (const s of [...topOf('binjari_stn', 10), ...MAJOR]) {
      if (list.length >= cap) break;
      if (!list.includes(s)) list.push(s);
    }
    return list;
  }

  function renderStationChips() {
    const list = chipStations();
    document.querySelectorAll('.stn-chips').forEach(box => {
      const input = document.getElementById(box.dataset.target);
      if (!input) return;
      box.innerHTML = '';
      list.forEach((s, i) => {
        const b = document.createElement('button');
        b.type = 'button';
        const isFav = FAVS.includes(s);
        b.textContent = (isFav ? '★ ' : '') + s;
        b.className = isFav ? 'fav' : '';
        b.title = isFav
          ? '클릭: 입력 · 드래그: 순서 변경 · 더블클릭: 즐겨찾기 해제'
          : '클릭: 입력 · 드래그해서 앞에 놓으면 즐겨찾기 고정';
        b.draggable = true;
        b.dataset.name = s;
        b.dataset.index = i;
        b.onclick = () => { input.value = s; input.dispatchEvent(new Event('change')); };
        b.ondblclick = () => { if (isFav) unpinFav(s); };
        b.addEventListener('dragstart', e => {
          e.dataTransfer.setData('text/plain', s);
          e.dataTransfer.effectAllowed = 'move';
        });
        b.addEventListener('dragover', e => {
          e.preventDefault();
          e.dataTransfer.dropEffect = 'move';
          b.classList.add('drag-over');
        });
        b.addEventListener('dragleave', () => b.classList.remove('drag-over'));
        b.addEventListener('drop', e => {
          e.preventDefault();
          e.stopPropagation();
          b.classList.remove('drag-over');
          const name = e.dataTransfer.getData('text/plain');
          if (name && name !== s) pinFav(name, parseInt(b.dataset.index, 10));
        });
        box.appendChild(b);
      });
      // 칩 사이 빈 곳에 떨어뜨리면 즐겨찾기 맨 뒤에 고정
      box.ondragover = e => { e.preventDefault(); };
      box.ondrop = e => {
        e.preventDefault();
        const name = e.dataTransfer.getData('text/plain');
        if (name) pinFav(name, FAVS.length);
      };
    });
  }

  function renderRouteChips() {
    const box = document.getElementById('routeChips');
    if (!box) return;
    const routes = topOf('binjari_route', 6);
    box.innerHTML = '';
    if (!routes.length) return;
    const lab = document.createElement('span');
    lab.className = 'rc-label';
    lab.textContent = '자주 쓰는 구간:';
    box.appendChild(lab);
    for (const r of routes) {
      const [o, d] = r.split('→');
      if (!o || !d) continue;
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = `${o} → ${d}`;
      b.onclick = () => { if (window.applyRoute) window.applyRoute(o, d); };
      box.appendChild(b);
    }
  }

  window.recordTrip = function (o, d) {
    o = (o || '').trim(); d = (d || '').trim();
    if (o) bump('binjari_stn', o);
    if (d) bump('binjari_stn', d);
    if (o && d) bump('binjari_route', `${o}→${d}`);
    renderStationChips();
    renderRouteChips();
  };

  // ── 감시 시작/중지 플로팅 바: 모든 페이지 하단 중앙에 항상 고정 ──
  (function initWatcherFab() {
    const s2 = document.createElement('style');
    s2.textContent = `
      #watcherFab { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%);
        z-index: 900; display: flex; align-items: center; gap: 10px;
        padding: 9px 14px; border-radius: 999px;
        background: rgba(255,255,255,.92); border: 1px solid rgba(0,0,0,.09);
        box-shadow: 0 10px 34px rgba(0,0,0,.18);
        -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px);
        font-size: 13px; white-space: nowrap; }
      #watcherFab .wf-state { color: #555; }
      #watcherFab .wf-state.running { color: #0a8a3c; font-weight: 700; }
      #watcherFab .wf-state.err { color: #c07b00; }
      #watcherFab button { border: 0; border-radius: 999px; padding: 8px 16px;
        font-weight: 700; font-size: 13px; color: #fff; cursor: pointer; }
      #watcherFab .wf-start { background: #0a8a3c; }
      #watcherFab .wf-start:hover { background: #0da046; }
      #watcherFab .wf-stop { background: #c02020; }
      #watcherFab .wf-stop:hover { background: #d92c2c; }
      #watcherFab button:disabled { opacity: .55; cursor: not-allowed; }
    `;
    document.head.appendChild(s2);
    const fab = document.createElement('div');
    fab.id = 'watcherFab';
    fab.innerHTML =
      '<span class="wf-state" id="wfState">감시 상태 확인 중…</span>' +
      '<button type="button" class="wf-start" id="wfStart" hidden>▶ 감시 시작</button>' +
      '<button type="button" class="wf-stop" id="wfStop" hidden>■ 감시 중지</button>';
    document.body.appendChild(fab);
    // 설정 페이지의 sticky 저장바와 겹치지 않게 위로 올림
    if (document.querySelector('.savebar')) fab.style.bottom = '96px';
    const state = document.getElementById('wfState');
    const startBtn = document.getElementById('wfStart');
    const stopBtn = document.getElementById('wfStop');
    startBtn.onclick = async () => {
      startBtn.disabled = true;
      try {
        if (window.startWatcher) {
          await window.startWatcher();   // 설정 페이지: 설정 저장 후 시작
        } else {
          const r = await fetch('/api/watcher/start', { method: 'POST' });
          const d = await r.json();
          if (!r.ok) throw new Error(d.detail || r.status);
        }
      } catch (e) {
        state.textContent = '시작 실패: ' + e.message;
        state.className = 'wf-state err';
      }
      startBtn.disabled = false;
      poll();
    };
    stopBtn.onclick = async () => {
      if (!confirm('감시(발권 워처)를 중지할까요?')) return;
      stopBtn.disabled = true;
      try {
        if (window.stopWatcher) { await window.stopWatcher(); }
        else { await fetch('/api/watcher/stop', { method: 'POST' }); }
      } catch (e) {}
      stopBtn.disabled = false;
      poll();
    };
    async function poll() {
      try {
        const s = await (await fetch('/api/watcher/status')).json();
        if (s.running) {
          state.textContent = '● 감시 실행 중 (' + (s.started_at || '') + '~)';
          state.className = 'wf-state running';
          startBtn.hidden = true;
          stopBtn.hidden = false;
        } else {
          if (s.returncode === 0) {
            state.textContent = '✅ 감시 완료';
            state.className = 'wf-state running';
          } else if (s.returncode !== null && s.returncode !== undefined) {
            state.textContent = '감시 종료 (code ' + s.returncode + ')';
            state.className = 'wf-state err';
          } else {
            state.textContent = '감시 꺼짐';
            state.className = 'wf-state';
          }
          startBtn.hidden = false;
          stopBtn.hidden = true;
        }
      } catch (e) { /* 서버 미응답 — 다음 폴링 */ }
    }
    poll();
    setInterval(poll, 5000);
  })();

  fetch('/api/stations').then(r => r.json()).then(data => {
    MAJOR = data.major || [];
    const dl = document.getElementById('stations');
    if (dl) {
      dl.innerHTML = '';
      const seen = new Set();
      for (const s of [...data.major, ...data.all]) {
        if (seen.has(s)) continue;
        seen.add(s);
        const o = document.createElement('option');
        o.value = s;
        dl.appendChild(o);
      }
    }
    renderStationChips();
    renderRouteChips();
  }).catch(() => { renderStationChips(); renderRouteChips(); });
})();
"""


@app.get("/common.js")
def common_js() -> Response:
    return Response(content=COMMON_JS, media_type="application/javascript; charset=utf-8")


@app.post("/api/search/ktx")
def api_search_ktx(req: SearchRequest) -> Dict[str, Any]:
    _parse_date(req.date)
    try:
        result = _ktx_list_schedules(req)
    except Exception as exc:
        LOGGER.exception("KTX 검색 실패")
        raise HTTPException(status_code=500, detail=f"KTX 검색 실패: {exc}") from exc
    return {"rail": "KTX", "count": len(result["rows"]), "rows": result["rows"],
            "cached": result["cached"], "fetched_at": result["fetched_at"]}


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
