"""
Authentication module — API Key verification.

Tách riêng auth logic cho clean separation of concerns.
Supports API Key via X-API-Key header.
"""
from fastapi import HTTPException, Security
from fastapi.security.api_key import APIKeyHeader

from app.config import settings

# ─── API Key Header ──────────────────────────────────────
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(api_key: str = Security(api_key_header)) -> str:
    """
    FastAPI dependency: verify X-API-Key header.

    Returns:
        str: The validated API key (used as user identifier for rate limiting).

    Raises:
        HTTPException 401: If key is missing or invalid.
    """
    if not api_key or api_key != settings.agent_api_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Include header: X-API-Key: <key>",
        )
    return api_key
