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


# ---------------------------------------------------------------------------
# JWT Security Unit Tests
# ---------------------------------------------------------------------------


def test_jwt_valid_token_round_trip_returns_original_owner_id() -> None:
    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    token = signer.create_access_token("sess_jwt_valid_123")
    assert isinstance(token, str)

    restored_id = signer.verify_access_token(token)
    assert restored_id == "sess_jwt_valid_123"


def test_jwt_token_contains_all_required_claims() -> None:
    import jwt

    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    now = datetime.now(UTC)
    token = signer.create_access_token("sess_claims_test", current_time=now)

    unverified = jwt.decode(token, options={"verify_signature": False})
    assert unverified["sub"] == "sess_claims_test"
    assert unverified["iss"] == "sourcetrace"
    assert unverified["aud"] == "sourcetrace-api"
    assert unverified["type"] == "anonymous_access"
    assert unverified["jti"].startswith("jti_")
    assert unverified["iat"] == int(now.timestamp())
    assert unverified["exp"] == int(now.timestamp()) + 604800


def test_jwt_exp_derived_from_configured_ttl() -> None:
    import jwt

    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    now = datetime.now(UTC)
    token = signer.create_access_token("sess_ttl_test", current_time=now, ttl_seconds=3600)

    unverified = jwt.decode(token, options={"verify_signature": False})
    assert unverified["exp"] == int(now.timestamp()) + 3600


def test_jwt_two_issued_tokens_have_different_jti() -> None:
    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    token1 = signer.create_access_token("sess_jti_test")
    token2 = signer.create_access_token("sess_jti_test")

    import jwt

    u1 = jwt.decode(token1, options={"verify_signature": False})
    u2 = jwt.decode(token2, options={"verify_signature": False})
    assert u1["jti"] != u2["jti"]


def test_jwt_missing_secret_rejected() -> None:
    from sourcetrace.core.security import JWTSigner

    with pytest.raises(SessionConfigurationError) as exc_info:
        JWTSigner(secret="")
    assert "JWT signing secret is not configured" in str(exc_info.value)


def test_jwt_secret_shorter_than_32_bytes_rejected() -> None:
    from sourcetrace.core.security import JWTSigner

    short_secret = "short_key_under_32_bytes"
    with pytest.raises(SessionConfigurationError) as exc_info:
        JWTSigner(secret=short_secret)
    assert short_secret not in str(exc_info.value)


def test_jwt_invalid_subject_prefix_rejected() -> None:
    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    with pytest.raises(ValueError):
        signer.create_access_token("invalid_prefix_123")


def test_jwt_malformed_token_rejected() -> None:
    from sourcetrace.core.exceptions import SessionInvalidError
    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    with pytest.raises(SessionInvalidError):
        signer.verify_access_token("not.a.valid.jwt")


def test_jwt_tampered_token_rejected() -> None:
    from sourcetrace.core.exceptions import SessionInvalidError
    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    token = signer.create_access_token("sess_tamper_test")
    tampered = token[:-4] + "ffff"

    with pytest.raises(SessionInvalidError):
        signer.verify_access_token(tampered)


def test_jwt_token_signed_with_another_secret_rejected() -> None:
    from sourcetrace.core.exceptions import SessionInvalidError
    from sourcetrace.core.security import JWTSigner

    signer1 = JWTSigner(secret=VALID_SECRET)
    signer2 = JWTSigner(secret="another_secret_key_that_is_32_bytes_long!!")

    token = signer1.create_access_token("sess_secret_test")
    with pytest.raises(SessionInvalidError):
        signer2.verify_access_token(token)


def test_jwt_expired_token_rejected() -> None:
    from sourcetrace.core.exceptions import SessionInvalidError
    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    now = datetime.now(UTC)
    past = now - timedelta(days=8)
    token = signer.create_access_token("sess_expired_test", current_time=past, ttl_seconds=3600)

    with pytest.raises(SessionInvalidError):
        signer.verify_access_token(token, current_time=now)


def test_jwt_missing_required_claims_rejected() -> None:
    import jwt

    from sourcetrace.core.exceptions import SessionInvalidError
    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    now = datetime.now(UTC)

    # Payload missing 'type' claim
    incomplete_payload = {
        "sub": "sess_incomplete",
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 3600,
        "jti": "jti_12345",
        "iss": "sourcetrace",
        "aud": "sourcetrace-api",
    }
    raw_token = jwt.encode(incomplete_payload, VALID_SECRET, algorithm="HS256")

    with pytest.raises(SessionInvalidError):
        signer.verify_access_token(raw_token)


def test_jwt_wrong_issuer_rejected() -> None:
    import jwt

    from sourcetrace.core.exceptions import SessionInvalidError
    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    now = datetime.now(UTC)
    payload = {
        "sub": "sess_wrong_iss",
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 3600,
        "jti": "jti_12345",
        "type": "anonymous_access",
        "iss": "wrong_issuer",
        "aud": "sourcetrace-api",
    }
    raw_token = jwt.encode(payload, VALID_SECRET, algorithm="HS256")

    with pytest.raises(SessionInvalidError):
        signer.verify_access_token(raw_token)


def test_jwt_wrong_audience_rejected() -> None:
    import jwt

    from sourcetrace.core.exceptions import SessionInvalidError
    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    now = datetime.now(UTC)
    payload = {
        "sub": "sess_wrong_aud",
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 3600,
        "jti": "jti_12345",
        "type": "anonymous_access",
        "iss": "sourcetrace",
        "aud": "wrong-api",
    }
    raw_token = jwt.encode(payload, VALID_SECRET, algorithm="HS256")

    with pytest.raises(SessionInvalidError):
        signer.verify_access_token(raw_token)


def test_jwt_wrong_token_type_rejected() -> None:
    import jwt

    from sourcetrace.core.exceptions import SessionInvalidError
    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    now = datetime.now(UTC)
    payload = {
        "sub": "sess_wrong_type",
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 3600,
        "jti": "jti_12345",
        "type": "user_access",
        "iss": "sourcetrace",
        "aud": "sourcetrace-api",
    }
    raw_token = jwt.encode(payload, VALID_SECRET, algorithm="HS256")

    with pytest.raises(SessionInvalidError):
        signer.verify_access_token(raw_token)


def test_jwt_invalid_subject_type_rejected() -> None:
    import jwt

    from sourcetrace.core.exceptions import SessionInvalidError
    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    now = datetime.now(UTC)
    payload = {
        "sub": 12345,  # non-string subject
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 3600,
        "jti": "jti_12345",
        "type": "anonymous_access",
        "iss": "sourcetrace",
        "aud": "sourcetrace-api",
    }
    raw_token = jwt.encode(payload, VALID_SECRET, algorithm="HS256")

    with pytest.raises(SessionInvalidError):
        signer.verify_access_token(raw_token)


def test_jwt_empty_jti_rejected() -> None:
    import jwt

    from sourcetrace.core.exceptions import SessionInvalidError
    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    now = datetime.now(UTC)
    payload = {
        "sub": "sess_empty_jti",
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 3600,
        "jti": "",  # empty string
        "type": "anonymous_access",
        "iss": "sourcetrace",
        "aud": "sourcetrace-api",
    }
    raw_token = jwt.encode(payload, VALID_SECRET, algorithm="HS256")

    with pytest.raises(SessionInvalidError):
        signer.verify_access_token(raw_token)


def test_jwt_algorithm_confusion_or_unsupported_algorithm_rejected() -> None:
    import jwt

    from sourcetrace.core.exceptions import SessionInvalidError
    from sourcetrace.core.security import JWTSigner

    signer = JWTSigner(secret=VALID_SECRET)
    now = datetime.now(UTC)
    payload = {
        "sub": "sess_algo_test",
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 3600,
        "jti": "jti_12345",
        "type": "anonymous_access",
        "iss": "sourcetrace",
        "aud": "sourcetrace-api",
    }
    # Encode token with algorithm 'none'
    raw_token = jwt.encode(payload, "", algorithm="none")

    with pytest.raises(SessionInvalidError):
        signer.verify_access_token(raw_token)
