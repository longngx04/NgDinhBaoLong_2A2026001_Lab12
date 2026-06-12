"""
Cost Guard — Monthly budget tracking per user.

Supports:
  - Redis-backed (production, stateless)
  - In-memory fallback (development)

Logic:
  - Track spending per user per month
  - Key format: budget:<user_id>:<YYYY-MM>
  - Reject with 402 when budget exceeded
  - Auto-reset each month (new key)
"""
import time
import logging
from datetime import datetime
from collections import defaultdict

from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

# ─── Redis connection (lazy init) ────────────────────────
_redis_client = None
_use_redis = False

# ─── In-memory fallback ─────────────────────────────────
_memory_budgets: dict[str, float] = defaultdict(float)
_budget_reset_month: str = ""


def _init_redis():
    """Try to connect to Redis for budget tracking."""
    global _redis_client, _use_redis
    if settings.redis_url:
        try:
            import redis
            _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
            _redis_client.ping()
            _use_redis = True
            logger.info("Cost guard: using Redis backend")
        except Exception:
            _use_redis = False
            logger.warning("Cost guard: Redis unavailable, using in-memory fallback")
    else:
        logger.info("Cost guard: no REDIS_URL, using in-memory fallback")


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """Estimate cost based on token counts (GPT-4o-mini pricing)."""
    return (input_tokens / 1000) * 0.00015 + (output_tokens / 1000) * 0.0006


def check_budget(user_key: str, estimated_cost: float = 0.0) -> dict:
    """
    Check if user still has budget remaining this month.

    Args:
        user_key: User identifier.

    Returns:
        dict with current spending info.

    Raises:
        HTTPException 402 if monthly budget exceeded.
    """
    global _redis_client
    if _redis_client is None and not _use_redis:
        _init_redis()

    monthly_limit = settings.monthly_budget_usd

    if _use_redis:
        return _check_redis(user_key, monthly_limit, estimated_cost)
    else:
        return _check_memory(user_key, monthly_limit, estimated_cost)


def record_cost(user_key: str, input_tokens: int, output_tokens: int) -> dict:
    """
    Record cost after a successful LLM call.

    Returns:
        dict with updated spending info.
    """
    cost = _estimate_cost(input_tokens, output_tokens)

    if _use_redis:
        return _record_redis(user_key, cost)
    else:
        return _record_memory(user_key, cost)


# ─── Redis implementation ────────────────────────────────

def _check_redis(user_key: str, monthly_limit: float, estimated_cost: float) -> dict:
    month_key = datetime.now().strftime("%Y-%m")
    key = f"budget:{user_key}:{month_key}"

    current = float(_redis_client.get(key) or 0)

    projected = current + estimated_cost
    if projected > monthly_limit:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Monthly budget exceeded. "
                f"Projected: ${projected:.4f} / ${monthly_limit:.2f}"
            ),
        )

    return {
        "current_usd": round(current, 4),
        "projected_usd": round(projected, 4),
        "limit_usd": monthly_limit,
    }


def _record_redis(user_key: str, cost: float) -> dict:
    month_key = datetime.now().strftime("%Y-%m")
    key = f"budget:{user_key}:{month_key}"

    current = float(_redis_client.get(key) or 0)
    if current + cost > settings.monthly_budget_usd:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Monthly budget exceeded. "
                f"Projected: ${current + cost:.4f} / ${settings.monthly_budget_usd:.2f}"
            ),
        )

    new_total = _redis_client.incrbyfloat(key, cost)
    _redis_client.expire(key, 32 * 24 * 3600)  # 32 days TTL

    return {"spent_usd": round(float(new_total), 4)}


# ─── In-memory implementation ────────────────────────────

def _check_memory(user_key: str, monthly_limit: float, estimated_cost: float) -> dict:
    global _budget_reset_month, _memory_budgets

    current_month = datetime.now().strftime("%Y-%m")
    if current_month != _budget_reset_month:
        _memory_budgets.clear()
        _budget_reset_month = current_month

    mem_key = f"{user_key}:{current_month}"
    current = _memory_budgets[mem_key]

    projected = current + estimated_cost
    if projected > monthly_limit:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Monthly budget exceeded. "
                f"Projected: ${projected:.4f} / ${monthly_limit:.2f}"
            ),
        )

    return {
        "current_usd": round(current, 4),
        "projected_usd": round(projected, 4),
        "limit_usd": monthly_limit,
    }


def _record_memory(user_key: str, cost: float) -> dict:
    current_month = datetime.now().strftime("%Y-%m")
    mem_key = f"{user_key}:{current_month}"
    if _memory_budgets[mem_key] + cost > settings.monthly_budget_usd:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Monthly budget exceeded. "
                f"Projected: ${_memory_budgets[mem_key] + cost:.4f} "
                f"/ ${settings.monthly_budget_usd:.2f}"
            ),
        )
    _memory_budgets[mem_key] += cost
    return {"spent_usd": round(_memory_budgets[mem_key], 4)}
