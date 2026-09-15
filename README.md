# 🪑 빈자리 (binjari)

**기차(KTX)의 빈자리를 감시하다가 잡아주는 예매 워처.**
조회 → 예약 → (옵션) 결제/발권 → (옵션) 승차권 전달까지 자동 처리하고, 성공하면 Teams 알림으로 알려줍니다.

> **개인 사용용(personal use) 프로젝트입니다.** 작성자 개인의 학습·편의 목적으로 만든 도구로,
> 범용 배포·상업적 이용을 염두에 두지 않았으며 동작 보장이나 지원을 제공하지 않습니다.

## 구성

| 모듈 | 역할 | 실행 |
|---|---|---|
| `ktx_watcher` | 코레일 KTX 감시·예매 워처 (경로 등 승객유형·승차권 전달 지원) | `python -m ktx_watcher.main` |
| `web_server` | 출발·도착·시간 조회 웹 UI (FastAPI) | `python -m uvicorn web_server:app --host 127.0.0.1 --port 8001` |
| `team_mcp` | Teams 알림 (Azure AD OAuth / Graph API) | `python -m team_mcp.login` (최초 인증) |

`web_server` 는 로그인 정보·카드번호를 다루는 API(`/api/settings`, `/api/watcher/*`)에 인증이 없으므로
**반드시 `127.0.0.1`(로컬)로만 바인딩**하세요. `--host 0.0.0.0` 은 같은 네트워크의 다른 기기에서도
접근·수정이 가능해지므로 권장하지 않습니다.

## OS 지원

- **Windows / macOS** 를 자동 감지해서 동작합니다 (`ktx_watcher/chrome_launcher.py` 가 플랫폼별 Chrome 실행파일
  경로를 자동 탐지). 못 찾으면 `.env.ktx` 의 `KTXA_CHROME_EXE` 로 직접 지정하세요.
  - macOS 기본 경로: `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
  - Playwright 자체 Chromium 이 아닌 **실제 Google Chrome 설치**가 필요합니다 (코레일 매크로/봇 가드가
    Playwright 가 직접 launch 한 Chromium 은 계속 차단하기 때문에, 이미 떠 있는 일반 Chrome 프로세스에
    CDP 로 attach 하는 방식으로 우회합니다).
- `KTXA_VDESK`(가상 데스크톱 자동 이동) 는 **Windows 전용 기능**(PowerShell)이며, macOS/Linux 에서는 켜져
  있어도 자동으로 skip 됩니다.

## 설치

- **자동 (Windows)**: Claude Code에서 `binjari_setup` 스킬 실행 — 의존성 설치, 실행 런처(`launch_binjari.bat`), 바탕화면 아이콘까지 셋업.
- **수동 (Windows/macOS 공통)**: Python 3.12+ 에서

  ```bash
  uv sync            # 또는: pip install -e .
  playwright install chromium
  ```

  macOS 는 위 설치 후 `.env.ktx` 에 `KTXA_CHROME_EXE`(필요 시)와 `KTXA_CDP_USER_DATA_DIR`
  (예: `~/chrome-ktx-watcher`)만 맥 경로로 채우면 됩니다.

## 설정

- `.env.ktx.example` 을 `.env.ktx` 로 복사해 `<...>` 자리에 실값(로그인·카드·Azure)을 채웁니다.
  **실값 파일은 커밋 금지** — `.gitignore` 가 `.env.*` 전체를 차단하며 양식(`*.example`)만 커밋됩니다.
- `.env.ktx` 는 평문으로 저장되므로(파일 권한을 소유자 전용으로 제한해도 디스크 암호화가 없다면
  완전한 보호는 아님) 신뢰할 수 있는 개인 PC에서만 사용하세요.
- Teams 알림을 쓰려면 Azure AD OAuth 값을 채우고 `python -m team_mcp.login` 으로 최초 1회 인증합니다.
- Azure 설정만 별도 파일로 뽑을 때는 `azure_env_export` 스킬 사용 (`.env.ktx` → `.env.azure`).

---

## ⚠️ 주의 사항

- 본 도구는 학습/개인 편의 목적이며, **사이트 약관 및 관련 법령 준수**가 필요합니다.
- 사이트 구조 변경 시 일부 동작이 중단될 수 있습니다(로그/캡쳐로 원인 추적 가능).
