"""Korail SPA search flow — CDP-only, simple.

흐름:
  1. navigate /ticket/search/general
  2. 폼 입력 (역 / 날짜 / 인원)
  3. 검색 버튼 클릭
  4. 결과 페이지 진입 대기
  5. -8002 모달 dismiss (있으면)
  6. KTX 탭 클릭 (옵션)
  7. row 파싱

매크로 가드 처리 정책 (사용자 결정 2026-05-14):
  - 빈 결과 + dismiss 했으면 그냥 return [] → main polling 이 다음 iteration 에서
    재검색. 같은 iteration 안에서의 자동 재시도는 안 한다 (단순함을 위해).
"""

from __future__ import annotations

import logging
import re
import time as _time
from datetime import date as _date, time as Time
from typing import Any, Dict, List, Optional

from playwright.sync_api import Page, TimeoutError as PWTimeoutError

from ..config import KTXAConfig
from . import CaptchaDetected, SiteLayoutChanged
from . import selectors as S
from .client import (
    KorailSPAClient,
    dismiss_macro_notice,
    dismiss_notice_modal,
    human_click,
    human_mouse,
    human_pause,
    human_type,
    safe_goto,
)

LOGGER = logging.getLogger("ktx_watcher_spa.korail.search")

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


# ─────────────────── form helpers ───────────────────

def _input_value(page: Page, sel: str) -> str:
    try:
        return (page.locator(sel).first.input_value(timeout=1500) or "").strip()
    except Exception:
        return ""


def _select_station(page: Page, slot: str, station: str) -> None:
    """slot='origin'|'dest'. 이미 입력돼 있으면 skip."""
    input_sel = S.ORIGIN_INPUT if slot == "origin" else S.DEST_INPUT
    if _input_value(page, input_sel) == station.strip():
        LOGGER.info("역 (%s) 이미 %r — skip", slot, station)
        return

    open_sel = S.ORIGIN_BTN if slot == "origin" else S.DEST_BTN
    LOGGER.info("역 선택: slot=%s name=%s", slot, station)
    human_mouse(page, moves=2)
    human_click(page.locator(open_sel).first)

    try:
        page.wait_for_selector(S.STATION_POPUP, timeout=8000)
    except PWTimeoutError:
        raise SiteLayoutChanged(f"역 선택 팝업 안 뜸 (slot={slot})")
    human_pause(0.4, 0.9)

    # 검색창에 입력 후 정확매칭 클릭
    try:
        inp = page.locator(S.STATION_POPUP_INPUT).first
        if inp.count() > 0:
            inp.fill(station, timeout=3000)
            human_pause(0.3, 0.7)
    except Exception as e:
        LOGGER.debug("역명 검색어 입력 skip: %s", e)

    try:
        opt = page.locator(S.STATION_POPUP_OPTION).get_by_text(station, exact=True).first
        if opt.count() == 0:
            raise SiteLayoutChanged(f"역 항목 미발견: {station}")
        human_click(opt)
        try:
            page.wait_for_selector(S.STATION_POPUP, state="hidden", timeout=4000)
        except Exception:
            pass
        human_pause(0.4, 0.9)
    except SiteLayoutChanged:
        raise
    except Exception as e:
        raise SiteLayoutChanged(f"역 항목 클릭 실패 ({station}): {e}") from e

    # 검증
    if _input_value(page, input_sel) != station.strip():
        raise SiteLayoutChanged(f"역 적용 검증 실패: expected={station!r}")


def _read_current_ym(page: Page) -> Optional[tuple[int, int]]:
    """현재 visible(slick-active) 슬라이드의 datepicker label 에서 YYYY-MM 추출.
    label 형식: "2026. 06." (점 구분) 또는 "2026년 06월" 둘 다 매칭."""
    raw = page.evaluate(
        """() => {
            const root = document.querySelector('.layerWrap.type_date-pop_wrap');
            if (!root) return null;
            const re = /(20\\d{2})\\s*[.\\uB144]\\s*(\\d{1,2})/;  // YYYY [. 또는 년] MM
            // visible slide 안의 .datepicker .date 우선
            const active = root.querySelectorAll('.slick-active');
            for (const el of active) {
                const dp = el.querySelector('.datepicker .date') || el;
                const t = (dp.textContent || '').trim();
                const m = t.match(re);
                if (m) return {y: +m[1], mo: +m[2]};
            }
            // fallback: 모든 datepicker .date
            for (const dp of root.querySelectorAll('.datepicker .date')) {
                const t = (dp.textContent || '').trim();
                const m = t.match(re);
                if (m) return {y: +m[1], mo: +m[2]};
            }
            return null;
        }"""
    )
    if not raw:
        return None
    return int(raw["y"]), int(raw["mo"])


def _click_day(page: Page, day: int, year: Optional[int] = None, month: Optional[int] = None) -> bool:
    """day 셀 클릭. slick carousel 안에 여러 월 datepicker 가 mount 돼 있어도
    year/month 가 주어지면 그 월 헤더 가진 카드 안의 day 만 클릭."""
    return bool(page.evaluate(
        """([d, y, mo]) => {
            const wrap = document.querySelector('.layerWrap.type_date-pop_wrap');
            if (!wrap) return false;
            // 후보 카드: slick-slide, datepk_wrap, 또는 wrap 전체 (단일 datepicker 케이스)
            const cards = [...wrap.querySelectorAll('.slick-slide'), ...wrap.querySelectorAll('.datepk_wrap')];
            const search = (root) => {
                if (y && mo) {
                    const headerEl = root.querySelector('.datepicker .date') || root;
                    const t = (headerEl.textContent || '').trim();
                    const m = t.match(/(20\\d{2})\\s*[.\\uB144]\\s*(\\d{1,2})/);
                    if (!m || +m[1] !== y || +m[2] !== mo) return false;
                }
                const dp = root.querySelector('.datepicker') || root;
                const tbody = dp.querySelector('tbody');
                if (!tbody) return false;
                for (const td of tbody.querySelectorAll('td')) {
                    if (td.classList.contains('disabled')) continue;
                    const a = td.querySelector('a');
                    if (!a || a.getAttribute('aria-disabled') === 'true') continue;
                    const span = a.querySelector('.day');
                    const txt = span ? span.textContent.trim() : a.textContent.trim();
                    if (txt === String(d)) { a.click(); return true; }
                }
                return false;
            };
            for (const c of cards) { if (search(c)) return true; }
            // fallback: month 헤더 검사 없이 wrap 전체에서 첫 매칭
            return search(wrap);
        }""",
        [day, year, month],
    ))


def _click_hour(page: Page, hour: int) -> bool:
    """시각 선택.

    NOTE (2026-09-15 실측): timeSelect 앵커의 aria-disabled="true" 는 실제
    클릭 가능 여부와 무관했다 — aria-disabled=true 인 앵커를 target.click() 해도
    "XX시 이후 출발" 로 정상 반영됨 (슬릭 캐러셀의 접근성/포커스 관리용 속성일
    뿐, 진짜 비활성 상태가 아니었다). 예전엔 이걸 클릭 가능 여부로 오판해서
    멀쩡한 시각도 스크롤-폴백으로 흘려보내다 결국 요청과 무관한 00시에
    정착하는 버그가 있었다 (그래서 매 검색이 00시 기준으로 진행되고
    실제로 감시하려던 시간대 열차는 후보에 아예 안 잡혔다). 24개 시각이
    전부 DOM 에 이미 존재하므로 캐러셀 스크롤 없이 텍스트로 찾아 바로 클릭한다.
    """
    texts = [f"{hour}시", f"{hour:02d}시"]
    result = page.evaluate(
        """(texts) => {
            const root = document.querySelector('.layerWrap.type_date-pop_wrap .timeSelect');
            if (!root) return 'absent';
            for (const a of root.querySelectorAll('a')) {
                if (texts.includes((a.textContent || '').trim())) {
                    a.click();
                    return 'clicked';
                }
            }
            return 'absent';
        }""",
        texts,
    )
    return result == "clicked"


def _set_date(page: Page, target: _date, hour: int) -> None:
    # picker 클릭이 공지 모달 (ReactModalPortal) 에 가로채일 수 있어 매번 dismiss 시도
    dismiss_notice_modal(page)
    # 이미 같은 값이면 picker 안 열음 (반복 자동화 시그널 회피).
    cur = _input_value(page, S.DATE_INPUT)
    want_prefix = target.isoformat()
    want_hour = f"{hour:02d}:00"
    if cur.startswith(want_prefix) and want_hour in cur:
        LOGGER.info("출발일 이미 일치 — picker skip (%s)", cur)
        return

    LOGGER.info("출발일 설정: %s %02d시 (현재=%s)", target.isoformat(), hour, cur)
    human_click(page.locator(S.DATE_PICKER_BTN).first)
    try:
        page.wait_for_selector(S.DATE_POPUP, timeout=8000)
    except PWTimeoutError:
        raise SiteLayoutChanged("date picker 팝업 안 뜸")
    human_pause(0.5, 0.9)

    # 월 정렬
    target_ym = (target.year, target.month)
    aligned = False
    for _ in range(12):
        cur = _read_current_ym(page)
        if cur is None:
            break  # 헤더 추출 실패 → 일 매칭으로 폴백
        if cur == target_ym:
            aligned = True
            break
        try:
            if cur < target_ym:
                page.locator(S.DATE_POPUP_NEXT).first.click(timeout=2000)
            else:
                page.locator(S.DATE_POPUP_PREV).first.click(timeout=2000)
            human_pause(0.3, 0.6)
        except Exception:
            break

    if not _click_day(page, target.day, target.year, target.month):
        # 진단: picker DOM 일부 dump
        try:
            dump = page.evaluate(
                """() => {
                    const w = document.querySelector('.layerWrap.type_date-pop_wrap');
                    if (!w) return 'WRAP_NULL';
                    const dps = [...w.querySelectorAll('.datepicker')];
                    const summary = dps.map(dp => {
                        const dateLabel = (dp.querySelector('.date')?.textContent || '').trim();
                        const tds = [...dp.querySelectorAll('tbody td')];
                        const td_cls_sample = tds.slice(0, 8).map(t => t.className || '');
                        const a_sample = tds.slice(0, 8).map(t => {
                            const a = t.querySelector('a');
                            if (!a) return 'NOA';
                            return JSON.stringify({txt: (a.textContent||'').trim(), ariaDis: a.getAttribute('aria-disabled'), cls: a.className || ''});
                        });
                        // find day=1 td across this dp
                        let day1_info = 'NONE';
                        for (const td of tds) {
                            const a = td.querySelector('a');
                            const txt = (a?.textContent || td.textContent || '').trim();
                            if (txt === '1') {
                                day1_info = JSON.stringify({
                                    td_cls: td.className || '',
                                    a_present: !!a,
                                    a_aria: a ? a.getAttribute('aria-disabled') : null,
                                    a_cls: a ? (a.className || '') : null,
                                });
                                break;
                            }
                        }
                        return { label: dateLabel, td_count: tds.length, td_cls_sample, a_sample, day1_info };
                    });
                    return JSON.stringify(summary, null, 0).slice(0, 4000);
                }"""
            )
        except Exception as e:
            dump = f"DUMP_ERR:{e}"
        LOGGER.warning("date picker %d일 미발견 — DOM dump: %s", target.day, dump)
        try:
            page.locator(S.DATE_POPUP_CANCEL).first.click(timeout=1500)
        except Exception:
            pass
        raise SiteLayoutChanged(f"date picker {target.day}일 셀 미발견")
    human_pause(0.3, 0.6)

    if not _click_hour(page, hour):
        # 가용한 다음 시각 fallback (오늘 날짜는 사이트가 현재 시각 이후만 제공 —
        # 2026-08-19 CDP 실측: timeSelect 에 현재시~23시만 렌더링됨)
        for h in list(range(hour + 1, 24)) + list(range(0, hour)):
            if _click_hour(page, h):
                LOGGER.warning("요청 시각 %02d시 선택 불가 — 대체 시각 적용: %02d시", hour, h)
                break
        else:
            LOGGER.warning("시각 선택 전부 실패 (%02d시 요청) — 사이트 기본값으로 진행", hour)
    human_pause(0.3, 0.6)

    # 안내 모달(ReactModal)이 뒤늦게 떠서 적용 버튼 클릭을 가로챌 수 있음
    dismiss_notice_modal(page)
    try:
        page.locator(S.DATE_POPUP_APPLY).first.click(timeout=4000)
    except Exception as e:
        raise SiteLayoutChanged(f"date picker 적용 실패: {e}") from e
    human_pause(0.5, 1.0)

    # 검증
    cur = _input_value(page, S.DATE_INPUT)
    if not cur.startswith(target.isoformat()):
        raise SiteLayoutChanged(f"date input 검증 실패: 현재={cur!r}")
    LOGGER.info("출발일 적용 OK: %s", cur)


def _read_passenger_counts(page: Page) -> Optional[Dict[str, int]]:
    """열린 인원선택 팝업의 {li 라벨: 현재 인원}. 팝업 미발견 시 None."""
    return page.evaluate(
        """() => {
            const root = document.querySelector('.layerWrap.personnel_pop_wrap');
            if (!root) return null;
            const out = {};
            for (const li of root.querySelectorAll('.ticketInfo_bottom li')) {
                const label = (li.querySelector('p')?.textContent || '').trim();
                const span = li.querySelector('.flo_right span');
                if (label && span) out[label] = parseInt(span.textContent.trim(), 10) || 0;
            }
            return out;
        }"""
    )


def _click_passenger_step(page: Page, label: str, up: bool) -> bool:
    """라벨(prefix 매칭) li 의 증가/감소 버튼 1회 클릭. disabled/미발견이면 False."""
    return bool(page.evaluate(
        """([label, up]) => {
            const root = document.querySelector('.layerWrap.personnel_pop_wrap');
            if (!root) return false;
            for (const li of root.querySelectorAll('.ticketInfo_bottom li')) {
                const t = (li.querySelector('p')?.textContent || '').trim();
                if (!t.startsWith(label)) continue;
                const btn = li.querySelector('.flo_right button.' + (up ? 'up_num' : 'down_num'));
                if (!btn || btn.disabled) return false;
                btn.click();
                return true;
            }
            return false;
        }""",
        [label, up],
    ))


def _set_passengers(page: Page, config: KTXAConfig) -> None:
    """인원선택 팝업에서 유형별 인원을 config 값에 맞춘다 (예: 경로 1명).

    폼의 '총 N명' 라벨은 합계만 보여서 구성(어른/경로 등)은 팝업을 열어야만
    확인 가능 → 폼 진입 시 1회 열어 검증, 이미 일치하면 취소로 닫는다.
    """
    desired_by_type = config.passenger_counts()

    def want(label: str) -> int:
        for t, n in desired_by_type.items():
            if label.startswith(S.PASSENGER_TYPE_PREFIX[t]):
                return n
        return 0

    dismiss_notice_modal(page)
    LOGGER.info("인원 확인: %s", desired_by_type)
    human_click(page.locator(S.PEOPLE_BTN).first)
    try:
        page.wait_for_selector(S.PEOPLE_POPUP, timeout=8000)
    except PWTimeoutError:
        raise SiteLayoutChanged("인원선택 팝업 안 뜸")
    human_pause(0.4, 0.8)

    cur = _read_passenger_counts(page)
    if not cur:
        raise SiteLayoutChanged("인원선택 팝업 카운트 파싱 실패")

    if all(cnt == want(label) for label, cnt in cur.items()):
        LOGGER.info("인원 이미 일치 — 팝업 취소로 닫음")
        try:
            page.locator(S.PEOPLE_POPUP_CANCEL).first.click(timeout=3000)
        except Exception:
            pass
        human_pause(0.3, 0.6)
        return

    LOGGER.info("인원 조정: 현재=%s → 목표=%s", cur, desired_by_type)
    # 증가 먼저 → 감소: 총원이 0명이 되는 순간(감소 버튼 disabled)을 피한다.
    for up in (True, False):
        for label, cnt in (_read_passenger_counts(page) or {}).items():
            delta = want(label) - cnt
            if (delta > 0) is not up or delta == 0:
                continue
            for _ in range(abs(delta)):
                if not _click_passenger_step(page, label, up):
                    raise SiteLayoutChanged(
                        f"인원 {'증가' if up else '감소'} 버튼 클릭 실패: {label}"
                    )
                human_pause(0.15, 0.4)

    cur = _read_passenger_counts(page) or {}
    bad = {l: c for l, c in cur.items() if c != want(l)}
    if bad:
        raise SiteLayoutChanged(f"인원 적용 전 검증 실패: {bad}")
    human_pause(0.3, 0.6)

    try:
        page.locator(S.PEOPLE_POPUP_APPLY).first.click(timeout=4000)
    except Exception as e:
        raise SiteLayoutChanged(f"인원 팝업 적용 실패: {e}") from e
    # 비어른 구성이면 "선택하신 인원이 확실한가요?" 확인 모달이 한 번 더 뜬다 → 예
    try:
        yes = page.locator(S.PEOPLE_CONFIRM_YES).first
        yes.wait_for(state="visible", timeout=3000)
        human_pause(0.3, 0.7)
        yes.click(timeout=3000)
        LOGGER.info("인원 확인 모달 '예' 클릭")
    except Exception:
        pass  # 어른 구성 등 모달이 없으면 그대로 진행
    try:
        page.wait_for_selector(S.PEOPLE_POPUP, state="hidden", timeout=4000)
    except Exception:
        pass
    human_pause(0.3, 0.7)

    total = sum(desired_by_type.values())
    try:
        label_txt = (page.locator(S.PEOPLE_BTN).first.inner_text(timeout=1500) or "").strip()
    except Exception:
        label_txt = ""
    if label_txt and str(total) not in label_txt:
        # 유아는 '총 N명' 합계에서 빠질 수 있어 경고만.
        LOGGER.warning("인원 라벨 불일치(계속 진행): 기대 총 %d명, 라벨=%r", total, label_txt)
    LOGGER.info("인원 적용 OK: %s (라벨: %s)", desired_by_type, label_txt or "?")


# ─────────────────── 결과 처리 ───────────────────

def _click_train_type_tab(page: Page, train_type: str) -> None:
    if not train_type or train_type in ("전체", "ALL"):
        return
    sel = S.TRAIN_TYPE_TAB.get(train_type)
    if not sel:
        LOGGER.warning("열차종류 '%s' 미지원", train_type)
        return
    loc = page.locator(sel).first
    if loc.count() == 0:
        LOGGER.warning("열차종류 탭 '%s' DOM 미발견", train_type)
        return
    try:
        cls = (loc.get_attribute("class", timeout=500) or "").lower()
        aria = (loc.get_attribute("aria-selected", timeout=500) or "").lower()
        if "active" in cls or "current" in cls or aria == "true":
            LOGGER.info("열차종류 탭 '%s' 이미 active", train_type)
            return
    except Exception:
        pass
    # 클릭 전 modal dismiss (delayed -8002 우회)
    for attempt in range(3):
        dismiss_macro_notice(page)
        try:
            loc.click(timeout=4000)
            LOGGER.info("열차종류 탭 '%s' 클릭 (attempt=%d)", train_type, attempt + 1)
            human_pause(0.8, 1.4)
            return
        except Exception as e:
            LOGGER.warning("열차종류 탭 클릭 attempt=%d 실패: %s", attempt + 1, e)
            _time.sleep(0.8)


def _parse_depart_time(text: str) -> Optional[Time]:
    m = _TIME_RE.search(text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h < 24 and 0 <= mi < 60:
        return Time(hour=h, minute=mi)
    return None


def _parse_result_rows(page: Page) -> List[Dict[str, Any]]:
    """결과 row 를 JS-side scan 으로 추출.

    DOM 구조 (CDP probe 2026-05-14):
      <li class="tckList ...">
        <div class="price_box gen [sold_out_soon|sold_out]">  ← 일반실
        <div class="price_box [sold_out_soon|sold_out]">      ← 특실
      </li>
    매진된 등급은 "특실" 라벨이 dom 에서 빠지므로 text-split 으로는 분리 불가.
    → class 로 직접 판별. sold_out (sold_out_soon 제외) = 매진. sold_out_soon = 매진임박(예약 가능).
    """
    raw = page.evaluate(
        """() => {
            const out = [];
            const containers = document.querySelectorAll('li.tckList');
            containers.forEach((el, i) => {
                const t = (el.textContent || '').trim();
                if (!/\\d{1,2}:\\d{2}/.test(t)) return;
                if (!/KTX|새마을|무궁화|ITX|누리로|청룡|SRT/i.test(t)) return;

                const parseBox = (box) => {
                    if (!box) return null;
                    const cls = box.className || '';
                    const txt = (box.textContent || '').trim();
                    const soldOutSoon = /sold_out_soon/.test(cls);
                    // soldOut: class 에 'sold_out' 있고 'sold_out_soon' 아님 (sold_out_wait 는 텍스트 기반으로 판정)
                    const soldOut = /\\bsold_out\\b/.test(cls) && !soldOutSoon;
                    const hasPrice = /\\d+,\\d+원/.test(txt);
                    return {
                        cls: cls,
                        text: txt,
                        sold_out: soldOut,
                        imminent: soldOutSoon,
                        has_price: hasPrice,
                    };
                };
                // priceBox 위치 기반: 좌측(첫 번째) = 일반실, 우측(두 번째) = 특실.
                // class 'gen' 은 예약 가능 row 에만 붙고 매진/wait/sold_out_wait row 에는 빠짐.
                const allBoxes = Array.from(el.querySelectorAll('.price_box'));
                let genBox = allBoxes.find(b => b.classList.contains('gen')) || allBoxes[0] || null;
                let specBox = null;
                for (const b of allBoxes) {
                    if (b !== genBox) { specBox = b; break; }
                }
                out.push({
                    text: t,
                    gen: parseBox(genBox),
                    spec: parseBox(specBox),
                });
            });
            return out;
        }"""
    )

    rows: List[Dict[str, Any]] = []
    for i, item in enumerate(raw or []):
        text = item.get("text", "")
        t = _parse_depart_time(text)
        if not t:
            continue
        name_m = re.search(r"(KTX[-\w]*|SRT|새마을[\w]*|무궁화[\w]*|ITX[-\w]*|누리로|청룡)", text)
        train_name = name_m.group(1) if name_m else ""

        rows.append({
            "_row_index": i,
            "depart_raw": text,
            "depart": t.strftime("%H:%M"),
            "depart_time": t,
            "train_name": train_name,
            "gen": item.get("gen"),    # {cls, text, sold_out, imminent, has_price} or None
            "spec": item.get("spec"),  # same or None
            # 호환: 구 filter 코드가 사용하는 string status (status_window 검사용)
            "general_status": (item.get("gen") or {}).get("text", ""),
            "first_status": (item.get("spec") or {}).get("text", ""),
        })
    return rows


# ─────────────────── high-level API ───────────────────

def navigate_to_search(client: KorailSPAClient) -> Page:
    page = client.main_page()
    safe_goto(page, S.SEARCH_URL, timeout_ms=30_000)
    try:
        page.wait_for_load_state("networkidle", timeout=12_000)
    except PWTimeoutError:
        LOGGER.debug("networkidle timeout — 계속")
    try:
        page.wait_for_selector(S.SEARCH_FORM_DETECT, timeout=10_000)
    except PWTimeoutError:
        raise SiteLayoutChanged("검색 폼 selector 미감지")
    # 진입 직후 매크로 안내 모달 있으면 dismiss
    dismiss_macro_notice(page)
    # 운영기간성 공지 모달은 React 비동기 mount — 최대 4초 polling.
    for _ in range(8):
        if dismiss_notice_modal(page):
            break
        try:
            page.wait_for_timeout(500)
        except Exception:
            break
    return page


def _set_srt_option(page: Page, config: KTXAConfig) -> None:
    """검색 폼의 '에스알티(SRT) 함께 보기' 라디오를 켠다 (KTXA_INCLUDE_SRT=true 일 때).

    라디오가 든 옵션 영역(.selectAreaWrap.inline)이 display:none 이라
    Playwright click 이 불가 → JS click (hidden 이어도 change 이벤트 발생, React 반영 확인됨).
    """
    if not config.ktxa_include_srt:
        return
    try:
        state = page.evaluate(
            """(sel) => {
                const y = document.querySelector(sel);
                if (!y) return 'missing';
                if (y.checked) return 'already';
                y.click();
                return y.checked ? 'set' : 'FAIL';
            }""",
            S.SRT_RADIO_Y,
        )
    except Exception as e:
        LOGGER.warning("SRT 함께 보기 설정 실패 (계속 진행): %s", e)
        return
    if state in ("missing", "FAIL"):
        LOGGER.warning("SRT 함께 보기 라디오 적용 실패: %s — site layout 변경 가능", state)
    else:
        LOGGER.info("SRT 함께 보기 ON (%s)", state)


def _ensure_srt_on_result(page: Page) -> None:
    """결과 페이지(search/list)의 SRT 체크박스가 풀려 있으면 다시 켠다 (클릭 시 자동 재조회)."""
    try:
        cb = page.locator(S.SRT_RESULT_CHECKBOX).first
        if cb.count() == 0 or cb.is_checked():
            return
        try:
            page.locator(S.SRT_RESULT_CHECKBOX_LABEL).first.click(timeout=3000)
        except Exception:
            page.evaluate("(sel) => document.querySelector(sel)?.click()", S.SRT_RESULT_CHECKBOX)
        human_pause(1.5, 2.5)
        LOGGER.info("결과 페이지 SRT 함께 보기 재활성화")
    except Exception as e:
        LOGGER.warning("결과 페이지 SRT 체크 확인 실패 (계속 진행): %s", e)


def fill_search_form(page: Page, config: KTXAConfig) -> None:
    hour = config.ktxa_times[0].hour if config.ktxa_times else 8
    for slot, station in (("origin", config.ktxa_origin), ("dest", config.ktxa_dest)):
        _select_station(page, slot, station)
        human_pause(1.0, 2.0)
    _set_date(page, config.ktxa_date, hour=hour)
    human_pause(1.0, 2.0)
    _set_passengers(page, config)
    human_pause(0.8, 1.4)


def submit_search(page: Page) -> List[Dict[str, Any]]:
    """검색 제출 → 결과 페이지 → row 파싱.

    매크로 안내(-8002/-8003) 가 뜨면 dismiss 후 빈 결과 return.
    재검색은 호출자(main polling) 책임.
    """
    human_mouse(page, moves=4)
    # 검색 제출 직전 사람-유사 대기 (자동화 즉시 제출 패턴 회피)
    human_pause(3.0, 6.0)
    human_click(page.locator(S.SEARCH_SUBMIT).first)

    try:
        page.wait_for_url(S.RESULT_URL_PATTERN, timeout=15_000)
    except PWTimeoutError:
        LOGGER.warning("SPA URL 라우팅 실패: %s", page.url)

    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PWTimeoutError:
        LOGGER.debug("결과 페이지 networkidle timeout")

    try:
        page.wait_for_selector(S.RESULT_PAGE_DETECT, timeout=10_000)
    except PWTimeoutError:
        LOGGER.warning("결과 페이지 detect selector 미감지")

    human_pause(1.5, 2.5)

    # -8002 안내 모달 / 팝업 처리 (popup 핸들러는 자동으로 동작, 메인 모달은 여기서)
    dismissed = dismiss_macro_notice(page)
    if dismissed:
        LOGGER.info("매크로 안내 dismiss 됨 — 이번 iteration 은 빈 결과로 반환")
        return []

    return _parse_result_rows(page)


def _expand_more_until(
    page: Page,
    config: KTXAConfig,
    raw: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """감시 대상 최댓 시각(+허용오차/시간창)까지 행이 로드되도록 '더보기' 로 확장.

    결과 페이지는 첫 화면에 일부 행만 주고 나머지는 '더보기' 로 이어 붙인다.
    KTXA_TIMES 가 오후·저녁까지 걸치면 확장 없이는 해당 후보가 아예 파싱되지 않는다.
    """
    if not raw or not config.ktxa_times:
        return raw
    latest = max(config.ktxa_times)
    limit_min = latest.hour * 60 + latest.minute + max(config.ktxa_tolerance_min, 0)
    if config.ktxa_time_window:
        wend = config.ktxa_time_window[1]
        limit_min = max(limit_min, wend.hour * 60 + wend.minute)
    limit_min = min(limit_min, 24 * 60 - 1)

    for _ in range(15):
        last = raw[-1].get("depart_time")
        if last and (last.hour * 60 + last.minute) >= limit_min:
            break
        try:
            btn = page.get_by_role("button", name="더보기").first
            if btn.count() == 0 or not btn.is_visible():
                break
            human_click(btn)
        except Exception as e:
            LOGGER.debug("더보기 확장 중단: %s", e)
            break
        human_pause(0.8, 1.4)
        try:
            raw = _parse_result_rows(page)
        except Exception as e:
            LOGGER.warning("더보기 확장 후 파싱 race: %s — 기존 행 유지", e)
            break
    return raw


def perform_search(
    client: KorailSPAClient,
    config: KTXAConfig,
) -> List[Dict[str, Any]]:
    # 이미 결과 페이지(/search/list)에 있으면 폼 재진입 없이 reload 만 (사람-유사 새로고침).
    page = client.main_page()
    cur_url = page.url or ""
    on_result = "/search/list" in cur_url
    hour = config.ktxa_times[0].hour if config.ktxa_times else 8
    if on_result:
        # 페이지가 설정과 다른 날짜를 보고 있으면 reload 반복은 무의미 — 폼 재진입.
        # (2026-08-19 실측: 시작 시 stale 설정으로 오늘 페이지가 잡히면 영원히 후보 0건)
        page_date = _input_value(page, S.DATE_INPUT)
        if page_date and not page_date.startswith(config.ktxa_date.isoformat()):
            LOGGER.warning(
                "결과 페이지 날짜 불일치 (페이지=%r, 설정=%s) — 검색 폼 재진입",
                page_date, config.ktxa_date.isoformat(),
            )
            on_result = False
    if on_result:
        LOGGER.info("결과 페이지에서 새로고침 (URL: %s)", cur_url)
        try:
            page.reload(wait_until="domcontentloaded", timeout=20_000)
        except PWTimeoutError:
            LOGGER.warning("reload domcontentloaded timeout — 계속")
        try:
            page.wait_for_load_state("networkidle", timeout=12_000)
        except PWTimeoutError:
            LOGGER.debug("reload networkidle timeout — 계속")
        human_pause(1.5, 2.5)
        dismissed = dismiss_macro_notice(page)
        if dismissed:
            LOGGER.info("매크로 안내 dismiss 됨 (reload) — 이번 iteration 빈 결과")
            return []
        if config.ktxa_include_srt:
            _ensure_srt_on_result(page)
        # reload 는 페이지를 처음 상태로 되돌려 출발 시각이 00:00 으로 리셋될 수 있다
        # (2026-09-15 실측: 날짜는 유지되지만 시각이 풀려서 이후 iteration 이 전부
        # 00시 기준 결과를 후보 매칭해 계속 0건이 나옴). _set_date 는 이미 원하는
        # 값이면 picker 를 열지 않고 바로 반환하므로 매 iteration 호출해도 저렴하다.
        try:
            _set_date(page, config.ktxa_date, hour=hour)
            human_pause(0.5, 1.0)
        except SiteLayoutChanged:
            raise
        except Exception as e:
            LOGGER.warning("reload 후 출발 시각 재확인 실패: %s — 계속 진행", e)
        try:
            raw = _parse_result_rows(page)
        except Exception as e:
            LOGGER.warning("reload 후 row 파싱 race: %s — 빈 결과로 반환", e)
            raw = []
    else:
        page = navigate_to_search(client)
        # 폼 입력은 값이 다를 때만 (반복 자동화 시그널 회피).
        # navigate_to_search 가 /search/general 로 갈 때 SPA 가 이전 form 값 유지함.
        if _input_value(page, S.ORIGIN_INPUT) != config.ktxa_origin.strip() \
                or _input_value(page, S.DEST_INPUT) != config.ktxa_dest.strip():
            fill_search_form(page, config)
        else:
            # 날짜만 따로 확인 (역은 같지만 날짜 다를 수 있음)
            _set_date(page, config.ktxa_date, hour=hour)
            human_pause(0.8, 1.4)
            # 인원 구성은 '총 N명' 라벨로 판별 불가 → 팝업 열어 확인
            _set_passengers(page, config)
            human_pause(0.8, 1.4)

        _set_srt_option(page, config)
        human_pause(0.5, 1.0)

        # 열차종류 필터 탭 — 검색 제출 *전* 옵션은 적용 안 함. 결과 페이지에서 클릭.
        raw = submit_search(page)

        if raw:
            # KTX 만 필터링 (탭 클릭)
            _click_train_type_tab(page, config.ktxa_train_type)
            human_pause(0.8, 1.4)
            try:
                raw = _parse_result_rows(page)
            except Exception as e:
                LOGGER.warning("KTX 탭 후 row 파싱 race: %s", e)
                raw = []

    # 감시 대상 최댓 시각까지 '더보기' 확장 (첫 화면엔 일부 행만 로드됨)
    raw = _expand_more_until(page, config, raw)

    # 사용자 선호 시간/좌석 매칭 후보로 압축
    # KTXA_SEAT_CLASS 가 비어 있으면 일반실+특실 둘 다 후보로 만든다 (ANY).
    seat_pref = (config.ktxa_seat_class or "").strip()
    want_general = (not seat_pref) or ("일반실" in seat_pref)
    want_special = (not seat_pref) or ("특실" in seat_pref)
    candidates: List[Dict[str, Any]] = []
    type_dropped = 0
    for r in raw:
        if not _is_candidate_time(r["depart_time"], config):
            continue
        # 열차종류 이중 방어 — 탭 클릭이 실패하면 '전체' 목록이 그대로 파싱된다
        # (2026-07-25 실측: KTXA_TRAIN_TYPE=KTX 인데 ITX-새마을 row 가 후보로 잡혀 예약 진행됨)
        if not _train_type_matches(r["train_name"], config.ktxa_train_type):
            type_dropped += 1
            continue
        # 실측 (2026-05-14): row 텍스트의 일반실/특실 직후 30자가 status.
        #   예약 가능: "23,700원5%적립..." (가격 표시)
        #   매진:     "23,700원5%적립매진"
        #   매진임박: "(매진임박)23,700원..." (예약 가능)
        for seat_label, status_col, enabled in (
            ("일반실", r.get("general_status", ""), want_general),
            ("특실", r.get("first_status", ""), want_special),
        ):
            if not enabled or not status_col:
                continue
            status_window = status_col[:40].replace(" ", "")
            without_imminent = status_window.replace("매진임박", "")
            has_price = ("원" in status_window) or ("예약하기" in status_window) or ("좌석선택" in status_window) or ("예매" in status_window)
            has_waitlist = "예약대기" in status_window
            has_standing = "입석" in status_window
            has_soldout = "매진" in without_imminent
            # status_kind 우선순위: 가격(예약 가능) > 예약대기 > 입석. 셋 다 없으면 skip.
            if has_price and not has_soldout:
                kind = "reserve"
            elif has_waitlist:
                kind = "waitlist"
            elif has_standing:
                kind = "standing"
            else:
                continue
            # SRT row 는 코레일 내 결제 불가 ('예매링크' → SRT 사이트 이동, 실측 2026-08-18)
            # → 알림 전용 후보로 표시. main 루프가 예약 시도 없이 Teams 알림만 보낸다.
            if r["train_name"].upper().startswith("SRT"):
                kind = "srt_link"
            # 좌석 직행만: 입석+좌석/예약대기 후보 제외 (srt_link 는 알림용이라 유지)
            if config.ktxa_seated_only and kind not in ("reserve", "srt_link"):
                continue
            candidates.append({
                "origin": config.ktxa_origin,
                "dest": config.ktxa_dest,
                "date": config.ktxa_date.isoformat(),
                "depart": r["depart"],
                "depart_time": r["depart_time"],
                "train_name": r["train_name"],
                "seat_class": seat_label,
                "status": status_col,
                "status_kind": kind,
                "_row_index": r["_row_index"],
                "_raw": r["depart_raw"],
            })
    candidates.sort(key=lambda c: c["depart_time"])
    if type_dropped:
        LOGGER.info("열차종류 불일치 %d건 제외 (KTXA_TRAIN_TYPE=%s)", type_dropped, config.ktxa_train_type)
    LOGGER.info("후보 %d건 (전체 row %d건)", len(candidates), len(raw))
    return candidates


# 열차종류 → row 열차명 매칭 키워드 (TRAIN_TYPE_TAB 의 탭 그룹과 동일 구성)
_TRAIN_TYPE_GROUPS = {
    "KTX": ("KTX", "청룡"),
    "KTX-산천": ("KTX", "청룡"),
    "새마을": ("새마을",),
    "ITX-새마을": ("새마을",),
    "무궁화": ("무궁화", "누리로"),
    "누리로": ("무궁화", "누리로"),
    "ITX-청춘": ("ITX-청춘",),
}


def _train_type_matches(train_name: str, train_type: str) -> bool:
    """탭 클릭 실패 대비 이중 방어 — row 열차명으로 열차종류 재검증."""
    if not train_type or train_type in ("전체", "ALL"):
        return True
    keys = _TRAIN_TYPE_GROUPS.get(train_type)
    if keys is None:
        return True  # 미지원 값은 탭 클릭 동작에 맡긴다
    if not train_name:
        return False  # 열차명 파싱 실패 row 는 오예약 방지 위해 제외
    return any(k in train_name for k in keys)


def _is_candidate_time(t: Time, config: KTXAConfig) -> bool:
    if config.ktxa_time_window:
        s, e = config.ktxa_time_window
        if not (s <= t <= e):
            return False
    if not config.ktxa_times:
        return True
    tol = config.ktxa_tolerance_min or 0
    target_minutes = [tt.hour * 60 + tt.minute for tt in config.ktxa_times]
    cand_min = t.hour * 60 + t.minute
    return any(abs(cand_min - tm) <= tol for tm in target_minutes)


__all__ = ["navigate_to_search", "fill_search_form", "submit_search", "perform_search"]
