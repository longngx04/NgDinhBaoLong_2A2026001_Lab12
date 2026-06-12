"""
Rate Limiter — Sliding Window algorithm.

Supports:
  - Redis-backed (production, stateless)
  - In-memory fallback (development)

Algorithm: Sliding Window using sorted sets.
  - Each request timestamp is stored
  - Old entries beyond the window are pruned
  - If count >= limit → reject with 429
"""
import time
import logging
from collections import defaultdict, deque

from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

# ─── Redis connection (lazy init) ────────────────────────
_redis_client = None
_use_redis = False


def _init_redis():
    """Try to connect to Redis for rate limiting."""
    global _redis_client, _use_redis
    if settings.redis_url:
        try:
            import redis
            _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
            _redis_client.ping()
            _use_redis = True
            logger.info("Rate limiter: using Redis backend")
        except Exception:
            _use_redis = False
            logger.warning("Rate limiter: Redis unavailable, using in-memory fallback")
    else:
        logger.info("Rate limiter: no REDIS_URL, using in-memory fallback")


# ─── In-memory fallback ─────────────────────────────────
_memory_windows: dict[str, deque] = defaultdict(deque)


def check_rate_limit(user_key: str) -> dict:
    """
    Check rate limit for a user.

    Args:
        user_key: Unique identifier for the user (e.g., API key prefix).

    Returns:
        dict with 'remaining' count.

    Raises:
        HTTPException 429 if limit exceeded.
    """
    # Lazy init Redis on first call
    global _redis_client
    if _redis_client is None and not _use_redis:
        _init_redis()

    limit = settings.rate_limit_per_minute
    window = 60  # seconds

    if _use_redis:
        return _check_redis(user_key, limit, window)
    else:
        return _check_memory(user_key, limit, window)


def _check_redis(user_key: str, limit: int, window: int) -> dict:
    """Redis-backed sliding window rate limiter."""
    now = time.time()
    key = f"rate:{user_key}"

    pipe = _redis_client.pipeline()
    pipe.zremrangebyscore(key, 0, now - window)  # remove old entries
    pipe.zcard(key)                              # count current
    pipe.zadd(key, {str(now): now})              # add new entry
    pipe.expire(key, window)                     # set TTL
    results = pipe.execute()

    current_count = results[1]

    if current_count >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {limit} req/min. Try again later.",
            headers={"Retry-After": "60"},
        )

    remaining = limit - current_count - 1
    return {"remaining": max(remaining, 0)}


def _check_memory(user_key: str, limit: int, window: int) -> dict:
    """In-memory sliding window rate limiter (fallback)."""
    now = time.time()
    dq = _memory_windows[user_key]

    # Remove old entries
    while dq and dq[0] < now - window:
        dq.popleft()

    if len(dq) >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {limit} req/min. Try again later.",
            headers={"Retry-After": "60"},
        )

    dq.append(now)
    remaining = limit - len(dq)
    return {"remaining": max(remaining, 0)}
