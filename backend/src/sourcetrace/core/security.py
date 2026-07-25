"""Session security primitives for anonymous browser sessions."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from pydantic import SecretStr

from sourcetrace.core.config import get_settings
from sourcetrace.core.exceptions import SessionConfigurationError

COOKIE_VERSION = "v1"
SESSION_INACTIVITY_DAYS = 7
SESSION_MAX_AGE_SECONDS = SESSION_INACTIVITY_DAYS * 24 * 60 * 60  # 604,800 seconds
MIN_SECRET_LENGTH = 32


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
        raw_secret: str | None = None
        if isinstance(secret, SecretStr):
            raw_secret = secret.get_secret_value()
        elif isinstance(secret, str):
            raw_secret = secret
        elif secret is None:
            settings = get_settings()
            if settings.session_signing_secret is not None:
                raw_secret = settings.session_signing_secret.get_secret_value()

        if not raw_secret or len(raw_secret) < MIN_SECRET_LENGTH:
            raise SessionConfigurationError(
                "Session signing secret is not configured or is too short."
            )

        self._secret_bytes = raw_secret.encode("utf-8")

    def create_cookie_token(
        self, owner_session_id: str, expires_at: datetime
    ) -> str:
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
