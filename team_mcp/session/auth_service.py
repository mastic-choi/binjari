"""
Authentication Service
인증 플로우와 토큰 관리의 핵심 기능
"""

import asyncio
import logging
import secrets
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
import aiohttp

from .azure_config import AzureConfig
from .auth_database import AuthDatabase

logger = logging.getLogger(__name__)


def get_default_user_email() -> Optional[str]:
    """
    auth.db의 azure_user_info 테이블에서 첫 번째 사용자 이메일을 가져옴

    Returns:
        첫 번째 사용자의 이메일 또는 None
    """
    db = AuthDatabase()
    users = db.list_users()
    if users:
        return users[0].get('user_email') or users[0].get('email')
    return None


class AuthService:
    """인증 서비스 - 인증 플로우와 토큰 관리"""

    def __init__(self, auth_db=None, config=None):
        """
        인증 서비스 초기화

        Args:
            auth_db: AuthDatabase 인스턴스 (선택적)
            config: AzureConfig 인스턴스 (선택적)
        """
        # DB 인스턴스 (전달받거나 새로 생성)
        if auth_db:
            self.auth_db = auth_db
        else:
            import os
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(base_dir, 'database', 'auth.db')
            self.auth_db = AuthDatabase(db_path)

        # Config 인스턴스 (전달받거나 새로 생성)
        if config:
            self.config = config
        else:
            self.config = AzureConfig(self.auth_db)

        self.session = None

        # 인증 상태 저장
        self.auth_states: Dict[str, Dict[str, Any]] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        """aiohttp 세션 관리"""
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    def start_auth_flow(self, force_new: bool = False) -> Dict[str, str]:
        """
        인증 플로우 시작 - 인증 URL 생성

        Args:
            force_new: 새로운 인증 강제 시작 여부

        Returns:
            Dict: 인증 정보
                - auth_url: Azure AD 인증 URL
                - state: 보안 검증용 state
        """
        # DB/테이블 존재 보장
        self.auth_db.ensure_tables()
        self.auth_db.ensure_default_app()

        # Config가 로드되지 않았으면 다시 로드
        if not self.config.azure_client_id:
            self.config.load_config_from_db()

        # State 생성
        state = secrets.token_urlsafe(32)

        # 인증 상태 저장
        self.auth_states[state] = {
            'created_at': datetime.now(timezone.utc),
            'status': 'pending'
        }

        # OAuth URL 생성
        auth_url = self._generate_auth_url(state, self.config.default_scopes)

        logger.info(f"Auth flow started with state: {state[:10]}...")

        return {
            'auth_url': auth_url,
            'state': state
        }

    async def complete_auth_flow(self, authorization_code: str, state: str) -> Dict[str, Any]:
        """
        인증 플로우 완료 - 콜백 처리

        Args:
            authorization_code: Azure AD에서 받은 인증 코드
            state: 보안 검증용 state

        Returns:
            Dict: 인증 결과
                - user_email: 사용자 이메일
                - access_token: 액세스 토큰
                - expires_at: 만료 시간
        """
        try:
            # State 검증 (CSRF 방지) — start_auth_flow 가 발급한 state 인지, 만료(10분) 전인지 확인.
            auth_info = self.auth_states.pop(state, None)
            if auth_info is None:
                raise Exception("Invalid or already-used OAuth state")
            age = (datetime.now(timezone.utc) - auth_info["created_at"]).total_seconds()
            if age > 600:
                raise Exception("OAuth state expired")

            # Authorization code로 토큰 교환
            token_result = await self._exchange_code_for_tokens(authorization_code)

            # 사용자 정보 가져오기
            user_info = await self._get_user_info(token_result['access_token'])
            email = user_info.get('mail') or user_info.get('userPrincipalName')

            if not email:
                raise Exception("Could not determine user email")

            # 토큰과 사용자 정보 저장
            self.auth_db.save_user(email, user_info)
            self.auth_db.save_token(email, token_result)

            logger.info(f"Authentication successful for {email}")

            return {
                'user_email': email,
                'display_name': user_info.get('displayName'),
                'object_id': user_info.get('id'),
                'access_token': token_result['access_token'],
                'refresh_token': token_result.get('refresh_token'),
                'expires_at': token_result['expires_at']
            }

        except Exception as e:
            logger.error(f"Failed to complete auth flow: {str(e)}")
            raise

    def _generate_auth_url(self, state: str, scopes: list) -> str:
        """OAuth 인증 URL 생성"""
        params = {
            'client_id': self.config.azure_client_id,
            'response_type': 'code',
            'redirect_uri': self.config.azure_redirect_uri,
            'response_mode': 'query',
            'scope': ' '.join(scopes),
            'state': state
        }

        base_url = f"https://login.microsoftonline.com/{self.config.azure_tenant_id}/oauth2/v2.0/authorize"
        return f"{base_url}?{urlencode(params)}"

    async def _exchange_code_for_tokens(self, auth_code: str) -> Dict[str, Any]:
        """Authorization code를 토큰으로 교환"""
        token_url = f"https://login.microsoftonline.com/{self.config.azure_tenant_id}/oauth2/v2.0/token"

        data = {
            'client_id': self.config.azure_client_id,
            'client_secret': self.config.azure_client_secret,
            'code': auth_code,
            'redirect_uri': self.config.azure_redirect_uri,
            'grant_type': 'authorization_code'
        }

        session = await self._get_session()
        async with session.post(token_url, data=data) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(f"Token exchange failed: {error_text}")

            token_data = await response.json()

            # 만료 시간 계산
            expires_in = token_data.get('expires_in', 3600)
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

            return {
                'access_token': token_data['access_token'],
                'refresh_token': token_data.get('refresh_token'),
                'token_type': token_data.get('token_type', 'Bearer'),
                'expires_in': expires_in,
                'expires_at': expires_at.isoformat(),
                'scope': token_data.get('scope', '')
            }

    async def _get_user_info(self, access_token: str) -> Dict[str, Any]:
        """사용자 정보 조회"""
        user_info_url = "https://graph.microsoft.com/v1.0/me"

        session = await self._get_session()
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json'
        }

        async with session.get(user_info_url, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(f"Failed to get user info: {error_text}")

            return await response.json()

    async def refresh_tokens(self, refresh_token: str) -> Dict[str, Any]:
        """
        토큰 갱신

        Args:
            refresh_token: 리프레시 토큰

        Returns:
            새로운 토큰 정보
        """
        token_url = f"https://login.microsoftonline.com/{self.config.azure_tenant_id}/oauth2/v2.0/token"

        data = {
            'client_id': self.config.azure_client_id,
            'client_secret': self.config.azure_client_secret,
            'refresh_token': refresh_token,
            'grant_type': 'refresh_token'
        }

        try:
            session = await self._get_session()
            async with session.post(token_url, data=data) as response:
                if response.status != 200:
                    error_text = await response.text()
                    if 'invalid_grant' in error_text:
                        raise Exception("Refresh token expired or revoked")
                    raise Exception(f"Token refresh failed: {error_text}")

                token_data = await response.json()

                # 만료 시간 계산
                expires_in = token_data.get('expires_in', 3600)
                expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

                result = {
                    'access_token': token_data['access_token'],
                    'token_type': token_data.get('token_type', 'Bearer'),
                    'expires_in': expires_in,
                    'expires_at': expires_at.isoformat(),
                    'scope': token_data.get('scope', '')
                }

                # 새 refresh token이 있으면 포함, 없으면 기존 것 유지
                if token_data.get('refresh_token'):
                    result['refresh_token'] = token_data['refresh_token']
                else:
                    result['refresh_token'] = refresh_token

                logger.info("Token refreshed successfully")
                return result

        except Exception as e:
            logger.error(f"Token refresh error: {str(e)}")
            raise

    def is_token_expired(self, expires_at: Any, buffer_seconds: int = 300) -> bool:
        """
        토큰 만료 확인

        Args:
            expires_at: 만료 시간 (datetime 또는 ISO string)
            buffer_seconds: 버퍼 시간 (기본 5분)

        Returns:
            만료 여부
        """
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)

        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        # 버퍼 시간을 고려한 만료 체크
        return datetime.now(timezone.utc) >= (expires_at - timedelta(seconds=buffer_seconds))

    def is_refresh_token_expired(self, created_at: Any, days: int = 90) -> bool:
        """
        리프레시 토큰 만료 확인 (Azure AD 기본 90일)

        Args:
            created_at: 토큰 생성 시간
            days: 유효 기간 (일)

        Returns:
            만료 여부
        """
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)

        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        expiry_date = created_at + timedelta(days=days)
        return datetime.now(timezone.utc) >= expiry_date

    def is_refresh_expiry_passed(self, refresh_token_expires_at: Any) -> bool:
        """
        리프레시 토큰의 절대 만료 시각 비교 (DB의 refresh_token_expires_at 컬럼 기준)
        save_token에서 한 번만 채워지고 그 후 보존되므로 매 refresh마다 리셋되지 않음.

        Args:
            refresh_token_expires_at: ISO 형식 문자열 또는 datetime, None이면 만료된 것으로 간주

        Returns:
            만료 여부
        """
        if refresh_token_expires_at is None:
            return True

        if isinstance(refresh_token_expires_at, str):
            refresh_token_expires_at = datetime.fromisoformat(refresh_token_expires_at)

        if refresh_token_expires_at.tzinfo is None:
            refresh_token_expires_at = refresh_token_expires_at.replace(tzinfo=timezone.utc)

        return datetime.now(timezone.utc) >= refresh_token_expires_at

    async def check_and_refresh_if_needed(self, email: Optional[str] = None, buffer_seconds: int = 300) -> Dict[str, Any]:
        """
        토큰 만료 확인 후 필요시 자동 갱신

        Args:
            email: 사용자 이메일. None이면 auth.db에서 첫 번째 사용자를 자동으로 가져옴
            buffer_seconds: 버퍼 시간 (기본 5분)

        Returns:
            Dict: 토큰 상태 및 갱신 결과
                - status: 'valid', 'refreshed', 'error'
                - access_token: 유효한 액세스 토큰
                - message: 상태 메시지
                - refreshed: 갱신 여부
        """
        if not email:
            email = get_default_user_email()
            if not email:
                return {
                    'status': 'error',
                    'message': 'No email provided and no users found in auth.db',
                    'refreshed': False
                }

        try:
            # 현재 토큰 정보 조회
            token_info = self.auth_db.get_token(email)

            if not token_info:
                return {
                    'status': 'error',
                    'message': f'No token found for {email}',
                    'refreshed': False
                }

            # 토큰 만료 확인 (버퍼 시간 포함)
            if not self.is_token_expired(token_info['expires_at'], buffer_seconds):
                # 토큰이 아직 유효함
                logger.info(f"Token for {email} is still valid")
                return {
                    'status': 'valid',
                    'access_token': token_info['access_token'],
                    'refresh_token': token_info.get('refresh_token'),
                    'expires_at': token_info['expires_at'],
                    'message': 'Token is valid',
                    'refreshed': False
                }

            # 토큰이 만료되었거나 버퍼 시간 이내 - 리프레시 토큰 확인
            refresh_token = token_info.get('refresh_token')
            if not refresh_token:
                logger.error(f"No refresh token available for {email}")
                auth_info = self.start_auth_flow()
                return {
                    'status': 'auth_required',
                    'auth_url': auth_info['auth_url'],
                    'state': auth_info['state'],
                    'message': f'No refresh token available. 재인증이 필요합니다.\n{auth_info["auth_url"]}',
                    'refreshed': False
                }

            # 리프레시 토큰 만료 확인 (refresh_token_expires_at 컬럼 직접 비교)
            if self.is_refresh_expiry_passed(token_info.get('refresh_token_expires_at')):
                logger.error(f"Refresh token expired for {email}")
                auth_info = self.start_auth_flow()
                return {
                    'status': 'auth_required',
                    'auth_url': auth_info['auth_url'],
                    'state': auth_info['state'],
                    'message': f'Refresh token 만료. 재인증이 필요합니다.\n{auth_info["auth_url"]}',
                    'refreshed': False
                }

            # 토큰 갱신 시도
            logger.info(f"Token for {email} is expired or within buffer time. Refreshing...")
            new_tokens = await self.refresh_tokens(refresh_token)

            # 갱신된 토큰 저장
            self.auth_db.update_token(email, new_tokens)
            logger.info(f"Token refreshed successfully for {email}")

            return {
                'status': 'refreshed',
                'access_token': new_tokens['access_token'],
                'refresh_token': new_tokens.get('refresh_token', refresh_token),
                'expires_at': new_tokens['expires_at'],
                'message': 'Token refreshed successfully',
                'refreshed': True
            }

        except Exception as e:
            logger.error(f"Error checking/refreshing token for {email}: {str(e)}")
            return {
                'status': 'error',
                'message': f'Failed to refresh token: {str(e)}',
                'refreshed': False
            }

    async def close(self):
        """리소스 정리"""
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("Auth service closed")
