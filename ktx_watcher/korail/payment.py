"""Korail SPA 결제 흐름 — CDP-only.

reserve.attempt_reservation 후 `/ticket/payment/payment` 페이지 도달 상태에서 호출.

흐름:
  1. 결제 페이지 도달 확인 (URL `/ticket/payment/payment`)
  2. '카드결제' 탭 클릭 (default 가 다른 탭일 수 있음)
  3. 카드정보 입력 (cardNo1~4, cardMonth, cardYear, hidAthnVal, hidVanPwd)
  4. 동의 체크 (#check)
  5. '결제/발권' 버튼 클릭
  6. popup polling (이용안내/완료 안내 자동 dismiss)
  7. 완료 키워드 확인

**필드 selector** — VERIFIED 2026-05-14 (CDP DOM probe on /ticket/payment/payment):
  - input[name='cardNo1'], cardNo2, cardNo3 (password), cardNo4  (각 4자리, maxlength=4)
  - input[name='cardMonth'] (id=mon03, MM 2자리)
  - cardYear 는 input 또는 select. 양쪽 fallback 시도.
  - input[name='hidAthnVal'] (id=certi_num, 인증번호 6자리, password)
  - input[name='hidVanPwd'] (카드 비밀번호 앞 2자리, password)
  - input#check (동의 체크박스)
  - button:has-text('결제/발권') (cls=btn_bn-depblue)
"""

from __future__ import annotations

import logging
import random
import time as _time
from datetime import date
from typing import Any, Dict

from playwright.sync_api import Page, TimeoutError as PWTimeoutError

from ..config import KTXAConfig
from . import CaptchaDetected, SiteLayoutChanged, UserActionRequired
from .client import (
    KorailSPAClient,
    dismiss_all_popups,
    human_click,
    human_pause,
    human_type,
)

LOGGER = logging.getLogger("ktx_watcher_spa.korail.payment")


# ─────────────────── input parsing ───────────────────

def _digits_only(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _parse_card(config: KTXAConfig) -> Dict[str, str]:
    """config 의 카드 정보를 검증 + 파싱. 4등분/MM/YY2/YYYY/pw2/id6."""
    raw = (config.pay_card_num or "").replace(" ", "")
    c1 = c2 = c3 = c4 = ""
    if raw:
        parts = [p for p in raw.split("-") if p]
        if len(parts) == 4 and all(len(p) == 4 and p.isdigit() for p in parts):
            c1, c2, c3, c4 = parts
        else:
            digits = _digits_only(raw)
            if len(digits) == 16:
                c1, c2, c3, c4 = digits[0:4], digits[4:8], digits[8:12], digits[12:16]

    mm = _digits_only(config.pay_card_mm)
    yyyy = _digits_only(config.pay_card_yy)
    pw2 = _digits_only(config.pay_card_pw2)
    id6 = _digits_only(config.pay_id6)

    errs = []
    for i, seg in enumerate([c1, c2, c3, c4], 1):
        if len(seg) != 4 or not seg.isdigit():
            errs.append(f"카드 {i}번째 4자리 필요")
    try:
        if not (1 <= int(mm) <= 12):
            errs.append("PAY_CARD_MM 01..12")
    except Exception:
        errs.append("PAY_CARD_MM 형식")
    mm = f"{int(mm):02d}" if mm else mm
    this_year = date.today().year
    yy_min, yy_max = this_year - 1, this_year + 15  # 카드 유효기간 상식 범위, 해마다 갱신 불필요
    if not (len(yyyy) == 4 and yy_min <= int(yyyy) <= yy_max):
        errs.append(f"PAY_CARD_YY {yy_min}..{yy_max} 4자리")
    if not (len(pw2) == 2 and pw2.isdigit()):
        errs.append("PAY_CARD_PW2 2자리")
    if not (len(id6) == 6 and id6.isdigit()):
        errs.append("PAY_ID6 6자리(YYMMDD)")

    if errs:
        raise ValueError("결제 config 오류: " + ", ".join(errs))

    return {
        "c1": c1, "c2": c2, "c3": c3, "c4": c4,
        "mm": mm, "yyyy": yyyy, "yy2": yyyy[-2:],
        "pw2": pw2, "id6": id6,
    }


# ─────────────────── helpers ───────────────────

def _is_payment_page(page: Page) -> bool:
    url = page.url or ""
    return "/ticket/payment/payment" in url


def _click_card_payment_tab(page: Page) -> None:
    """결제수단 선택 탭에서 '카드결제' 가 active 가 아니면 클릭."""
    btn = page.locator("button:has-text('카드결제')").first
    if btn.count() == 0:
        LOGGER.warning("'카드결제' 탭 미발견 — default 가 카드결제 아닐 수 있음")
        return
    try:
        cls = (btn.get_attribute("class", timeout=500) or "").lower()
        if "btnon" in cls or "active" in cls or "on" in cls.split():
            LOGGER.info("카드결제 탭 이미 active")
            return
    except Exception:
        pass
    try:
        human_click(btn)
        LOGGER.info("'카드결제' 탭 클릭")
        human_pause(0.8, 1.4)
    except PWTimeoutError as e:
        raise SiteLayoutChanged(f"카드결제 탭 클릭 실패: {e}") from e


def _fill_card_segment(page: Page, name: str, value: str) -> None:
    """입력 후 실제 DOM 값을 읽어 검증, 불일치면 지우고 재입력 (최대 3회).

    실측 2026-07-07: 검증 없이 타이핑만 하면 cardNo1 에 엉뚱한 값(회원번호 앞자리)이
    남아 결제가 거부된 사례 — 사이트 JS 간섭 여부와 무관하게 값 기준으로 확정한다.
    """
    loc = page.locator(f"input[name='{name}']").first
    if loc.count() == 0:
        raise SiteLayoutChanged(f"카드 입력 필드 미발견: name={name}")
    for attempt in range(1, 4):
        try:
            loc.click(timeout=3000)
        except Exception:
            pass
        _time.sleep(random.uniform(0.15, 0.4))
        try:
            if loc.input_value(timeout=1000):
                loc.fill("")
                _time.sleep(random.uniform(0.1, 0.25))
        except Exception:
            pass
        loc.press_sequentially(value, delay=random.randint(70, 140))
        _time.sleep(random.uniform(0.2, 0.5))
        try:
            cur = loc.input_value(timeout=1500)
        except Exception:
            return  # 값 판독 불가 필드 — 기존 동작대로 진행
        if cur == value:
            return
        LOGGER.warning("카드 필드 %s 값 불일치 (attempt=%d, len=%d) — 재입력", name, attempt, len(cur))
    raise UserActionRequired(f"카드 필드 {name} 입력이 3회 모두 불일치 — 수동 확인 필요")


def _set_card_year(page: Page, data: Dict[str, str]) -> None:
    """cardYear 가 input 인지 select 인지 케이스 모두 처리."""
    sel = page.locator("select[name='cardYear']").first
    if sel.count() > 0:
        # select: value 가 '27' (2자리) 또는 '2027' 둘 다 시도
        for val in (data["yy2"], data["yyyy"]):
            try:
                sel.select_option(value=val, timeout=2000)
                LOGGER.info("cardYear select_option=%s 적용", val)
                return
            except Exception:
                continue
        # label 기반 fallback
        try:
            sel.select_option(label=data["yyyy"], timeout=2000)
            LOGGER.info("cardYear label=%s 적용", data["yyyy"])
            return
        except Exception:
            pass
        raise SiteLayoutChanged(f"cardYear select 적용 실패 (yy2={data['yy2']})")

    inp = page.locator("input[name='cardYear']").first
    if inp.count() > 0:
        try:
            inp.click(timeout=3000)
        except Exception:
            pass
        # maxlength 확인 — 2 면 yy2, 4 면 yyyy
        try:
            ml = inp.evaluate("el => el.maxLength")
        except Exception:
            ml = 2
        val = data["yyyy"] if ml == 4 else data["yy2"]
        inp.press_sequentially(val, delay=random.randint(80, 150))
        LOGGER.info("cardYear input fill=%s", val)
        return

    raise SiteLayoutChanged("cardYear 필드 미발견 (select/input 모두 없음)")


# ─────────────────── main API ───────────────────

def perform_payment(
    client: KorailSPAClient,
    config: KTXAConfig,
) -> None:
    """결제 페이지 도달 상태에서 카드 정보 입력 + 결제/발권 클릭까지.

    config.ktxa_payment_mode 가 False 면 즉시 return (no-op).
    """
    if not config.ktxa_payment_mode:
        LOGGER.info("KTXA_PAYMENT_MODE=false — 결제 단계 skip")
        return

    page = client.main_page()

    # 0) 결제 페이지 진입 대기 (reserve 가 /reservation/detail 까지 갔으면
    #    '결제하기' 클릭으로 /payment/payment 로 이동해야 함)
    if not _is_payment_page(page):
        # /reservation/detail 에서 '결제하기' 버튼 클릭
        bt = page.locator("button:has-text('결제하기'), a:has-text('결제하기')").first
        if bt.count() == 0:
            raise SiteLayoutChanged(
                f"결제 페이지 미진입 + '결제하기' 버튼 없음 (url={page.url})"
            )
        try:
            human_click(bt)
            LOGGER.info("'결제하기' 클릭 → 결제 페이지로 이동")
        except PWTimeoutError as e:
            raise SiteLayoutChanged(f"결제하기 클릭 실패: {e}") from e

        # popup polling + URL 변화 대기
        for _ in range(8):
            _time.sleep(1.0)
            dismiss_all_popups(client.context)
            if _is_payment_page(page):
                break
        if not _is_payment_page(page):
            raise SiteLayoutChanged(f"결제 페이지로 이동 실패 (url={page.url})")

    LOGGER.info("결제 페이지 진입 OK: %s", page.url)

    # 1) 카드 정보 검증
    data = _parse_card(config)
    LOGGER.info(
        "결제 진행: ****-****-****-%s MM/YYYY=%s/%s",
        data["c4"], data["mm"], data["yyyy"],
    )

    # 2) 카드결제 탭 보장
    _click_card_payment_tab(page)
    dismiss_all_popups(client.context)
    human_pause(0.6, 1.2)

    # 3) 카드번호 4등분
    _fill_card_segment(page, "cardNo1", data["c1"])
    _fill_card_segment(page, "cardNo2", data["c2"])
    _fill_card_segment(page, "cardNo3", data["c3"])  # password type
    _fill_card_segment(page, "cardNo4", data["c4"])
    human_pause(0.5, 1.0)

    # 4) 유효기간
    _fill_card_segment(page, "cardMonth", data["mm"])
    _set_card_year(page, data)
    human_pause(0.5, 1.0)

    # 5) 인증번호 (YYMMDD) + 카드 비밀번호 앞 2자리
    _fill_card_segment(page, "hidAthnVal", data["id6"])
    _fill_card_segment(page, "hidVanPwd", data["pw2"])
    human_pause(0.5, 1.0)

    # 6) 동의 체크 (#check). 라벨이 intercept 하므로 label[for='check'] 클릭 → 실패 시 JS 강제.
    try:
        chk = page.locator("input#check").first
        if chk.count() > 0 and not chk.is_checked():
            try:
                page.locator("label[for='check']").first.click(timeout=3000)
                LOGGER.info("동의 체크 (label[for='check'] 클릭)")
            except Exception as e_lbl:
                LOGGER.warning("label 클릭 실패: %s — JS 강제 토글", e_lbl)
                page.evaluate(
                    """() => {
                        const el = document.querySelector('input#check');
                        if (el) {
                            el.checked = true;
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        }
                    }"""
                )
            human_pause(0.3, 0.7)
            if not chk.is_checked():
                LOGGER.warning("동의 체크 적용 실패 — 결제 거부될 수 있음")
    except Exception as e:
        LOGGER.warning("동의 체크 처리 중 예외(계속 진행): %s", e)

    dismiss_all_popups(client.context)

    # 6.5) 클릭 직전 카드번호 4등분 최종 검증 — 입력 후 다른 값으로 바뀌어 있으면 재입력
    for seg_name, seg_val in (("cardNo1", data["c1"]), ("cardNo2", data["c2"]),
                              ("cardNo3", data["c3"]), ("cardNo4", data["c4"]),
                              ("cardMonth", data["mm"])):
        try:
            cur = page.locator(f"input[name='{seg_name}']").first.input_value(timeout=1000)
        except Exception:
            continue
        if cur != seg_val:
            LOGGER.warning("최종 검증: %s 값 뒤바뀜(len=%d) — 재입력", seg_name, len(cur))
            _fill_card_segment(page, seg_name, seg_val)
    human_pause(0.3, 0.7)

    # 7) 결제/발권 클릭
    pay_btn = page.locator("button:has-text('결제/발권')").first
    if pay_btn.count() == 0:
        raise SiteLayoutChanged("'결제/발권' 버튼 미발견")
    try:
        human_click(pay_btn)
        LOGGER.info("'결제/발권' 클릭 — 결제 진행 중")
    except PWTimeoutError as e:
        raise SiteLayoutChanged(f"결제/발권 클릭 실패: {e}") from e

    # 8) popup polling — 결제 진행 중 안내 모달 / 완료 안내 자동 dismiss
    for poll_i in range(15):
        _time.sleep(1.0)
        dismissed = dismiss_all_popups(client.context)
        if dismissed:
            LOGGER.info("결제 단계 popup dismiss iter=%d count=%d", poll_i, dismissed)

    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass
    human_pause(1.5, 2.5)

    # 9) 결제 완료 확인 — URL 변화가 가장 명확한 신호.
    #   성공: /payment/payment → /myticket/list (승차권 확인) 또는 /payment/complete
    #   실패/미진행: /payment/payment 그대로
    url = page.url or ""
    if any(p in url for p in ("/myticket/list", "/payment/complete", "/ticket/myticket")):
        LOGGER.info("✅✅ 결제/발권 완료 (url=%s)", url)
        return

    # URL 그대로 → 결제 안 됨. body 텍스트로 실패 사유 추정.
    try:
        html = page.content()
    except Exception:
        html = ""

    fail_kw = (
        "결제 실패", "결제실패",
        "유효기간",
        "비밀번호",
        "인증번호 오류",
        "카드번호",
        "동의",
    )
    detected = [kw for kw in fail_kw if kw in html]
    if detected:
        raise UserActionRequired(
            f"결제 미완료 — 키워드 감지: {detected} (url={url})"
        )

    raise UserActionRequired(
        f"결제 완료 URL 변화 안 됨 — url 그대로 ({url}). 동의/카드정보/네트워크 확인 필요."
    )


__all__ = ["perform_payment"]
