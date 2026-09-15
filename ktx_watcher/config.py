"""KTX-A watcher config (CDP-only)."""

from __future__ import annotations

import os
import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


ENV_FILES = (Path(".env.ktx"), Path("env") / ".env", Path(".env"))

# .env.ktx.example 의 "<...>" 자리표시자를 실값 대신 그대로 남겨둔 경우 —
# 문자 그대로 파일 경로/역명 등에 쓰이면 (예: KTXA_CDP_USER_DATA_DIR 가
# "<사용자별 Chrome 프로필 폴더 경로>" 그대로 Chrome --user-data-dir 로 들어가
# 그 이름의 디렉터리가 실제로 생겨버림) 조용히 이상 동작하므로, 값을 채우지
# 않은 것과 동일하게(빈 문자열) 취급한다.
_PLACEHOLDER_RE = re.compile(r"^<.*>$")


class ConfigError(RuntimeError):
    """Raised when configuration validation fails."""


# 인원선택 팝업의 승객 유형 (korail SPA, CDP probe 2026-07-07)
PASSENGER_TYPES = ("어른", "어린이", "유아", "경로", "중증장애인", "경증장애인", "국가유공자")


def _load_env_files() -> None:
    for f in ENV_FILES:
        if f.is_file():
            load_dotenv(f, override=False)


def _boolify(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


class KTXAConfig(BaseModel):
    """CDP-only Korail watcher 설정."""

    # ─ Required (CDP 강제) ─
    ktxa_cdp_port: int = Field(9222, alias="KTXA_CDP_PORT")
    ktxa_chrome_exe: Optional[str] = Field(None, alias="KTXA_CHROME_EXE")
    ktxa_cdp_user_data_dir: Optional[str] = Field(
        None, alias="KTXA_CDP_USER_DATA_DIR"
    )
    ktxa_cdp_startup_timeout: float = Field(15.0, alias="KTXA_CDP_STARTUP_TIMEOUT")
    # true 면 Chrome 창을 별도 가상 데스크톱("binjari")으로 자동 이동 (작업 화면 방해 X)
    ktxa_vdesk: bool = Field(False, alias="KTXA_VDESK")

    # ─ Trip ─
    ktxa_origin: str = Field(alias="KTXA_ORIGIN")
    ktxa_dest: str = Field(alias="KTXA_DEST")
    ktxa_date: date = Field(alias="KTXA_DATE")
    ktxa_times: List[time] = Field(alias="KTXA_TIMES")
    ktxa_passengers: int = Field(1, alias="KTXA_PASSENGERS")
    # 승객 유형: 단일 유형명("경로") 이면 KTXA_PASSENGERS 명 전원 그 유형.
    # "어른:1,경로:1" 형식이면 유형별 인원 직접 지정 (KTXA_PASSENGERS 무시).
    ktxa_passenger_type: str = Field("어른", alias="KTXA_PASSENGER_TYPE")
    ktxa_seat_class: str = Field("", alias="KTXA_SEAT_CLASS")
    # true 면 바로 좌석 예매 가능한 열차만 후보 (입석+좌석/예약대기 제외)
    ktxa_seated_only: bool = Field(False, alias="KTXA_SEATED_ONLY")
    ktxa_train_type: str = Field("KTX", alias="KTXA_TRAIN_TYPE")
    # true 면 검색 시 "에스알티(SRT) 함께 보기" 옵션을 켠다 (수서 발착 SRT 열차 포함)
    ktxa_include_srt: bool = Field(False, alias="KTXA_INCLUDE_SRT")
    ktxa_tolerance_min: int = Field(0, alias="KTXA_TOLERANCE_MIN")
    # 확보 목표 건수: 감시 시각(열차) 중 몇 건을 예약하면 종료할지.
    # 1 = 아무거나 1건(기본), 0 = 선택한 열차 전부.
    ktxa_reserve_limit: int = Field(1, alias="KTXA_RESERVE_LIMIT")
    ktxa_time_window: Optional[Tuple[time, time]] = Field(
        None, alias="KTXA_TIME_WINDOW"
    )

    # ─ Credentials (reserve 모드에서만 필수) ─
    ktxa_user: str = Field("", alias="KTXA_USER")
    ktxa_pass: str = Field("", alias="KTXA_PASS")

    # ─ Polling ─
    ktxa_poll_min: int = Field(3, alias="KTXA_POLL_MIN")
    ktxa_poll_max: int = Field(10, alias="KTXA_POLL_MAX")
    ktxa_once: bool = Field(False, alias="KTXA_ONCE")

    # ─ Misc ─
    ktxa_mode: str = Field("search", alias="KTXA_MODE")
    ktxa_log_dir: Path = Field(Path("./runs"), alias="KTXA_LOG_DIR")
    ktxa_log_level: str = Field("INFO", alias="KTXA_LOG_LEVEL")

    # ─ Payment (reserve mode 끝나면 자동 결제까지) ─
    # KTXA_PAYMENT_MODE=true 면 결제 페이지에서 카드 정보 입력 + 결제/발권 클릭까지.
    # 카드 정보는 .env 의 PAY_* 변수.
    ktxa_payment_mode: bool = Field(False, alias="KTXA_PAYMENT_MODE")
    pay_card_num: str = Field("", alias="PAY_CARD_NUM")
    pay_card_mm: str = Field("", alias="PAY_CARD_MM")
    pay_card_yy: str = Field("", alias="PAY_CARD_YY")
    pay_card_pw2: str = Field("", alias="PAY_CARD_PW2")
    pay_id6: str = Field("", alias="PAY_ID6")

    # ─ 승차권 전달하기 (발권 후 자동 전달) ─
    ktxa_transfer_enabled: bool = Field(False, alias="KTXA_TRANSFER_ENABLED")
    # False 면 수신자 입력까지만 하고 '전송하기' 직전 중단 (dry-run)
    ktxa_transfer_send: bool = Field(False, alias="KTXA_TRANSFER_SEND")
    ktxa_transfer_member_no: str = Field("", alias="KTXA_TRANSFER_MEMBER_NO")
    ktxa_transfer_name: str = Field("", alias="KTXA_TRANSFER_NAME")
    ktxa_transfer_phone: str = Field("", alias="KTXA_TRANSFER_PHONE")

    # ─ Notifier (선택) ─
    teams_enabled: bool = Field(False, alias="TEAMS_ENABLED")
    teams_user_email: Optional[str] = Field(None, alias="TEAMS_USER_EMAIL")
    teams_chat_id: Optional[str] = Field(None, alias="TEAMS_CHAT_ID")
    teams_recipient_name: Optional[str] = Field(None, alias="TEAMS_RECIPIENT_NAME")
    teams_prefix: str = Field("[binjari KTX]", alias="TEAMS_PREFIX")

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # ─ validators ─
    @model_validator(mode="before")
    @classmethod
    def _strip_placeholders(cls, data):
        if isinstance(data, dict):
            for k, v in list(data.items()):
                if isinstance(v, str) and _PLACEHOLDER_RE.match(v.strip()):
                    data[k] = ""
        return data

    @field_validator("ktxa_date", mode="before")
    @classmethod
    def _parse_date(cls, v):
        if isinstance(v, date):
            return v
        try:
            return datetime.strptime(v, "%Y-%m-%d").date()
        except (TypeError, ValueError) as e:
            raise ValueError("KTXA_DATE must be YYYY-MM-DD") from e

    @field_validator("ktxa_times", mode="before")
    @classmethod
    def _parse_times(cls, v):
        if isinstance(v, str):
            entries = [x.strip() for x in v.split(",") if x.strip()]
        else:
            entries = list(v or [])
        if not entries:
            raise ValueError("KTXA_TIMES must include at least one HH:MM")
        out = []
        for item in entries:
            if isinstance(item, time):
                out.append(item)
                continue
            try:
                out.append(datetime.strptime(item, "%H:%M").time())
            except Exception as e:
                raise ValueError(f"Invalid time: {item!r}") from e
        return out

    @field_validator("ktxa_time_window", mode="before")
    @classmethod
    def _parse_window(cls, v):
        if v in ("", None):
            return None
        if isinstance(v, tuple):
            return v
        parts = [p.strip() for p in str(v).split(",") if p.strip()]
        if len(parts) != 2:
            raise ValueError("KTXA_TIME_WINDOW must be 'HH:MM,HH:MM'")
        try:
            s = datetime.strptime(parts[0], "%H:%M").time()
            e = datetime.strptime(parts[1], "%H:%M").time()
        except Exception as exc:
            raise ValueError("invalid KTXA_TIME_WINDOW") from exc
        if s > e:
            raise ValueError("KTXA_TIME_WINDOW start must be <= end")
        return s, e

    @field_validator("ktxa_once", "teams_enabled", "ktxa_payment_mode", "ktxa_seated_only",
                     "ktxa_transfer_enabled", "ktxa_transfer_send", "ktxa_vdesk", mode="before")
    @classmethod
    def _parse_bool(cls, v):
        return _boolify(v)

    @field_validator("ktxa_log_dir", mode="before")
    @classmethod
    def _parse_log_dir(cls, v):
        return v if isinstance(v, Path) else Path(str(v))

    @field_validator("ktxa_log_level")
    @classmethod
    def _upper(cls, v):
        return v.upper()

    @field_validator("ktxa_origin", "ktxa_dest", "ktxa_train_type", mode="after")
    @classmethod
    def _non_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("required value cannot be empty")
        return v.strip()

    @field_validator("ktxa_seat_class", mode="after")
    @classmethod
    def _seat_class_opt(cls, v):
        return (v or "").strip()

    @field_validator("ktxa_passenger_type", mode="after")
    @classmethod
    def _passenger_type(cls, v):
        v = (v or "").replace(" ", "") or "어른"
        names = [p.partition(":")[0] for p in v.split(",")] if ":" in v else [v]
        for name in names:
            if name not in PASSENGER_TYPES:
                raise ValueError(
                    f"KTXA_PASSENGER_TYPE 유형 오류: {name!r} (가능: {', '.join(PASSENGER_TYPES)})"
                )
        if ":" in v:
            for p in v.split(","):
                cnt = p.partition(":")[2]
                if not cnt.isdigit() or int(cnt) < 0:
                    raise ValueError(f"KTXA_PASSENGER_TYPE 인원 오류: {p!r} (예: 어른:1,경로:1)")
        return v

    def passenger_counts(self) -> Dict[str, int]:
        """유형별 인원 {유형: N, ...}. 명시 안 된 유형은 0명으로 간주."""
        if ":" in self.ktxa_passenger_type:
            out: Dict[str, int] = {}
            for p in self.ktxa_passenger_type.split(","):
                name, _, cnt = p.partition(":")
                out[name] = out.get(name, 0) + int(cnt)
            return {k: n for k, n in out.items() if n > 0}
        return {self.ktxa_passenger_type: self.ktxa_passengers}

    @field_validator("ktxa_user", "ktxa_pass", "ktxa_transfer_member_no",
                     "ktxa_transfer_name", "ktxa_transfer_phone", mode="after")
    @classmethod
    def _strip_opt(cls, v):
        return (v or "").strip()

    @model_validator(mode="after")
    def _final(cls, values: "KTXAConfig"):
        if values.ktxa_date < date.today():
            raise ValueError(
                f"KTXA_DATE({values.ktxa_date})가 이미 지난 날짜입니다 — 오늘({date.today()}) "
                "이후 날짜로 설정하세요 (Chrome 을 띄운 뒤 date picker 에서 지난 날짜를 "
                "못 찾아 한참 뒤에야 실패하므로, 여기서 먼저 걸러낸다)"
            )
        if values.ktxa_reserve_limit < 0:
            raise ValueError("KTXA_RESERVE_LIMIT must be >= 0 (0=전부)")
        if values.ktxa_poll_min <= 0:
            raise ValueError("KTXA_POLL_MIN must be > 0")
        if values.ktxa_poll_max < values.ktxa_poll_min:
            raise ValueError("KTXA_POLL_MAX must be >= KTXA_POLL_MIN")
        if values.ktxa_cdp_port <= 0 or values.ktxa_cdp_port > 65535:
            raise ValueError("KTXA_CDP_PORT out of range")
        if sum(values.passenger_counts().values()) <= 0:
            raise ValueError("승객 인원 합이 0명 (KTXA_PASSENGERS / KTXA_PASSENGER_TYPE 확인)")
        if values.ktxa_mode not in {"search", "reserve"}:
            raise ValueError("KTXA_MODE must be 'search' or 'reserve'")
        if values.ktxa_mode == "reserve":
            if not values.ktxa_user:
                raise ValueError("KTXA_USER required for reserve mode")
            if not values.ktxa_pass:
                raise ValueError("KTXA_PASS required for reserve mode")
        return values


_RAIL_SHARED_MAP = (
    ("RAIL_ORIGIN", "KTXA_ORIGIN"),
    ("RAIL_DEST", "KTXA_DEST"),
    ("RAIL_DATE", "KTXA_DATE"),
    ("RAIL_TIMES", "KTXA_TIMES"),
    ("RAIL_TIME_WINDOW", "KTXA_TIME_WINDOW"),
    ("RAIL_PASSENGERS", "KTXA_PASSENGERS"),
    ("RAIL_PASSENGER_TYPE", "KTXA_PASSENGER_TYPE"),
    ("RAIL_SEAT_CLASS", "KTXA_SEAT_CLASS"),
    ("RAIL_TOLERANCE_MIN", "KTXA_TOLERANCE_MIN"),
)


def _apply_rail_fallback(data: dict) -> None:
    """RAIL_* 공통 키가 있고 KTXA_* 가 비어 있으면 RAIL 값으로 채운다."""
    for shared, specific in _RAIL_SHARED_MAP:
        if not (data.get(specific) or "").strip() and (data.get(shared) or "").strip():
            data[specific] = data[shared]


def load_config() -> KTXAConfig:
    _load_env_files()
    data = {k: os.environ.get(k, "") for k in os.environ}
    _apply_rail_fallback(data)
    try:
        return KTXAConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(str(e)) from e


__all__ = ["KTXAConfig", "ConfigError", "load_config"]
