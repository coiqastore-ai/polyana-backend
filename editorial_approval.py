"""
Editorial approval flow — send preview to admin, handle callbacks.

States: draft → waiting_approval → publishing → published
                             → rejected
                             → needs_revision
                   publishing → waiting_approval (on failure/recovery)
"""

import html
import json
import logging

import asyncpg

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

log = logging.getLogger("polyana.editorial_approval")

# Valid state transitions
_VALID_TRANSITIONS = {
    "draft": {"waiting_approval"},
    "waiting_approval": {"publishing", "rejected", "needs_revision"},
    "needs_revision": {"waiting_approval"},
    "publishing": {"published", "waiting_approval"},
}


def can_transition(from_status: str, to_status: str) -> bool:
    """Check if a state transition is valid."""
    return to_status in _VALID_TRANSITIONS.get(from_status, set())


async def send_editorial_for_approval(
    *,
    bot,
    db: asyncpg.Connection,
    recipe_id: int,
    admin_chat_id: int,
    bot_username: str,
    frontend_url: str,
) -> dict:
    """
    Send editorial recipe preview to admin for approval.
    Transitions: draft → waiting_approval
    Returns {"ok": True, "message_id": int}
    """
    from telegram_publisher import _build_post_text

    rec = await db.fetchrow(
        "SELECT * FROM recipes WHERE id=$1 AND is_editorial=TRUE", recipe_id
    )
    if not rec:
        raise ValueError("Recipe not found or not editorial")

    if rec["editorial_status"] not in ("draft", "needs_revision", "waiting_approval", "publishing"):
        raise ValueError(f"Cannot send for approval from status '{rec['editorial_status']}'")

    # Build preview text
    ings = await db.fetch(
        "SELECT name, qty, unit FROM ingredients WHERE recipe_id=$1 ORDER BY sort_order, id",
        recipe_id,
    )
    steps = await db.fetch(
        "SELECT step_number, text FROM recipe_steps WHERE recipe_id=$1 ORDER BY step_number",
        recipe_id,
    )

    post_text = _build_post_text(rec, ings, steps, include_footer=False)

    # Add header
    header = "🌿 ПОСТ НА СОГЛАСОВАНИЕ\n\n"
    footer_parts = []

    if rec.get("trend_score"):
        footer_parts.append(f"Trend Score: {rec['trend_score']}")
    if rec.get("source_platform"):
        footer_parts.append(f"Источник: {rec['source_platform']}")
    if rec.get("source_author"):
        footer_parts.append(f"Автор: {rec['source_author']}")

    footer = ""
    if footer_parts:
        footer = "\n\n" + "\n".join(footer_parts)

    full_text = header + post_text + footer

    # Build keyboard
    slug = rec.get("content_slug") or ""
    preview_url = f"{frontend_url}?startapp=editorial_{slug}" if slug else ""

    keyboard_buttons = [
        [
            InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"ea:approve:{recipe_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ea:reject:{recipe_id}"),
        ],
    ]
    if preview_url:
        keyboard_buttons.append([
            InlineKeyboardButton(text="👁 Посмотреть рецепт", url=preview_url),
        ])
    keyboard_buttons.append([
        InlineKeyboardButton(text="✏️ На доработку", callback_data=f"ea:revise:{recipe_id}"),
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    # Send to admin
    msg = await bot.send_message(
        admin_chat_id,
        full_text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    # Update status
    await db.execute(
        "UPDATE recipes SET editorial_status='waiting_approval', updated_at=NOW() WHERE id=$1",
        recipe_id,
    )

    log.info("Sent editorial recipe %s for approval to admin %s, message_id=%s",
             recipe_id, admin_chat_id, msg.message_id)

    return {"ok": True, "message_id": msg.message_id}


async def handle_approval_callback(
    *,
    bot,
    db: asyncpg.Connection,
    callback_data: str,
    admin_user_id: int,
    admin_chat_id: int,
    editorial_chat_id: int,
    message_id: int,
    chat_id: int,
    bot_username: str,
    frontend_url: str,
) -> dict:
    """
    Handle admin approval callback.
    callback_data format: ea:{action}:{recipe_id}

    editorial_chat_id: target Telegram channel/group ID for publishing.
    """
    from telegram_publisher import publish_recipe_to_telegram

    parts = callback_data.split(":")
    if len(parts) != 3:
        return {"ok": False, "error": "Invalid callback data"}

    action = parts[1]
    try:
        recipe_id = int(parts[2])
    except ValueError:
        return {"ok": False, "error": "Invalid recipe ID"}

    # Verify admin
    if admin_user_id != admin_chat_id:
        return {"ok": False, "error": "Not admin"}

    # Validate channel config BEFORE any state change
    if action == "approve" and not editorial_chat_id:
        return {"ok": False, "error": "EDITORIAL_TELEGRAM_CHAT_ID is not configured"}

    # Fetch recipe with row lock to prevent concurrent callbacks
    rec = await db.fetchrow(
        "SELECT * FROM recipes WHERE id=$1 AND is_editorial=TRUE FOR UPDATE",
        recipe_id,
    )
    if not rec:
        return {"ok": False, "error": "Recipe not found"}

    current_status = rec["editorial_status"]

    if action == "approve":
        if not can_transition(current_status, "publishing"):
            return {"ok": False, "error": f"Cannot approve from '{current_status}'"}

        # Check idempotency — already published?
        if rec.get("editorial_telegram_message_id"):
            return {"ok": True, "already_published": True}

        # Step 1: Short transaction — lock, validate, set publishing
        await db.execute(
            "UPDATE recipes SET editorial_status='publishing', updated_at=NOW() WHERE id=$1",
            recipe_id,
        )

        # Step 2: Telegram send (outside transaction — network call)
        try:
            result = await publish_recipe_to_telegram(
                bot=bot,
                db=db,
                recipe_id=recipe_id,
                chat_id=editorial_chat_id,
                bot_username=bot_username,
                frontend_url=frontend_url,
            )
        except Exception as e:
            log.error("Failed to publish after approval: %s", e)
            # Step 3b: Failure — revert to waiting_approval
            await db.execute(
                "UPDATE recipes SET editorial_status='waiting_approval', updated_at=NOW() "
                "WHERE id=$1 AND editorial_telegram_message_id IS NULL",
                recipe_id,
            )
            return {"ok": False, "error": f"Publish failed: {e}"}

        # Update admin message
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✅ Опубликовано в канале\n\n{rec['name']}",
                parse_mode="HTML",
            )
        except Exception:
            pass

        return {"ok": True, "message_id": result.get("message_id")}

    elif action == "reject":
        if not can_transition(current_status, "rejected"):
            return {"ok": False, "error": f"Cannot reject from '{current_status}'"}

        await db.execute(
            "UPDATE recipes SET editorial_status='rejected', updated_at=NOW() WHERE id=$1",
            recipe_id,
        )

        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"❌ Отклонено\n\n{rec['name']}",
                parse_mode="HTML",
            )
        except Exception:
            pass

        return {"ok": True, "rejected": True}

    elif action == "revise":
        if not can_transition(current_status, "needs_revision"):
            return {"ok": False, "error": f"Cannot request revision from '{current_status}'"}

        await db.execute(
            "UPDATE recipes SET editorial_status='needs_revision', updated_at=NOW() WHERE id=$1",
            recipe_id,
        )

        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✏️ На доработке\n\n{rec['name']}",
                parse_mode="HTML",
            )
            await bot.send_message(
                chat_id,
                "Рецепт отправлен на доработку.",
            )
        except Exception:
            pass

        return {"ok": True, "needs_revision": True}

    return {"ok": False, "error": "Unknown action"}


async def recover_stale_publishing(
    *,
    db: asyncpg.Connection,
    stale_minutes: int = 10,
) -> list[dict]:
    """
    Recover recipes stuck in 'publishing' state.
    If status='publishing' AND editorial_telegram_message_id IS NULL
    AND updated_at < NOW() - INTERVAL '{stale_minutes} minutes',
    revert to 'waiting_approval'.
    Returns list of recovered recipe ids.
    """
    rows = await db.fetch(
        """
        UPDATE recipes
        SET editorial_status='waiting_approval', updated_at=NOW()
        WHERE editorial_status='publishing'
          AND editorial_telegram_message_id IS NULL
          AND updated_at < NOW() - make_interval(mins => $1)
        RETURNING id
        """,
        stale_minutes,
    )
    recovered = [r["id"] for r in rows]
    if recovered:
        log.info("Recovered %d stale publishing recipes: %s", len(recovered), recovered)
    return recovered
