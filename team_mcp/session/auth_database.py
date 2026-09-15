"""
Authentication Database Module
Azure AD 인증 관련 데이터(앱, 사용자, 토큰)의 CRUD 작업을 담당
"""

import sqlite3
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
import logging

from .token_crypto import TokenCipher

logger = logging.getLogger(__name__)


class AuthDatabase:
    """인증 데이터베이스 - Azure AD 앱, 사용자, 토큰 정보 저장"""

    def __init__(self, db_path: str = "database/auth.db"):
        """
        데이터베이스 초기화

        Args:
            db_path: 데이터베이스 파일 경로
        """
        # Always resolve to an absolute path so callers can use relative or env paths
        resolved_path = os.path.expanduser(db_path)
        if not os.path.isabs(resolved_path):
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            resolved_path = os.path.join(base_dir, resolved_path)

        # Ensure the directory exists (no-op if already present)
        db_dir = os.path.dirname(resolved_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self.db_path = resolved_path
        # 기본 app_id (환경 변수에서 가져옴)
        self.default_client_id = os.getenv('AZURE_CLIENT_ID')
        self._cipher = TokenCipher(self.db_path)
        self.ensure_tables()
        self.ensure_default_app()

    def ensure_tables(self):
        """필요한 테이블 생성 (azure_* 테이블 사용)"""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            # schema.sql 파일 읽기
            import os
            schema_path = os.path.join(os.path.dirname(self.db_path), 'schema.sql')

            if os.path.exists(schema_path):
                with open(schema_path, 'r') as f:
                    schema_sql = f.read()
                    cursor.executescript(schema_sql)
                    conn.commit()
                    logger.info("Database tables created from schema.sql")
            else:
                # schema.sql이 없으면 직접 생성
                cursor.executescript("""
                    -- Azure AD Application Configuration
                    CREATE TABLE IF NOT EXISTS azure_app_config (
                        client_id TEXT PRIMARY KEY,
                        client_secret TEXT NOT NULL,
                        tenant_id TEXT NOT NULL DEFAULT 'common',
                        redirect_uri TEXT NOT NULL DEFAULT 'http://localhost:5000/callback',
                        app_name TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    -- User Information
                    CREATE TABLE IF NOT EXISTS azure_user_info (
                        user_email TEXT PRIMARY KEY,
                        object_id TEXT UNIQUE,
                        display_name TEXT,
                        given_name TEXT,
                        surname TEXT,
                        job_title TEXT,
                        department TEXT,
                        office_location TEXT,
                        mobile_phone TEXT,
                        business_phones TEXT,
                        preferred_language TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    -- Token Information (Improved Schema)
                    CREATE TABLE IF NOT EXISTS azure_token_info (
                        user_email TEXT PRIMARY KEY,
                        access_token TEXT NOT NULL,
                        refresh_token TEXT,
                        -- token_type 제거 (항상 'Bearer'이므로 불필요)
                        scope TEXT,
                        access_token_expires_at TIMESTAMP NOT NULL,  -- 명확한 이름으로 변경
                        refresh_token_expires_at TIMESTAMP,  -- refresh token 만료 시간 추가
                        id_token TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_email) REFERENCES azure_user_info(user_email) ON DELETE CASCADE
                    );

                    -- Indexes
                    CREATE INDEX IF NOT EXISTS idx_access_token_expires ON azure_token_info(access_token_expires_at);
                    CREATE INDEX IF NOT EXISTS idx_refresh_token_expires ON azure_token_info(refresh_token_expires_at);
                    CREATE INDEX IF NOT EXISTS idx_user_object_id ON azure_user_info(object_id);
                """)
                conn.commit()
                logger.info("Database tables created directly")

        except Exception as e:
            logger.error(f"[ERROR] Failed to create tables: {e}")
            raise
        finally:
            conn.close()

    def ensure_default_app(self):
        """기본 Azure AD 앱 정보 확인/생성"""
        if not self.default_client_id:
            return

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            # 앱이 이미 있는지 확인
            cursor.execute(
                "SELECT client_id FROM azure_app_config WHERE client_id = ?",
                (self.default_client_id,)
            )

            if not cursor.fetchone():
                # 없으면 생성
                cursor.execute("""
                    INSERT INTO azure_app_config (
                        client_id,
                        client_secret,
                        tenant_id,
                        redirect_uri
                    ) VALUES (?, ?, ?, ?)
                """, (
                    self.default_client_id,
                    os.getenv('AZURE_CLIENT_SECRET'),
                    os.getenv('AZURE_TENANT_ID', 'common'),
                    os.getenv('AZURE_REDIRECT_URI', 'http://localhost:5000/callback')
                ))
                conn.commit()
                logger.info(f"Default app registered: {self.default_client_id[:8]}...")

        except Exception as e:
            logger.error(f"[ERROR] Failed to ensure default app: {e}")
        finally:
            conn.close()

    def save_user(self, email: str, user_info: Dict[str, Any]) -> bool:
        """
        사용자 정보 저장 (azure_user_info 테이블 사용)

        Args:
            email: 사용자 이메일
            user_info: Microsoft Graph API에서 받은 사용자 정보

        Returns:
            성공 여부
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            object_id = user_info.get("id") or user_info.get("objectId")

            cursor.execute("""
                INSERT OR REPLACE INTO azure_user_info (
                    user_email,
                    object_id,
                    display_name,
                    given_name,
                    surname,
                    job_title,
                    department,
                    office_location,
                    mobile_phone,
                    business_phones,
                    preferred_language,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                email,
                object_id,
                user_info.get('displayName'),
                user_info.get('givenName'),
                user_info.get('surname'),
                user_info.get('jobTitle'),
                user_info.get('department'),
                user_info.get('officeLocation'),
                user_info.get('mobilePhone'),
                str(user_info.get('businessPhones', [])),
                user_info.get('preferredLanguage')
            ))

            conn.commit()
            logger.info(f"User saved to azure_user_info: {email}")
            return True

        except Exception as e:
            logger.error(f"[ERROR] Failed to save user: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def save_token(self, email: str, token_info: Dict[str, Any]) -> bool:
        """
        토큰 정보 저장 (azure_token_info 테이블 사용)

        Args:
            email: 사용자 이메일
            token_info: 토큰 정보

        Returns:
            성공 여부
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            # 먼저 사용자의 object_id를 가져옴 (선택적)
            cursor.execute("""
                SELECT object_id FROM azure_user_info
                WHERE user_email = ?
            """, (email,))

            row = cursor.fetchone()
            object_id = row[0] if row else None

            # Refresh token 만료 시간 계산 (기본 90일)
            from datetime import datetime, timedelta, timezone
            refresh_expires_at = None
            if token_info.get('refresh_token'):
                # Azure AD 기본값: 90일 (비활성), 최대 1년 (활성 사용)
                # ISO 형식으로 통일 (UTC 시간대 명시)
                refresh_expires_at = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()

            # azure_token_info에 UPSERT - created_at 보존하여 refresh_token 90일 만료가 매 refresh마다
            # 리셋되지 않도록 함. 기존 row가 있으면 created_at 그대로, refresh_token_expires_at도
            # 새 값이 있을 때만 갱신 (없으면 기존 만료 시간 유지)
            cursor.execute("""
                INSERT INTO azure_token_info (
                    user_email,
                    access_token,
                    refresh_token,
                    id_token,
                    access_token_expires_at,
                    refresh_token_expires_at,
                    scope,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_email) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = COALESCE(excluded.refresh_token, refresh_token),
                    id_token = COALESCE(excluded.id_token, id_token),
                    access_token_expires_at = excluded.access_token_expires_at,
                    refresh_token_expires_at = COALESCE(refresh_token_expires_at, excluded.refresh_token_expires_at),
                    scope = COALESCE(excluded.scope, scope),
                    updated_at = CURRENT_TIMESTAMP
            """, (
                email,
                self._cipher.encrypt(token_info.get('access_token')),
                self._cipher.encrypt(token_info.get('refresh_token')),
                self._cipher.encrypt(token_info.get('id_token')),
                token_info.get('expires_at'),  # access token 만료 시간
                refresh_expires_at,  # refresh token 만료 시간 (신규 row일 때만 사용)
                token_info.get('scope')
            ))

            conn.commit()
            logger.info(f"Token saved to azure_token_info for: {email}")
            return True

        except Exception as e:
            logger.error(f"[ERROR] Failed to save token: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def get_token(self, email: str) -> Optional[Dict[str, Any]]:
        """
        사용자의 토큰 정보 조회 (azure_token_info에서)

        Args:
            email: 사용자 이메일

        Returns:
            토큰 정보 또는 None
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()

            # 토큰 조회 (user_email로 직접 조인)
            cursor.execute("""
                SELECT t.*, u.user_email as email
                FROM azure_token_info t
                LEFT JOIN azure_user_info u ON t.user_email = u.user_email
                WHERE t.user_email = ?
                ORDER BY t.updated_at DESC
                LIMIT 1
            """, (email,))

            row = cursor.fetchone()
            if row:
                token = dict(row)
                token['access_token'] = self._cipher.decrypt(token.get('access_token'))
                token['refresh_token'] = self._cipher.decrypt(token.get('refresh_token'))
                token['id_token'] = self._cipher.decrypt(token.get('id_token'))
                # Convert access_token_expires_at string to datetime if needed
                if isinstance(token.get('access_token_expires_at'), str):
                    token['access_token_expires_at'] = datetime.fromisoformat(
                        token['access_token_expires_at'].replace('Z', '+00:00')
                    )
                # For backward compatibility - map to old column name
                token['expires_at'] = token.get('access_token_expires_at')
                return token
            return None

        except Exception as e:
            logger.error(f"[ERROR] Failed to get token: {e}")
            return None
        finally:
            conn.close()

    def update_token(self, email: str, token_info: Dict[str, Any]) -> bool:
        """
        토큰 갱신

        Args:
            email: 사용자 이메일
            token_info: 새로운 토큰 정보

        Returns:
            성공 여부
        """
        return self.save_token(email, token_info)

    def delete_token(self, email: str) -> bool:
        """
        토큰 삭제 (로그아웃)

        Args:
            email: 사용자 이메일

        Returns:
            성공 여부
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            # azure_token_info는 user_email이 기본키이므로 직접 삭제
            cursor.execute("""
                DELETE FROM azure_token_info
                WHERE user_email = ?
            """, (email,))
            conn.commit()

            if cursor.rowcount > 0:
                logger.info(f"Token deleted for: {email}")
                return True

            return False

        except Exception as e:
            logger.error(f"[ERROR] Failed to delete token: {e}")
            return False
        finally:
            conn.close()

    def get_user(self, email: str) -> Optional[Dict[str, Any]]:
        """
        사용자 정보 조회

        Args:
            email: 사용자 이메일

        Returns:
            사용자 정보 또는 None
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM azure_user_info
                WHERE user_email = ?
            """, (email,))

            row = cursor.fetchone()
            return dict(row) if row else None

        except Exception as e:
            logger.error(f"[ERROR] Failed to get user: {e}")
            return None
        finally:
            conn.close()

    def list_users(self) -> List[Dict[str, Any]]:
        """
        모든 사용자 목록 조회

        Returns:
            사용자 리스트
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT u.*, t.access_token_expires_at as expires_at,
                       CASE WHEN t.access_token_expires_at > datetime('now') THEN 1 ELSE 0 END as has_valid_token
                FROM azure_user_info u
                LEFT JOIN azure_token_info t ON u.user_email = t.user_email
                ORDER BY u.updated_at DESC
            """)

            rows = cursor.fetchall()
            users = []
            for row in rows:
                user = dict(row)
                # Convert expires_at if present (에러 처리 추가)
                if user.get('expires_at') and isinstance(user['expires_at'], str):
                    try:
                        user['expires_at'] = datetime.fromisoformat(
                            user['expires_at'].replace('Z', '+00:00')
                        )
                    except ValueError:
                        # 잘못된 날짜 형식은 무시
                        user['expires_at'] = None
                        user['has_valid_token'] = 0
                # Use email field
                user['email'] = user.get('user_email') or user.get('mail')
                users.append(user)

            return users

        except Exception as e:
            logger.error(f"[ERROR] Failed to list users: {e}")
            return []
        finally:
            conn.close()

    def cleanup_expired_tokens(self) -> int:
        """
        만료된 토큰 정리

        Returns:
            정리된 토큰 수
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            # ISO format with timezone을 처리하기 위해 Python에서 비교
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()

            cursor.execute("""
                DELETE FROM azure_token_info
                WHERE (refresh_token IS NULL AND access_token_expires_at < ?)
                   OR (refresh_token_expires_at IS NOT NULL
                       AND refresh_token_expires_at < ?)
            """, (now, now))

            conn.commit()
            count = cursor.rowcount

            if count > 0:
                logger.info(f"Cleaned up {count} expired tokens")

            return count

        except Exception as e:
            logger.error(f"[ERROR] Failed to cleanup tokens: {e}")
            return 0
        finally:
            conn.close()

    def migrate_from_old_tables(self) -> bool:
        """
        기존 users/tokens 테이블에서 azure_* 테이블로 마이그레이션

        Returns:
            성공 여부
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            # 1. users -> azure_user_info 마이그레이션
            cursor.execute("""
                INSERT OR IGNORE INTO azure_user_info (
                    user_email, object_id, display_name,
                    created_at, updated_at
                )
                SELECT
                    email, object_id, display_name,
                    created_at, updated_at
                FROM users
            """)

            # 2. tokens -> azure_token_info 마이그레이션
            # (save_token() 을 거쳐야 access_token/refresh_token 이 암호화되어 들어간다 —
            #  raw INSERT...SELECT 로 옮기면 평문 그대로 복사돼 버린다.)
            cursor.execute("SELECT email, access_token, refresh_token, access_token_expires_at, scope FROM tokens")
            old_tokens = cursor.fetchall()
            conn.commit()

            for email, access_token, refresh_token, expires_at, scope in old_tokens:
                self.save_token(email, {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_at": expires_at,
                    "scope": scope,
                })

            logger.info("Migration from old tables completed")
            return True

        except Exception as e:
            logger.error(f"[ERROR] Migration failed: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()
