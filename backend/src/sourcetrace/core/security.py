"""Session security primitives for anonymous browser sessions."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

import jwt
from pydantic import SecretStr

from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.exceptions import SessionConfigurationError, SessionInvalidError

COOKIE_VERSION = "v1"
SESSION_INACTIVITY_DAYS = 7
SESSION_MAX_AGE_SECONDS = SESSION_INACTIVITY_DAYS * 24 * 60 * 60  # 604,800 seconds
MIN_SECRET_LENGTH = 32


def _coerce_secret(secret: str | SecretStr | None, fallback: SecretStr | None) -> str | None:
    """Resolve a signing secret from an explicit value or a configured fallback."""
    if isinstance(secret, SecretStr):
        return secret.get_secret_value()
    if isinstance(secret, str):
        return secret
    if secret is None and fallback is not None:
        return fallback.get_secret_value()
    return None


def generate_owner_session_id() -> str:
    """Generate a cryptographically random opaque session ID."""
    return f"sess_{secrets.token_urlsafe(32)}"


def generate_conversation_id() -> str:
    """Generate a cryptographically random opaque conversation ID."""
    return f"conv_{secrets.token_urlsafe(16)}"


def generate_message_id() -> str:
    """Generate a cryptographically random opaque message ID."""
    return f"msg_{secrets.token_urlsafe(16)}"


class SessionSigner:
    """Creates and validates HMAC-SHA256 signed session cookie tokens."""

    def __init__(self, secret: str | SecretStr | None = None) -> None:
        fallback = get_settings().session_signing_secret if secret is None else None
        raw_secret = _coerce_secret(secret, fallback)

        if not raw_secret or len(raw_secret) < MIN_SECRET_LENGTH:
            raise SessionConfigurationError(
                "Session signing secret is not configured or is too short."
            )

        self._secret_bytes = raw_secret.encode("utf-8")

    def create_cookie_token(self, owner_session_id: str, expires_at: datetime) -> str:
        """Sign and format an anonymous session cookie token."""
        exp_timestamp = int(expires_at.astimezone(UTC).timestamp())
        payload = f"{COOKIE_VERSION}.{owner_session_id}.{exp_timestamp}"
        signature = hmac.new(
            self._secret_bytes, payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{payload}.{signature}"

    def verify_cookie_token(
        self, cookie_token: str, current_time: datetime | None = None
    ) -> str | None:
        """Verify cookie signature, expiration, and version. Return owner_session_id if valid."""
        if not cookie_token or not isinstance(cookie_token, str):
            return None

        parts = cookie_token.split(".")
        if len(parts) != 4:
            return None

        version, owner_session_id, exp_str, signature_hex = parts

        if version != COOKIE_VERSION:
            return None

        if not owner_session_id or not owner_session_id.startswith("sess_"):
            return None

        try:
            exp_timestamp = int(exp_str)
        except ValueError:
            return None

        now = current_time or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)

        if int(now.timestamp()) >= exp_timestamp:
            return None

        payload = f"{version}.{owner_session_id}.{exp_str}"
        expected_sig = hmac.new(
            self._secret_bytes, payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_sig, signature_hex):
            return None

        return owner_session_id


class JWTSigner:
    """Creates and verifies stateless JWT Bearer tokens for anonymous browser sessions."""

    def __init__(
        self,
        secret: str | SecretStr | None = None,
        settings: Settings | None = None,
    ) -> None:
        active_settings = settings or get_settings()
        raw_secret = _coerce_secret(secret, active_settings.jwt_secret)

        secret_bytes = (raw_secret or "").encode("utf-8")
        if len(secret_bytes) < MIN_SECRET_LENGTH:
            raise SessionConfigurationError("JWT signing secret is not configured or is too short.")

        self._secret_bytes = secret_bytes
        self._algorithm = active_settings.jwt_algorithm
        self._issuer = active_settings.jwt_issuer
        self._audience = active_settings.jwt_audience
        self._default_ttl_seconds = active_settings.jwt_access_token_ttl_seconds

    def create_access_token(
        self,
        owner_session_id: str,
        current_time: datetime | None = None,
        ttl_seconds: int | None = None,
    ) -> str:
        """Encode and sign an anonymous access JWT."""
        if (
            not isinstance(owner_session_id, str)
            or not owner_session_id
            or not owner_session_id.startswith("sess_")
        ):
            raise ValueError("Invalid owner_session_id format.")

        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl_seconds
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
            raise ValueError("ttl_seconds must be a positive integer.")

        now = current_time or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)

        iat = int(now.timestamp())
        exp = iat + ttl
        jti = f"jti_{secrets.token_urlsafe(16)}"

        payload = {
            "sub": owner_session_id,
            "iat": iat,
            "exp": exp,
            "jti": jti,
            "type": "anonymous_access",
            "iss": self._issuer,
            "aud": self._audience,
        }

        return jwt.encode(payload, self._secret_bytes, algorithm=self._algorithm)

    def verify_access_token(
        self,
        token: str,
        current_time: datetime | None = None,
    ) -> str:
        """Verify JWT signature, claims, and lifetime. Return owner_session_id if valid."""
        if not token or not isinstance(token, str):
            raise SessionInvalidError("Token must be a non-empty string.")

        try:
            payload = jwt.decode(
                token,
                self._secret_bytes,
                algorithms=[self._algorithm],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": ["sub", "iat", "exp", "jti", "type", "iss", "aud"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except jwt.PyJWTError as exc:
            raise SessionInvalidError("Invalid or expired JWT token.") from exc

        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub.startswith("sess_"):
            raise SessionInvalidError("Invalid subject in token.")

        token_type = payload.get("type")
        if not isinstance(token_type, str) or token_type != "anonymous_access":
            raise SessionInvalidError("Invalid token type.")

        jti = payload.get("jti")
        if not isinstance(jti, str) or not jti:
            raise SessionInvalidError("Invalid JTI in token.")

        # "iss" needs no re-check: jwt.decode already required and matched it.
        # "aud" is re-checked because PyJWT also accepts list-valued audiences.
        aud = payload.get("aud")
        if not isinstance(aud, str) or aud != self._audience:
            raise SessionInvalidError("Invalid audience in token.")

        iat = payload.get("iat")
        if isinstance(iat, bool) or not isinstance(iat, int):
            raise SessionInvalidError("Invalid iat claim in token.")

        exp = payload.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, int):
            raise SessionInvalidError("Invalid exp claim in token.")

        if current_time is not None:
            now = (
                current_time
                if current_time.tzinfo is not None
                else current_time.replace(tzinfo=UTC)
            )
            now_ts = int(now.timestamp())
            if now_ts >= exp or now_ts < iat:
                raise SessionInvalidError("Token expired or not yet valid.")

        return sub
