"""JWT encode/decode helpers.

Hardenings over the Blackbox port:

  - **Issuer + audience binding**: every token carries `iss="dwellerd"` and
    `aud="dwellerd-web"`. `decode_access_token` enforces both — a token
    issued for some other surface area (or by a different `dwellerd`
    process accidentally pointed at our DB) won't pass.

  - **JWT ID (`jti`)**: every token gets a fresh UUID4. Required claim,
    needed for revocation lists / audit logging.

  - **Not-before (`nbf`)**: prevents accidental clock-skew use of a
    token before its `iat`. PyJWT validates if the claim is present.

  - **Required-claims enforcement**: decode rejects tokens missing any of
    `exp`, `iat`, `nbf`, `iss`, `aud`, `jti`, `sub` — covers the case of
    a token forged with the right secret but a wrong claim shape.

  - **Single algorithm**: only `HS256` accepted. PyJWT defends against the
    `alg=none` and HS-RS confusion attacks already, but we list it
    explicitly for defense-in-depth.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt

ISSUER = "dwellerd"
AUDIENCE = "dwellerd-web"
ALGORITHM = "HS256"

_REQUIRED_CLAIMS = ("exp", "iat", "nbf", "iss", "aud", "jti", "sub")


def encode_access_token(
    *,
    sub: str,
    role: str,
    sid: str,
    secret: str,
    expiry_seconds: int,
) -> tuple[str, str]:
    """Issue an access JWT. Returns (token, jti) so the caller can record
    the jti on the session row for later revocation.

    `sub`  — username
    `role` — "admin" / "viewer" / future
    `sid`  — opaque session id from the `sessions` table; lets us bind the
             JWT to a server-side row that can be revoked.
    """
    now = datetime.now(tz=timezone.utc)
    jti = str(uuid.uuid4())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": sub,
        "role": role,
        "sid": sid,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expiry_seconds)).timestamp()),
        "jti": jti,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM), jti


def decode_access_token(token: str, secret: str) -> dict:
    """Returns the claims dict on success. Raises:
        - jwt.ExpiredSignatureError       — exp < now
        - jwt.ImmatureSignatureError      — nbf > now
        - jwt.InvalidIssuerError          — iss mismatch
        - jwt.InvalidAudienceError        — aud mismatch
        - jwt.MissingRequiredClaimError   — any of REQUIRED_CLAIMS missing
        - jwt.InvalidTokenError (parent)  — anything else (bad signature,
                                            algorithm confusion, malformed)

    Caller maps these to a 401 with the appropriate detail string.
    """
    return jwt.decode(
        token,
        secret,
        algorithms=[ALGORITHM],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"require": list(_REQUIRED_CLAIMS)},
    )


# ── terminal-unlock tokens (separate audience) ────────────────────────────


# Terminal tokens are JWTs proving "this user just passed PAM" — issued by
# /api/terminal/unlock and consumed once by the /terminal WS namespace.
# Bound to a different audience so a leaked terminal token can't be used
# in place of an access token (and vice versa).
TERMINAL_AUDIENCE = "dwellerd-terminal"


def encode_terminal_token(
    *,
    unix_user: str,
    via_web_user: str,
    uid: int,
    secret: str,
    expiry_seconds: int,
) -> str:
    """Mint a one-shot terminal-unlock token.

    `unix_user`     — the system account the PTY will exec as
    `via_web_user`  — the dashboard user who unlocked (audit trail)
    `uid`           — pre-resolved /etc/passwd uid; saves a getpwnam at
                       socket-connect time
    """
    now = datetime.now(tz=timezone.utc)
    payload = {
        "iss": ISSUER,
        "aud": TERMINAL_AUDIENCE,
        "sub": unix_user,
        "via": via_web_user,
        "uid": uid,
        "kind": "terminal",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expiry_seconds)).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_terminal_token(token: str, secret: str) -> dict:
    """Validates a terminal-unlock token. Strict aud/iss check — an access
    JWT (aud=dwellerd-web) deliberately won't pass."""
    return jwt.decode(
        token,
        secret,
        algorithms=[ALGORITHM],
        audience=TERMINAL_AUDIENCE,
        issuer=ISSUER,
        options={"require": ["exp", "iat", "nbf", "iss", "aud", "jti", "sub", "uid"]},
    )


# ── refresh tokens (opaque) ───────────────────────────────────────────────


# Refresh tokens aren't JWTs — they're opaque random strings the server
# stores hashed (sha256, not bcrypt: this is a high-entropy bearer secret,
# not a low-entropy password, and we need fast O(1) lookup against the
# table). The plaintext refresh string only ever lives client-side
# (httpOnly cookie); the DB row holds only the hash.

_REFRESH_BYTES = 32  # 256 bits of entropy


def generate_refresh_token() -> str:
    """Return a fresh URL-safe refresh string. 256 bits of entropy is well
    over what's needed; we'd accept 128 bits but bumping the size costs
    nothing and gives a wider safety margin."""
    return secrets.token_urlsafe(_REFRESH_BYTES)


def hash_refresh_token(plain: str) -> str:
    """SHA-256 of the refresh string, hex-encoded. Used for the `sessions`
    table lookup — equivalent of password hash for opaque bearer tokens.

    Why SHA-256 and not bcrypt: refresh tokens carry full entropy already
    (256 random bits via `secrets.token_urlsafe`), so a slow KDF buys
    nothing — there's no dictionary to attack. Fast hash also lets us do
    O(1) table lookup by hash on every refresh call without burning CPU.
    """
    import hashlib
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()
