"""Authentication: callsign + API key, presented as a bearer token.

Keys are generated from the platform entropy source and only ever stored
hashed. Repeated failures from one source address are slowed down and land in
the audit trail.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..models import User

_bearer = HTTPBearer(auto_error=False)

THROTTLE_BASE_DELAY = 0.5
THROTTLE_MAX_DELAY = 8.0
THROTTLE_WINDOW_S = 900.0


def generate_api_key() -> str:
    """A fresh key from the OS entropy source (BCryptGenRandom on Windows)."""
    return secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


class AuthThrottle:
    """Exponential slow-down for repeated bad keys from the same address."""

    def __init__(self) -> None:
        self._failures: dict[str, tuple[int, float]] = {}

    def record_failure(self, source: str) -> int:
        count, first_seen = self._failures.get(source, (0, time.monotonic()))
        if time.monotonic() - first_seen > THROTTLE_WINDOW_S:
            count, first_seen = 0, time.monotonic()
        count += 1
        self._failures[source] = (count, first_seen)
        return count

    def clear(self, source: str) -> None:
        self._failures.pop(source, None)

    def delay_for(self, source: str) -> float:
        count = self._failures.get(source, (0, 0.0))[0]
        if count <= 1:
            return 0.0
        return min(THROTTLE_BASE_DELAY * (2 ** (count - 2)), THROTTLE_MAX_DELAY)


def client_address(request: Request) -> str | None:
    return request.client.host if request.client else None


def _source_of(request: Request) -> str:
    return client_address(request) or "unknown"


async def require_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    """Resolve the caller from their bearer API key."""
    manager = request.app.state.manager
    source = _source_of(request)

    async def reject(reason: str) -> HTTPException:
        count = manager.throttle.record_failure(source)
        manager.db.audit(source, "auth.failed", f"{reason} (attempt {count})")
        manager.logger.warning("auth failed from %s: %s (attempt %d)", source, reason, count)
        await asyncio.sleep(manager.throttle.delay_for(source))
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials is None or not credentials.credentials:
        raise await reject("no bearer credentials")

    user = manager.db.get_user_by_key_hash(hash_api_key(credentials.credentials))
    if user is None:
        raise await reject("unknown API key")
    if not user.enabled:
        raise await reject(f"disabled account {user.callsign}")

    manager.throttle.clear(source)
    return user


async def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin privileges required")
    return user
