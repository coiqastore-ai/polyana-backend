"""
Telegram Rich Message helpers for ПОЛЯНА.
Uses InputRichMessage for rich formatting with buttons.
"""
import logging
from typing import Optional

from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputTextMessageContent, InlineQueryResultArticle,
)
from nutrition import nutrition_per_serving, format_nutrition_card

log = logging.getLogger("polyana.telegram_rich")


def build_recipe_rich(recipe: dict) -> dict:
    """
    Build rich message content for a recipe.
    Returns dict with 'html' and 'reply_markup' keys.
    """
    lines = []
    
    # Header
    emoji = recipe.get("emoji", "🍽")
    name = recipe.get("name", "Рецепт")
    lines.append(f"{emoji} <b>{name}</b>")
    
    # Time and servings
    cook_time = recipe.get("cook_time_minutes")
    servings = recipe.get("servings")
    meta_parts = []
    if servings:
        meta_parts.append(f"🍽 {servings} порц.")
    if cook_time:
        meta_parts.append(f"⏱ {cook_time} мин.")
    if meta_parts:
        lines.append(" · ".join(meta_parts))
    
    # Nutrition if available
    nutrition = recipe.get("nutrition")
    if nutrition:
        per = nutrition_per_serving(recipe)
        if per:
            lines.append("")
            lines.append(format_nutrition_card(per))
    
    # Ingredients
    lines.append("")
    lines.append("<b>Ингредиенты:</b>")
    
    for ing in recipe.get("ingredients", []):
        qty = ing.get("qty")
        unit = ing.get("unit") or ""
        name = ing.get("name", "")
        
        if qty is not None:
            qty_text = f"{qty:.2f}".rstrip("0").rstrip(".") if not float(qty).is_integer() else str(int(qty))
            amount = f" — {qty_text}"
            if unit:
                amount += f" {unit}"
        else:
            amount = ""
        
        lines.append(f"• {name}{amount}")
    
    # Steps
    lines.append("")
    lines.append("<b>Приготовление:</b>")
    
    for i, step in enumerate(recipe.get("steps", []), 1):
        text = step.get("text", "")
        lines.append(f"{i}. {text}")
    
    html = "\n".join(lines)
    
    # Buttons
    buttons = []
    
    # Save button (callback)
    buttons.append([
        InlineKeyboardButton(
            text="💾 Сохранить себе",
            callback_data=f"save_shared_{recipe.get('id', '')}",
        )
    ])
    
    # Shopping list button
    buttons.append([
        InlineKeyboardButton(
            text="🛒 В список покупок",
            callback_data=f"add_shopping_{recipe.get('id', '')}",
        )
    ])
    
    # Open in POLYANA
    buttons.append([
        InlineKeyboardButton(
            text="🌿 Открыть в ПОЛЯНЕ",
            url=f"https://t.me/polyana_recipe_bot/polyana?startapp=recipe_{recipe.get('id', '')}",
        )
    ])
    
    reply_markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    return {
        "html": html,
        "reply_markup": reply_markup,
    }


def build_referral_rich(referral_url: str) -> dict:
    """
    Build rich message content for referral invitation.
    Returns dict with 'html' and 'reply_markup' keys.
    """
    html = (
        "🌿 <b>ПОЛЯНА</b>\n\n"
        "Рецепты, меню и покупки в одном месте.\n\n"
        "• сохраняйте рецепты из ссылок, фото, текста и голоса;\n"
        "• составляйте меню для встреч и праздников;\n"
        "• собирайте общий список покупок;\n"
        "• отправляйте рецепты друзьям."
    )
    
    reply_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🌿 Открыть ПОЛЯНУ",
            url=referral_url,
        )]
    ])
    
    return {
        "html": html,
        "reply_markup": reply_markup,
    }


def build_event_rich(event: dict) -> dict:
    """
    Build rich message content for an event.
    Returns dict with 'html' and 'reply_markup' keys.
    """
    lines = []
    
    lines.append(f"🎉 <b>{event.get('name', 'Событие')}</b>")
    
    if event.get("event_date"):
        lines.append(f"📅 {event['event_date']}")
    
    if event.get("location"):
        lines.append(f"📍 {event['location']}")
    
    if event.get("description"):
        lines.append("")
        lines.append(event["description"])
    
    if event.get("guests_count"):
        lines.append(f"\n👥 Гостей: {event['guests_count']}")
    
    html = "\n".join(lines)
    
    buttons = []
    if event.get("share_token"):
        buttons.append([
            InlineKeyboardButton(
                text="📋 Открыть меню",
                url=f"https://t.me/polyana_recipe_bot/polyana?startapp=event_{event['id']}",
            )
        ])
    
    reply_markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    return {
        "html": html,
        "reply_markup": reply_markup,
    }


async def send_recipe_result(
    bot,
    chat_id: int,
    recipe: dict,
    feature_rich: bool = False,
    caption: str = None,
) -> bool:
    """
    Send recipe result to chat. Uses Rich Message if enabled, falls back to HTML.
    Returns True if sent successfully.
    """
    from aiogram.types import InputRichMessage, InputRichMessageContent
    
    rich_data = build_recipe_rich(recipe)
    
    if feature_rich:
        try:
            # Try Rich Message
            rich_msg = InputRichMessage(
                html=rich_data["html"],
                reply_markup=rich_data["reply_markup"],
            )
            await bot.send_rich_message(
                chat_id=chat_id,
                rich_message=rich_msg,
            )
            return True
        except Exception as e:
            log.warning("Rich message failed, falling back to HTML: %s", e)
    
    # Fallback to HTML message
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=rich_data["html"],
            reply_markup=rich_data["reply_markup"],
            parse_mode="HTML",
        )
        return True
    except Exception as e:
        log.error("Failed to send recipe: %s", e)
        return False


async def send_rich_or_fallback(
    bot,
    chat_id: int,
    html: str,
    reply_markup=None,
    feature_rich: bool = False,
) -> bool:
    """
    Send a rich message or fall back to regular HTML message.
    """
    from aiogram.types import InputRichMessage
    
    if feature_rich:
        try:
            rich_msg = InputRichMessage(
                html=html,
                reply_markup=reply_markup,
            )
            await bot.send_rich_message(
                chat_id=chat_id,
                rich_message=rich_msg,
            )
            return True
        except Exception as e:
            log.warning("Rich message failed, falling back: %s", e)
    
    # Fallback
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=html,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        return True
    except Exception as e:
        log.error("Failed to send message: %s", e)
        return False
