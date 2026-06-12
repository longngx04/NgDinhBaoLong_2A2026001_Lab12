"""
Production AI Agent — Kết hợp tất cả Day 12 concepts

Checklist:
  ✅ Config từ environment (12-factor)
  ✅ Structured JSON logging
  ✅ API Key authentication (app/auth.py)
  ✅ Rate limiting (app/rate_limiter.py)
  ✅ Cost guard (app/cost_guard.py)
  ✅ Input validation (Pydantic)
  ✅ Health check + Readiness probe
  ✅ Graceful shutdown
  ✅ Security headers
  ✅ CORS
  ✅ Error handling
  ✅ Conversation history (Redis-backed, stateless)
  ✅ Stateless design — all state in Redis
"""
import os
import time
import signal
import logging
import json
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from app.config import settings
from app.auth import verify_api_key
from app.rate_limiter import check_rate_limit
from app.cost_guard import check_budget, record_cost

# Mock LLM (thay bằng OpenAI/Anthropic khi có API key)
from utils.mock_llm import ask as llm_ask

# ─────────────────────────────────────────────────────────
# Logging — JSON structured
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)

START_TIME = time.time()
_is_ready = False
_request_count = 0
_error_count = 0

# ─────────────────────────────────────────────────────────
# Redis connection for conversation history (stateless)
# ─────────────────────────────────────────────────────────
_redis = None
_use_redis = False


def _init_redis():
    """Initialize Redis connection for stateless conversation history."""
    global _redis, _use_redis
    if settings.redis_url:
        try:
            import redis as redis_lib
            _redis = redis_lib.from_url(
                settings.redis_url, decode_responses=True
            )
            _redis.ping()
            _use_redis = True
            logger.info("Connected to Redis — stateless mode ✅")
        except Exception as e:
            _use_redis = False
            logger.warning(f"Redis unavailable ({e}) — using in-memory fallback ⚠️")
    else:
        logger.warning("REDIS_URL not set — using in-memory fallback ⚠️")


# In-memory fallback for conversation history (NOT scalable)
_memory_history: dict[str, list] = {}


def get_conversation_history(user_id: str) -> list[dict]:
    """Load conversation history from Redis (or memory fallback)."""
    if _use_redis:
        raw = _redis.lrange(f"history:{user_id}", 0, -1)
        return [json.loads(item) for item in raw]
    return _memory_history.get(user_id, [])


def save_to_history(user_id: str, role: str, content: str):
    """Append message to conversation history in Redis (or memory fallback)."""
    entry = json.dumps({
        "role": role,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    if _use_redis:
        _redis.rpush(f"history:{user_id}", entry)
        # Keep last 20 messages (10 turns)
        _redis.ltrim(f"history:{user_id}", -20, -1)
        # TTL: 1 hour
        _redis.expire(f"history:{user_id}", 3600)
    else:
        if user_id not in _memory_history:
            _memory_history[user_id] = []
        _memory_history[user_id].append(json.loads(entry))
        # Keep last 20
        if len(_memory_history[user_id]) > 20:
            _memory_history[user_id] = _memory_history[user_id][-20:]


# ─────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _is_ready
    logger.info(json.dumps({
        "event": "startup",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
    }))

    # Initialize Redis
    _init_redis()

    time.sleep(0.1)  # simulate init
    _is_ready = True
    logger.info(json.dumps({"event": "ready"}))

    yield

    _is_ready = False
    logger.info(json.dumps({"event": "shutdown", "msg": "Graceful shutdown initiated"}))
    # Close Redis connection
    if _redis:
        try:
            _redis.close()
        except Exception:
            pass
    logger.info(json.dumps({"event": "shutdown_complete"}))


# ─────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    global _request_count, _error_count
    start = time.time()
    _request_count += 1
    try:
        response: Response = await call_next(request)
        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        if "server" in response.headers:
            del response.headers["server"]
        duration = round((time.time() - start) * 1000, 1)
        logger.info(json.dumps({
            "event": "request",
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "ms": duration,
        }))
        return response
    except Exception:
        _error_count += 1
        raise


# ─────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000,
                          description="Your question for the agent")
    user_id: str = Field(default="anonymous", max_length=100,
                         description="User identifier for conversation tracking")


class AskResponse(BaseModel):
    question: str
    answer: str
    user_id: str
    model: str
    timestamp: str
    conversation_turns: int


# ─────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────

@app.get("/", tags=["Info"])
def root():
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "endpoints": {
            "ask": "POST /ask (requires X-API-Key)",
            "health": "GET /health",
            "ready": "GET /ready",
        },
    }


@app.post("/ask", response_model=AskResponse, tags=["Agent"])
async def ask_agent(
    body: AskRequest,
    request: Request,
    _key: str = Depends(verify_api_key),
):
    """
    Send a question to the AI agent.

    **Authentication:** Include header `X-API-Key: <your-key>`

    Features:
    - Conversation history tracked per user_id
    - Rate limited per API key
    - Cost guard per API key
    """
    user_key = _key[:8]  # use first 8 chars as key bucket

    # ── Rate limit ──
    rate_info = check_rate_limit(user_key)

    input_tokens = len(body.question.split()) * 2

    # Budget check before the LLM call. The final response cost is checked again
    # when recorded because output length is only known after generation.
    check_budget(user_key, estimated_cost=(input_tokens / 1000) * 0.00015)

    logger.info(json.dumps({
        "event": "agent_call",
        "user_id": body.user_id,
        "q_len": len(body.question),
        "client": str(request.client.host) if request.client else "unknown",
    }))

    # ── Save user question to history ──
    save_to_history(body.user_id, "user", body.question)

    # ── Call LLM (mock) ──
    answer = llm_ask(body.question)

    # ── Save answer to history ──
    save_to_history(body.user_id, "assistant", answer)

    # ── Record cost ──
    output_tokens = len(answer.split()) * 2
    record_cost(user_key, input_tokens, output_tokens)

    # ── Get conversation turn count ──
    history = get_conversation_history(body.user_id)
    turns = len([m for m in history if m["role"] == "user"])

    return AskResponse(
        question=body.question,
        answer=answer,
        user_id=body.user_id,
        model=settings.llm_model,
        timestamp=datetime.now(timezone.utc).isoformat(),
        conversation_turns=turns,
    )


@app.get("/history/{user_id}", tags=["Agent"])
def get_history(user_id: str, _key: str = Depends(verify_api_key)):
    """Get conversation history for a user."""
    history = get_conversation_history(user_id)
    return {
        "user_id": user_id,
        "messages": history,
        "count": len(history),
    }


@app.get("/health", tags=["Operations"])
def health():
    """Liveness probe. Platform restarts container if this fails."""
    status = "ok"
    checks = {
        "llm": "mock" if not settings.openai_api_key else "openai",
        "redis": "connected" if _use_redis else "in-memory-fallback",
    }

    # Check Redis health if connected
    if _use_redis:
        try:
            _redis.ping()
            checks["redis"] = "connected"
        except Exception:
            checks["redis"] = "disconnected"
            status = "degraded"

    return {
        "status": status,
        "version": settings.app_version,
        "environment": settings.environment,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/ready", tags=["Operations"])
def ready():
    """Readiness probe. Load balancer stops routing here if not ready."""
    if not _is_ready:
        raise HTTPException(503, "Not ready")

    # Check Redis if configured
    if _use_redis:
        try:
            _redis.ping()
        except Exception:
            raise HTTPException(503, "Redis not available")

    return {"ready": True}


@app.get("/metrics", tags=["Operations"])
def metrics(_key: str = Depends(verify_api_key)):
    """Basic metrics (protected)."""
    return {
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "error_count": _error_count,
        "storage_backend": "redis" if _use_redis else "in-memory",
    }


# ─────────────────────────────────────────────────────────
# Graceful Shutdown
# ─────────────────────────────────────────────────────────
def _handle_signal(signum, _frame):
    logger.info(json.dumps({"event": "signal_received", "signum": signum}))


signal.signal(signal.SIGTERM, _handle_signal)


if __name__ == "__main__":
    logger.info(f"Starting {settings.app_name} on {settings.host}:{settings.port}")
    logger.info(f"API Key: {settings.agent_api_key[:4]}****")
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        timeout_graceful_shutdown=30,
    )
