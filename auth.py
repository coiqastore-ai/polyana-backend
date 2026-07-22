import hashlib, hmac, time, json, logging
from urllib.parse import parse_qsl
from fastapi import HTTPException, Header
from config import BOT_TOKEN


def validate_init_data(init_data: str) -> dict | None:
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    h = parsed.pop("hash", None)
    if not h:
        return None
    try:
        auth_date = int(parsed.get("auth_date", 0))
    except (ValueError, TypeError):
        return None
    if auth_date <= 0 or (time.time() - auth_date) > 86400:
        return None
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, h):
        return None
    try:
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None


async def get_current_user(
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
) -> int:
    """Extract user_id ONLY from HMAC-signed Telegram initData — never from query params."""
    if not x_telegram_init_data:
        raise HTTPException(401, "Missing initData")
    user = validate_init_data(x_telegram_init_data)
    if not user or "id" not in user:
        raise HTTPException(401, "Invalid initData")
    return int(user["id"])
