"""Offline unit tests for session security module."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from sourcetrace.core.config import Settings
from sourcetrace.core.exceptions import SessionConfigurationError
from sourcetrace.core.security import (
    SessionSigner,
    generate_owner_session_id,
)

VALID_SECRET = "a_very_secret_key_that_is_at_least_32_bytes_long!"


def test_opaque_ids_are_unique_and_cryptographically_random() -> None:
    ids = [generate_owner_session_id() for _ in range(100)]
    assert len(set(ids)) == 100
    for sid in ids:
        assert sid.startswith("sess_")
        assert len(sid) > 20


def test_valid_signed_cookie_restores_only_its_signed_owner_id() -> None:
    signer = SessionSigner(secret=VALID_SECRET)
    exp = datetime.now(UTC) + timedelta(days=7)
    token = signer.create_cookie_token("sess_valid123", exp)

    restored_id = signer.verify_cookie_token(token)
    assert restored_id == "sess_valid123"


def test_tampering_one_cookie_byte_invalidates_it() -> None:
    signer = SessionSigner(secret=VALID_SECRET)
    exp = datetime.now(UTC) + timedelta(days=7)
    token = signer.create_cookie_token("sess_valid123", exp)

    # Tamper one byte in the token signature
    char_list = list(token)
    char_list[-1] = "a" if char_list[-1] != "a" else "b"
    tampered_token = "".join(char_list)

    assert signer.verify_cookie_token(tampered_token) is None


def test_expired_malformed_unknown_version_cookies_rejected() -> None:
    signer = SessionSigner(secret=VALID_SECRET)
    now = datetime.now(UTC)

    # Expired cookie
    past = now - timedelta(seconds=10)
    expired_token = signer.create_cookie_token("sess_expired", past)
    assert signer.verify_cookie_token(expired_token) is None

    # Unknown version
    token_v2 = f"v2.sess_valid123.{int(now.timestamp())}.fakesig"
    assert signer.verify_cookie_token(token_v2) is None

    # Malformed formats
    assert signer.verify_cookie_token("") is None
    assert signer.verify_cookie_token("invalid_token") is None
    assert signer.verify_cookie_token("v1.sess_123") is None
    assert signer.verify_cookie_token("v1.sess_123.not_an_int.sig") is None


def test_no_signing_secret_exposed_in_exceptions_or_responses() -> None:
    secret_value = "too_short_key"
    with pytest.raises(SessionConfigurationError) as exc_info:
        SessionSigner(secret=secret_value)

    err_msg = str(exc_info.value)
    assert secret_value not in err_msg
    assert "Session signing secret is not configured" in err_msg


def test_missing_signing_secret_fails_only_when_session_service_used() -> None:
    # Settings import and initialization work without session_signing_secret
    settings = Settings(_env_file=None, session_signing_secret=None)
    assert settings.session_signing_secret is None

    # Error happens only when SessionSigner is constructed with missing/empty secret
    with pytest.raises(SessionConfigurationError):
        SessionSigner(secret=SecretStr(""))


def test_exact_expiration_boundary_rejected() -> None:
    signer = SessionSigner(secret=VALID_SECRET)
    exp = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    token = signer.create_cookie_token("sess_exact_boundary", exp)

    # Current time exactly equal to exp timestamp must be rejected
    assert signer.verify_cookie_token(token, current_time=exp) is None
    # Current time 1 second after exp timestamp must be rejected
    assert (
        signer.verify_cookie_token(token, current_time=exp + timedelta(seconds=1))
        is None
    )
    # Current time 1 second before exp timestamp is valid
    assert (
        signer.verify_cookie_token(token, current_time=exp - timedelta(seconds=1))
        == "sess_exact_boundary"
    )
