"""
Telegram publisher for editorial recipes.

Posts formatted recipes to a Telegram channel/chat with inline buttons.
"""

import html
import logging

import asyncpg

log = logging.getLogger("polyana.editorial")


def _build_post_text(rec, ings, steps, *, include_footer: bool = True) -> str:
    """Build the HTML post text for a recipe."""
    name = rec["name"]
    emoji = rec["emoji"] or "🍽"
    description = rec.get("description") or ""
    servings = rec.get("servings")
    cook_time = rec.get("cook_time_minutes")
    category = rec.get("category")
    tags = list(rec.get("tags") or [])

    lines = [f"{emoji} <b>{html.escape(name)}</b>"]

    if description:
        desc = description[:200]
        if len(description) > 200:
            desc += "..."
        lines.append(f"\n{html.escape(desc)}")

    meta = []
    if cook_time:
        meta.append(f"⏱ {cook_time} минут")
    if servings:
        meta.append(f"🍽 {servings} порц.")
    if meta:
        lines.append(" · ".join(meta))

    # Nutrition
    calories = rec.get("calories_kcal")
    protein = rec.get("protein_g")
    fat = rec.get("fat_g")
    carbs = rec.get("carbs_g")
    if calories is not None or protein is not None:
        nut_parts = []
        if calories is not None:
            nut_parts.append(f"⚡ {_fmt_num(calories)} ккал")
        if protein is not None:
            nut_parts.append(f"Б {_fmt_num(protein)}")
        if fat is not None:
            nut_parts.append(f"Ж {_fmt_num(fat)}")
        if carbs is not None:
            nut_parts.append(f"У {_fmt_num(carbs)}")
        lines.append(" · ".join(nut_parts))

    # Ingredients (max 15)
    lines.append(f"\n🥕 <b>Ингредиенты</b>")
    for ing in ings[:15]:
        q = ing.get("qty")
        if q and q != 0:
            q_str = str(int(q)) if q == int(q) else str(round(q, 2)).rstrip("0").rstrip(".")
            qty = f"{q_str} {ing.get('unit') or ''}".strip()
        else:
            qty = ""
        line = f"• {html.escape(ing['name'])}"
        if qty:
            line += f" — {html.escape(qty)}"
        lines.append(line)
    if len(ings) > 15:
        lines.append(f"...и ещё {len(ings) - 15}")

    # Steps (max 8, truncate long ones)
    lines.append(f"\n👩‍🍳 <b>Как готовить</b>")
    for step in steps[:8]:
        text = step["text"][:150]
        if len(step["text"]) > 150:
            text += "..."
        lines.append(f"{step['step_number']}. {html.escape(text)}")
    if len(steps) > 8:
        lines.append(f"\nПолный рецепт — по кнопке ниже 👇")

    if include_footer:
        # Hashtags
        hashtag_parts = []
        if category:
            hashtag_parts.append(category.lower().replace(" ", ""))
        for tag in tags[:3]:
            hashtag_parts.append(tag.lower().replace(" ", ""))
        if hashtag_parts:
            hashtags = " ".join(f"#{t}" for t in hashtag_parts)
            lines.append(f"\n{hashtags}")

    return "\n".join(lines)


def _fmt_num(val) -> str:
    """Format number without unnecessary decimals."""
    if val is None:
        return ""
    f = float(val)
    if f == int(f):
        return str(int(f))
    return str(round(f, 1))


async def publish_recipe_to_telegram(
    *,
    bot,
    db: asyncpg.Connection,
    recipe_id: int,
    chat_id: int,
    bot_username: str,
    frontend_url: str,
) -> dict:
    """
    Publish an editorial recipe to Telegram.

    Requirements:
    - recipe exists and is_editorial=TRUE
    - editorial_status = 'publishing'
    - has at least 1 ingredient and 1 step

    After success: sets editorial_status='published', published_at=NOW().
    Returns {"ok": True, "message_id": int} on success.
    """
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    # Fetch recipe
    rec = await db.fetchrow(
        "SELECT * FROM recipes WHERE id=$1 AND is_editorial=TRUE", recipe_id
    )
    if not rec:
        raise ValueError("Recipe not found or not editorial")
    if rec["editorial_status"] != "publishing":
        raise ValueError(f"Cannot publish: status is '{rec['editorial_status']}', need 'publishing'")

    # Fetch ingredients and steps
    ings = await db.fetch(
        "SELECT name, qty, unit FROM ingredients WHERE recipe_id=$1 ORDER BY sort_order, id",
        recipe_id,
    )
    steps = await db.fetch(
        "SELECT step_number, text FROM recipe_steps WHERE recipe_id=$1 ORDER BY step_number",
        recipe_id,
    )

    if not ings:
        raise ValueError("Cannot publish: no ingredients")
    if not steps:
        raise ValueError("Cannot publish: no steps")

    slug = rec.get("content_slug")
    message_text = _build_post_text(rec, ings, steps)

    # Build buttons
    mini_app_url = f"{frontend_url}?startapp=editorial_{slug}" if slug else f"{frontend_url}"
    share_text = f"{rec['name']} — рецепт из ПОЛЯНЫ"
    share_url = f"https://t.me/share/url?url={mini_app_url}&text={share_text}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌿 Сохранить в Поляне", url=mini_app_url),
        ],
        [
            InlineKeyboardButton(text="📤 Поделиться", url=share_url),
        ],
    ])

    # Send message
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=message_text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.error("Failed to publish recipe %s: %s", recipe_id, e)
        raise

    # Update recipe status with idempotency fields
    await db.execute(
        """
        UPDATE recipes
        SET editorial_status='published',
            visibility='public',
            published_at=NOW(),
            editorial_telegram_message_id=$2,
            editorial_telegram_chat_id=$3,
            updated_at=NOW()
        WHERE id=$1
        """,
        recipe_id, msg.message_id, chat_id,
    )

    log.info("Published editorial recipe %s to chat %s, message_id=%s", recipe_id, chat_id, msg.message_id)

    return {"ok": True, "message_id": msg.message_id}
