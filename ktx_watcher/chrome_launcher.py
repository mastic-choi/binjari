"""Chrome 디버그 인스턴스 관리 (subprocess + 헬스체크).

watcher 가 직접 chrome.exe 를 ``--remote-debugging-port=<port>`` 로 띄우고,
9222 헬스 엔드포인트(``/json/version``) 가 응답할 때까지 polling 한다.

- 이미 같은 포트에 Chrome 이 살아있으면 그대로 재사용 (subprocess 안 띄움).
- watcher 가 띄운 경우, 종료 시 같은 프로세스를 terminate.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

import platformdirs

LOGGER = logging.getLogger("ktx_watcher_spa.chrome_launcher")


def _default_user_data_dir() -> Path:
    """KTXA_CDP_USER_DATA_DIR 미설정 시 쓸 전용 프로필 경로.

    최신 Chrome(2025년 이후 버전)은 보안 정책상 **기본 프로필**로는
    --remote-debugging-port 를 거부한다 — CDP 포트가 열리지 않고, 대신
    이미 떠 있는 사용자의 평소 Chrome 창에 about:blank 탭만 하나 얹힌 뒤
    끝나버린다 (겉보기엔 "빈 탭만 뜨고 아무 일도 안 일어남"). user_data_dir
    를 아예 지정하지 않는 게 오히려 흔한 실패 원인이라, 비워뒀을 때도
    이 워처만 쓰는 전용 프로필 폴더를 자동으로 잡아준다.
    """
    return Path(platformdirs.user_data_dir("binjari")) / "chrome-cdp-profile"


def _force_ipv4_for_localhost() -> None:
    """Windows + Chrome CDP 호환: 'localhost' 를 IPv4(127.0.0.1) 만 해석되게 한다.

    Chrome 의 ``--remote-debugging-port`` 는 0.0.0.0(IPv4) 만 listen 하는데
    Python 의 ``socket.getaddrinfo`` 는 IPv6(::1) 을 우선 반환해 EADDRINUSE 가 난다.
    playwright 의 connect_over_cdp 도 같은 경로를 탄다.
    """
    if getattr(_force_ipv4_for_localhost, "_applied", False):
        return
    orig = socket.getaddrinfo

    def _ipv4_only(host, port, *args, **kwargs):
        if isinstance(host, str) and host.lower() in ("localhost", "::1"):
            host = "127.0.0.1"
        # AF_INET 만 반환하도록 family 인자도 제한
        if len(args) >= 1:
            args = (socket.AF_INET,) + args[1:]
        else:
            kwargs["family"] = socket.AF_INET
        return orig(host, port, *args, **kwargs)

    socket.getaddrinfo = _ipv4_only  # type: ignore[assignment]
    _force_ipv4_for_localhost._applied = True  # type: ignore[attr-defined]
    LOGGER.debug("socket.getaddrinfo monkey-patched → IPv4 only for localhost")


# 모듈 import 시점에 즉시 적용 (urllib / playwright 모두 영향받기 전에)
_force_ipv4_for_localhost()

DEFAULT_CHROME_PATHS_WIN = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

DEFAULT_CHROME_PATHS_MAC = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
)

DEFAULT_CHROME_PATHS_LINUX = (
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
)

DEFAULT_CHROME_PATHS = (
    DEFAULT_CHROME_PATHS_WIN
    if os.name == "nt"
    else DEFAULT_CHROME_PATHS_MAC
    if sys.platform == "darwin"
    else DEFAULT_CHROME_PATHS_LINUX
)


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _cdp_alive(port: int, retries: int = 2, http_timeout: float = 5.0) -> bool:
    """포트가 열려있고 ``/json/version`` 이 응답하면 True.

    NOTE: Chrome DevTools 는 Host=127.0.0.1 을 거부 (RemoteDisconnected).
    URL 은 반드시 ``localhost`` 로 호출해야 함. (검증 2026-05-14)
    """
    if not _port_open("127.0.0.1", port, timeout=1.0):
        return False
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(f"http://localhost:{port}/json/version")
            with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                if resp.status == 200:
                    return True
        except Exception as e:
            LOGGER.debug("CDP probe attempt=%d 실패: %s", attempt + 1, e)
        time.sleep(0.3)
    return False


class ChromeLauncher:
    """chrome.exe 를 디버그 모드로 띄우고 lifecycle 을 관리.

    Usage:
        launcher = ChromeLauncher(port=9222, user_data_dir=..., exe_path=...)
        launcher.ensure_running()
        ...
        launcher.shutdown_if_owned()
    """

    def __init__(
        self,
        port: int = 9222,
        user_data_dir: Optional[Path] = None,
        exe_path: Optional[str] = None,
        startup_timeout: float = 15.0,
        vdesk: bool = False,
        vdesk_name: str = "binjari",
    ) -> None:
        self.port = port
        self.user_data_dir = (
            Path(user_data_dir).expanduser() if user_data_dir else _default_user_data_dir()
        )
        self.exe_path = exe_path or self._find_chrome_exe()
        self.startup_timeout = startup_timeout
        self.vdesk = vdesk
        self.vdesk_name = vdesk_name
        self._proc: Optional[subprocess.Popen] = None  # watcher 가 띄운 경우만 set
        self._reused_existing = False

    @staticmethod
    def _find_chrome_exe() -> str:
        for p in DEFAULT_CHROME_PATHS:
            if os.path.isfile(p):
                return p
        raise RuntimeError(
            "chrome.exe 경로를 찾지 못했습니다. KTXA_CHROME_EXE 환경변수로 지정하세요."
        )

    @property
    def cdp_url(self) -> str:
        return f"http://localhost:{self.port}"

    def ensure_running(self) -> str:
        """Chrome 디버그 인스턴스가 살아있도록 보장. CDP URL 반환."""
        if _cdp_alive(self.port):
            LOGGER.info("Chrome 디버그 인스턴스 이미 살아있음: port=%d (재사용)", self.port)
            self._reused_existing = True
            self._move_to_vdesk()
            return self.cdp_url

        args = [
            self.exe_path,
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            # 다른 가상 데스크톱/가려진 창에서도 렌더링·타이머 스로틀링 방지
            "--disable-features=CalculateNativeWinOcclusion",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ]
        if self.user_data_dir:
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            args.append(f"--user-data-dir={self.user_data_dir}")
        args.append("about:blank")

        LOGGER.info(
            "Chrome 디버그 모드 기동: %s (port=%d, user_data_dir=%s)",
            self.exe_path, self.port, self.user_data_dir,
        )
        # stdout/stderr 는 무시 (Chrome 이 콘솔에 잡히면 종료 시 같이 죽음 회피)
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

        # CDP 가 떴는지 polling
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            if _cdp_alive(self.port):
                LOGGER.info("Chrome CDP 준비 완료 (port=%d)", self.port)
                self._move_to_vdesk()
                return self.cdp_url
            time.sleep(0.3)

        # timeout → 정리
        self.shutdown_if_owned()
        raise RuntimeError(
            f"Chrome 디버그 포트 {self.port} 가 {self.startup_timeout}s 안에 응답하지 않음"
        )

    def _move_to_vdesk(self) -> None:
        """Chrome 창을 별도 가상 데스크톱(vdesk_name)으로 이동. 실패해도 진행.

        vdesk_move.ps1 이 VirtualDesktop 모듈(내부 API)로 이동한다 —
        문서화된 IVirtualDesktopManager 는 타 프로세스 창에 Access Denied.
        창이 CDP 준비보다 늦게 뜰 수 있어 짧게 재시도한다.
        """
        if not self.vdesk:
            return
        if os.name != "nt":
            LOGGER.debug("vdesk 이동 skip: Windows 전용 기능 (현재 플랫폼=%s)", sys.platform)
            return
        script = Path(__file__).with_name("vdesk_move.ps1")
        cmd = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script),
            "-Port", str(self.port), "-Name", self.vdesk_name,
        ]
        for attempt in range(3):
            try:
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30.0,
                    encoding="utf-8", errors="replace",
                )
            except Exception as e:
                LOGGER.warning("vdesk 이동 실행 실패: %s", e)
                return
            out = (r.stdout or "").strip()
            if r.returncode == 0:
                LOGGER.info("Chrome 창 가상 데스크톱 이동: %s", out)
                return
            if r.returncode == 4:  # 창이 아직 안 뜸 → 재시도
                time.sleep(1.0)
                continue
            LOGGER.warning(
                "vdesk 이동 skip (exit=%d): %s %s",
                r.returncode, out, (r.stderr or "").strip()[:200],
            )
            return
        LOGGER.warning("vdesk 이동 포기: Chrome 창을 찾지 못함 (port=%d)", self.port)

    def shutdown_if_owned(self) -> None:
        """watcher 가 띄운 인스턴스만 종료. 사용자가 띄운 건 건드리지 않음."""
        if self._reused_existing or self._proc is None:
            return
        try:
            LOGGER.info("Chrome 디버그 인스턴스 종료 (watcher-owned)")
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except Exception as e:
            LOGGER.warning("Chrome 종료 실패: %s", e)
        finally:
            self._proc = None


__all__ = ["ChromeLauncher", "_cdp_alive"]
