"""Password hashing and signed cookies, all stdlib.

Two jobs: turn a password into something safe to store, and turn a user id into a
tamper-evident cookie value. No sessions table — the signature is the proof.
"""

import base64
import hashlib
import hmac
import os
import secrets
import time

from src.constants.constants import (
    COOKIE_MAX_AGE,
    SCRYPT_BLOCK,
    SCRYPT_COST,
    SCRYPT_DKLEN,
    SCRYPT_PARALLEL,
    SCRYPT_SALT_BYTES,
)


def _b64(raw: bytes) -> str:
    """URL-safe base64 with the padding stripped, so it is cookie- and path-safe."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """Return 'scrypt$n$r$p$salt$hash'. The parameters travel with the hash.

    Storing them means a future cost increase can rehash on next login instead of
    invalidating every existing password.
    """
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_COST, SCRYPT_BLOCK, SCRYPT_PARALLEL)
    return f"scrypt${SCRYPT_COST}${SCRYPT_BLOCK}${SCRYPT_PARALLEL}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a hash from `hash_password`."""
    try:
        scheme, cost, block, parallel, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _scrypt(password, _unb64(salt), int(cost), int(block), int(parallel))
    except ValueError, TypeError:
        return False  # malformed hash is a failed login, never a crash
    return hmac.compare_digest(expected, _unb64(digest))


def _scrypt(password: str, salt: bytes, cost: int, block: int, parallel: int) -> bytes:
    # OpenSSL caps scrypt memory at 32 MB by default, which rejects n >= 2**15.
    # 128 * r * n is the real requirement, so ask for exactly that.
    return hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=cost,
        r=block,
        p=parallel,
        dklen=SCRYPT_DKLEN,
        maxmem=128 * block * cost * 2,
    )


def _secret() -> bytes:
    """The cookie signing key. A missing one is a hard error, never a default.

    A guessable key means anyone can mint a session for any user id.
    """
    key = os.getenv("SECRET_KEY")
    if not key:
        raise RuntimeError(
            "SECRET_KEY is not set. Add a random value to .env.local — "
            "`python -c 'import secrets; print(secrets.token_urlsafe(32))'`"
        )
    return key.encode()


def sign(user_id: int, now: int | None = None) -> str:
    """Return 'user_id.issued_at.signature' for a session cookie."""
    payload = f"{user_id}.{int(now if now is not None else time.time())}"
    return f"{payload}.{_b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())}"


def unsign(value: str, now: int | None = None) -> int | None:
    """Return the user id if the signature holds and the cookie is fresh, else None."""
    try:
        user_id, issued, signature = value.split(".")
        payload = f"{user_id}.{issued}"
        expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
        # Compare the encoded form: base64 has slack bits, so two different strings can
        # decode to the same digest and a tampered last character would slip through.
        if not hmac.compare_digest(_b64(expected), signature):
            return None
        age = int(now if now is not None else time.time()) - int(issued)
        if not 0 <= age <= COOKIE_MAX_AGE:  # a future issue date is as wrong as an expired one
            return None
        return int(user_id)
    except ValueError, TypeError:
        return None


def demo():
    """Self-check: hashing, verification, signing and every way a cookie can be wrong."""
    os.environ.setdefault("SECRET_KEY", "test-only-key")

    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse", stored)
    assert not verify_password("Correct horse", stored)
    assert not verify_password("", stored)
    # Same password twice must differ: the salt is what stops a rainbow table.
    assert hash_password("x") != hash_password("x")
    for junk in ("", "nonsense", "scrypt$bad$8$1$aa$bb", "argon2$1$2$3$aa$bb", "a$b$c$d$e$f"):
        assert not verify_password("x", junk), junk

    token = sign(42)
    assert unsign(token) == 42
    assert unsign(token[:-1] + ("A" if token[-1] != "A" else "B")) is None  # bad signature
    assert unsign("42.0.deadbeef") is None
    assert unsign("nonsense") is None
    assert unsign(sign(42, now=1000), now=1000 + COOKIE_MAX_AGE + 1) is None  # expired
    assert unsign(sign(42, now=2000), now=1000) is None  # issued in the future
    assert unsign(sign(42, now=1000), now=1000 + COOKIE_MAX_AGE) == 42  # exactly at the edge

    # A different key must not validate another key's cookie.
    os.environ["SECRET_KEY"] = "a-different-key"
    assert unsign(token) is None
    os.environ["SECRET_KEY"] = "test-only-key"

    print("ok — passwords hash and verify, cookies reject tampering, expiry and key changes")


if __name__ == "__main__":
    demo()
