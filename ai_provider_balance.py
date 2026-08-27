"""
AI provider balance abstraction.

Returns balance info without exposing API keys.
"""

import logging
import os

import httpx

log = logging.getLogger("polyana.ai_balance")


async def get_ai_provider_balance() -> dict:
    """
    Get current AI provider balance.

    Returns:
        {
            "provider": str,
            "balance": float | None,
            "currency": "USD",
            "available": bool,
        }
    """
    # Try providers in order of preference
    providers = [
        _try_qwen_balance,
        _try_openrouter_balance,
        _try_mimo_balance,
    ]

    for provider_fn in providers:
        try:
            result = await provider_fn()
            if result and result.get("available"):
                return result
        except Exception as e:
            log.debug("Provider balance check failed: %s", e)

    return {
        "provider": "unknown",
        "balance": None,
        "currency": "USD",
        "available": False,
    }


async def _try_qwen_balance() -> dict | None:
    """Try to get Qwen/DashScope balance."""
    api_key = _get_env("QWEN_API_KEY")
    if not api_key:
        return None

    # DashScope doesn't have a direct balance API in all plans
    # Return unavailable for now — can be extended when API is available
    return {
        "provider": "qwen",
        "balance": None,
        "currency": "USD",
        "available": False,
    }


async def _try_openrouter_balance() -> dict | None:
    """Try to get OpenRouter balance."""
    api_key = _get_env("OPENROUTER_API_KEY")
    if not api_key:
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if r.status_code == 200:
                data = r.json().get("data", {})
                balance = data.get("usage")
                limit = data.get("limit")
                if limit is not None and balance is not None:
                    remaining = float(limit) - float(balance)
                    return {
                        "provider": "openrouter",
                        "balance": round(remaining, 2),
                        "currency": "USD",
                        "available": True,
                    }
    except Exception as e:
        log.debug("OpenRouter balance check failed: %s", e)

    return {
        "provider": "openrouter",
        "balance": None,
        "currency": "USD",
        "available": False,
    }


async def _try_mimo_balance() -> dict | None:
    """Try to get MiMo balance."""
    api_key = _get_env("MIMO_API_KEY")
    if not api_key:
        return None

    # MiMo API doesn't expose balance endpoint
    return {
        "provider": "mimo",
        "balance": None,
        "currency": "USD",
        "available": False,
    }


def _get_env(key: str) -> str | None:
    """Get env var from environment or /etc/polyana/env."""
    val = os.environ.get(key)
    if val:
        return val
    try:
        with open("/etc/polyana/env") as f:
            for line in f:
                if line.startswith(f"{key}="):
                    return line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    return None
