"""Password hashing — bcrypt directly.

Plain passwords never persist; only the bcrypt hash lives in `users.password_hash`.

Hardenings over the Blackbox port:

  - `verify_password_constant_time(plain, hashed_or_none)` — always runs a
    bcrypt check, even when the user lookup returned None. Without this, an
    attacker can timing-distinguish "user exists" from "user doesn't" because
    the existing-user path runs ~250 ms of bcrypt work and the missing-user
    path returns instantly. The dummy hash here is generated at import time
    against a random throwaway password so it can't be precomputed.

  - Explicit `bcrypt.gensalt(rounds=12)` for clarity. 12 rounds = ~250 ms on
    modern CPUs; bump to 13 if your hardware tolerates the latency.
"""
from __future__ import annotations

import bcrypt

_ROUNDS = 12

# Generated once per process. The password is throwaway random bytes —
# nobody knows it, nothing checks against it; we use the resulting hash
# only as the "negative path" target so verify timing matches the real
# user-exists path to within ms.
_DUMMY_HASH = bcrypt.hashpw(
    bcrypt.gensalt(rounds=4).hex().encode("utf-8"),  # cheap input
    bcrypt.gensalt(rounds=_ROUNDS),
).decode("utf-8")


def hash_password(plain: str) -> str:
    """Hash a plaintext password for storage. bcrypt has a 72-byte input
    limit — longer passwords get silently truncated. The login endpoint
    rejects > 128 chars at the schema layer, so this caps at the same."""
    if len(plain) > 128:
        raise ValueError("password too long (max 128 chars)")
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=_ROUNDS)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def verify_password_constant_time(plain: str, hashed: str | None) -> bool:
    """Login-flow variant: when `hashed` is None (user not found / disabled),
    we still run a real bcrypt check against a dummy hash so the response
    timing is indistinguishable from the real path. Returns False either way.
    """
    target = hashed or _DUMMY_HASH
    ok = verify_password(plain, target)
    # When hashed was None we forced a dummy compare — discard its result.
    if hashed is None:
        return False
    return ok
