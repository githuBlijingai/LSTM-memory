from __future__ import annotations

import threading
import time
from collections.abc import Callable
from functools import lru_cache

import redis
import structlog
from fastapi import Depends, Request

from app.core.config import Settings
from app.core.errors import RateLimitedError
from app.core.metrics import record_rate_limited
from app.core.security import CurrentUser

logger = structlog.get_logger(__name__)

_MEMORY_LOCK = threading.Lock()
_MEMORY_COUNTERS: dict[str, tuple[int, float]] = {}
_MAX_MEMORY_ENTRIES = 10_000


def reset_rate_limits() -> None:
    """清空进程内计数，供测试与本地排障使用。"""
    with _MEMORY_LOCK:
        _MEMORY_COUNTERS.clear()


@lru_cache(maxsize=8)
def _redis_client(redis_url: str) -> redis.Redis:
    return redis.Redis.from_url(
        redis_url,
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
    )


def _consume_memory(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    now = time.time()
    expires_at = now + window_seconds
    with _MEMORY_LOCK:
        if len(_MEMORY_COUNTERS) > _MAX_MEMORY_ENTRIES:
            for stale_key, (_, stale_expiry) in list(_MEMORY_COUNTERS.items()):
                if stale_expiry <= now:
                    _MEMORY_COUNTERS.pop(stale_key, None)
        count, expiry = _MEMORY_COUNTERS.get(key, (0, expires_at))
        if expiry <= now:
            count, expiry = 0, expires_at
        count += 1
        _MEMORY_COUNTERS[key] = (count, expiry)
    if count > limit:
        return False, max(1, int(expiry - now))
    return True, 0


def _consume_redis(
    client: redis.Redis, key: str, limit: int, window_seconds: int
) -> tuple[bool, int]:
    pipe = client.pipeline()
    pipe.incr(key)
    pipe.expire(key, window_seconds * 2)
    count, _ = pipe.execute()
    if int(count) > limit:
        ttl = client.ttl(key)
        return False, max(1, int(ttl) if ttl and ttl > 0 else window_seconds)
    return True, 0


def _consume(settings: Settings, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    if settings.redis_url:
        try:
            return _consume_redis(
                _redis_client(settings.redis_url), key, limit, window_seconds
            )
        except (redis.RedisError, OSError) as exc:
            # Redis 不可用时退回进程内计数，避免入口防护拖垮联调。
            logger.warning("rate_limit_redis_unavailable", error_type=type(exc).__name__)
    return _consume_memory(key, limit, window_seconds)


def rate_limit(
    scope: str,
    limit_setting: str,
    window_seconds: int,
    user_dependency: Callable[..., CurrentUser],
):
    """按用户与固定窗口限流；limit 从 Settings 的 limit_setting 字段读取。"""

    def dependency(
        request: Request,
        user: CurrentUser = Depends(user_dependency),
    ) -> None:
        settings: Settings = request.app.state.settings
        if not settings.rate_limit_enabled:
            return
        limit = int(getattr(settings, limit_setting))
        bucket = int(time.time() // window_seconds)
        key = f"{settings.rate_limit_redis_prefix}:{scope}:{user.subject}:{bucket}"
        allowed, retry_after = _consume(settings, key, limit, window_seconds)
        if not allowed:
            record_rate_limited(scope)
            raise RateLimitedError(retry_after)

    return dependency
