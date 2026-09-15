"""OAuth 토큰 필드 암호화 (Fernet 대칭키, DB 파일과 별도 보관).

auth.db 옆에 auth.db.key 를 따로 둔다 — DB 파일만 복사/유출/백업 동기화 사고가 나도
키 파일이 없으면 access_token/refresh_token/id_token 은 그대로 읽히지 않는다.
같은 계정으로 로그인한 다른 프로세스나 디스크 전체 접근까지 막는 건 아니라서
(OS 키체인 수준의 방어는 아님) 완전한 암호화 솔루션은 아니지만, DB 파일 단독
유출에 대한 최소한의 방어선은 된다.

기존에 평문으로 저장된 레코드는 decrypt() 가 InvalidToken 을 평문으로 간주해
그대로 반환하므로 별도 마이그레이션 없이 다음 save_token() 시점에 자연히
암호화된다.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


def _key_path(db_path: str) -> Path:
    return Path(db_path).with_name(Path(db_path).name + ".key")


def _load_or_create_key(db_path: str) -> bytes:
    path = _key_path(db_path)
    if path.is_file():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


class TokenCipher:
    """db_path 별로 독립된 키를 쓰는 Fernet wrapper. None 은 그대로 통과시킨다."""

    def __init__(self, db_path: str):
        self._fernet = Fernet(_load_or_create_key(db_path))

    def encrypt(self, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            # 이 코드 적용 전에 평문으로 저장된 기존 레코드 — 그대로 반환.
            return value
