import hashlib
import hmac

from app.security import verify_signature

SECRET = "test-secret"


def _make_sig(payload: bytes) -> str:
    digest = hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature():
    payload = b'{"action": "opened"}'
    assert verify_signature(payload, _make_sig(payload), SECRET)


def test_invalid_signature():
    payload = b'{"action": "opened"}'
    assert not verify_signature(payload, "sha256=badhash", SECRET)


def test_missing_signature():
    assert not verify_signature(b"payload", None, SECRET)


def test_wrong_prefix():
    assert not verify_signature(b"payload", "md5=somehash", SECRET)


def test_tampered_payload():
    original = b'{"action": "opened"}'
    sig = _make_sig(original)
    tampered = b'{"action": "closed"}'
    assert not verify_signature(tampered, sig, SECRET)
