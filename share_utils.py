"""Pure helpers for Telegram recipe sharing.

Kept separate from ``main.py`` so the URL and HTML contract can be tested
without starting the bot or connecting to PostgreSQL.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape
from urllib.parse import quote


def build_mini_app_deep_link(
    bot_username: str,
    short_name: str,
    start_parameter: str,
) -> str:
    """Build a Direct Mini App link registered through BotFather."""
    username = (bot_username or "").strip().lstrip("@")
    app_name = (short_name or "").strip().strip("/")
    if not username or not app_name:
        raise ValueError("bot username and Mini App short name are required")
    return (
        f"https://t.me/{quote(username, safe='')}/{quote(app_name, safe='')}"
        f"?startapp={quote(str(start_parameter), safe='')}"
    )


def _escaped(value, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return escape(text, quote=False)


def _format_quantity(value) -> str:
    if value in (None, "", 0, 0.0):
        return ""
    try:
        number = Decimal(str(value))
        if number == number.to_integral_value():
            return str(int(number))
        return format(number.quantize(Decimal("0.01")).normalize(), "f")
    except (InvalidOperation, TypeError, ValueError):
        return _escaped(value, 40)


def build_recipe_share_text(
    snapshot: dict,
    *,
    ingredient_limit: int = 12,
    step_limit: int = 6,
) -> str:
    """Render a compact, HTML-safe Telegram message from a recipe snapshot."""
    ingredients = list(snapshot.get("ingredients") or [])
    steps = list(snapshot.get("steps") or [])
    emoji = _escaped(snapshot.get("emoji") or "🍽", 16)
    name = _escaped(snapshot.get("name") or "Рецепт", 120)
    lines = [f"{emoji} <b>{name}</b>"]

    meta = []
    if snapshot.get("category"):
        meta.append(_escaped(snapshot["category"], 60))
    if snapshot.get("servings"):
        meta.append(f"🍽 {_escaped(snapshot['servings'], 20)} порц.")
    if snapshot.get("cook_time_minutes"):
        meta.append(f"⏱ {_escaped(snapshot['cook_time_minutes'], 20)} мин.")
    if meta:
        lines.append(" · ".join(meta))

    if ingredients:
        lines.append(f"\n🥄 Ингредиенты ({len(ingredients)}):")
        for ingredient in ingredients[:ingredient_limit]:
            quantity = _format_quantity(ingredient.get("qty"))
            unit = _escaped(ingredient.get("unit"), 30)
            amount = f"{quantity} {unit}".strip()
            item_name = _escaped(ingredient.get("name") or "Ингредиент", 100)
            lines.append(f"  • {item_name}" + (f" — {amount}" if amount else ""))
        if len(ingredients) > ingredient_limit:
            lines.append(f"  … и ещё {len(ingredients) - ingredient_limit}")

    if steps and step_limit > 0:
        lines.append("\n📋 Приготовление:")
        for position, step in enumerate(steps[:step_limit], start=1):
            number = _escaped(step.get("step_number") or position, 12)
            text = _escaped(step.get("text") or "", 300)
            lines.append(f"  {number}. {text}")
        if len(steps) > step_limit:
            lines.append(f"  … и ещё {len(steps) - step_limit} шагов")

    lines.append("\n🌿 Рецепт из ПОЛЯНЫ")
    return "\n".join(lines)


def recipe_share_title(snapshot: dict) -> str:
    """Return a valid, compact InlineQueryResult title."""
    title = str(snapshot.get("name") or "Рецепт").strip() or "Рецепт"
    return title[:64]


def prepared_expiration_value(prepared) -> int | None:
    """Convert aiogram's expiration value to a JSON-safe Unix timestamp."""
    value = getattr(prepared, "expiration_date", None)
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, (int, float)):
        return int(value)
    return None
