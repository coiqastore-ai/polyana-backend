"""
Daily admin digest — business summary sent to ADMIN_CHAT_ID.

Metrics from previous day (00:00–23:59 Europe/Moscow).
"""

import logging
from datetime import datetime, timezone, timedelta

import asyncpg

from ai_provider_balance import get_ai_provider_balance

log = logging.getLogger("polyana.digest")

MSK = timezone(timedelta(hours=3))


async def build_daily_admin_digest(db: asyncpg.Connection) -> str:
    """Build the daily digest text for the previous day."""
    now_msk = datetime.now(MSK)
    yesterday = (now_msk - timedelta(days=1)).date()
    day_start = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=MSK)
    day_end = day_start + timedelta(days=1)
    day_start_utc = day_start.astimezone(timezone.utc)
    day_end_utc = day_end.astimezone(timezone.utc)

    date_str = _format_date_ru(yesterday)

    lines = [f"🌿 ПОЛЯНА — итоги за {date_str}", ""]

    # ── Users ─────────────────────────────────────────────────────────────
    new_users = await db.fetchval(
        "SELECT COUNT(*) FROM users WHERE created_at >= $1 AND created_at < $2",
        day_start_utc, day_end_utc,
    )
    total_users = await db.fetchval("SELECT COUNT(*) FROM users")
    lines.append(f"👤 Новых пользователей: {new_users}")
    lines.append(f"👥 Всего пользователей: {total_users}")
    lines.append("")

    # ── Payments ──────────────────────────────────────────────────────────
    successful_payments = await db.fetchval(
        "SELECT COUNT(*) FROM payment_orders WHERE status='succeeded' AND paid_at >= $1 AND paid_at < $2",
        day_start_utc, day_end_utc,
    )
    revenue = await db.fetchval(
        "SELECT COALESCE(SUM(amount), 0) FROM payment_orders WHERE status='succeeded' AND paid_at >= $1 AND paid_at < $2",
        day_start_utc, day_end_utc,
    )
    refunds = await db.fetchval(
        "SELECT COALESCE(SUM(amount), 0) FROM payment_orders WHERE status='refunded' AND refunded_at >= $1 AND refunded_at < $2",
        day_start_utc, day_end_utc,
    )

    lines.append(f"💳 Успешных оплат: {successful_payments}")
    lines.append(f"💰 Выручка: {revenue / 100:,.0f} ₽".replace(",", " "))
    if refunds > 0:
        lines.append(f"↩️ Возвраты: {refunds / 100:,.0f} ₽".replace(",", " "))
    lines.append("")

    # ── AI usage ──────────────────────────────────────────────────────────
    ai_ops = await db.fetchval(
        "SELECT COUNT(*) FROM ai_usage_log WHERE created_at >= $1 AND created_at < $2",
        day_start_utc, day_end_utc,
    )
    ai_cost = await db.fetchval(
        "SELECT COALESCE(SUM(provider_cost_usd), 0) FROM ai_usage_log WHERE created_at >= $1 AND created_at < $2",
        day_start_utc, day_end_utc,
    )

    lines.append(f"🤖 AI-операций: {ai_ops}")
    if ai_cost > 0:
        lines.append(f"💸 Расходы на AI: ${ai_cost:.2f}")

    # Provider balance
    balance_info = await get_ai_provider_balance()
    if balance_info.get("available") and balance_info.get("balance") is not None:
        lines.append(f"💵 Баланс AI ({balance_info['provider']}): ${balance_info['balance']:.2f}")
    else:
        lines.append(f"💵 Баланс AI: недоступен")
    lines.append("")

    # ── Recipes ───────────────────────────────────────────────────────────
    recipes_created = await db.fetchval(
        "SELECT COUNT(*) FROM recipes WHERE created_at >= $1 AND created_at < $2 AND is_editorial = FALSE",
        day_start_utc, day_end_utc,
    )
    lines.append(f"🍲 Пользовательских рецептов создано: {recipes_created}")
    lines.append("")

    # ── Channel funnel ────────────────────────────────────────────────────
    editorial_opens = await db.fetchval(
        "SELECT COUNT(*) FROM analytics_events WHERE event_type='editorial_recipe_opened' AND ts >= $1 AND ts < $2",
        day_start_utc, day_end_utc,
    )
    editorial_saves = await db.fetchval(
        "SELECT COUNT(*) FROM analytics_events WHERE event_type='editorial_recipe_saved' AND ts >= $1 AND ts < $2",
        day_start_utc, day_end_utc,
    )
    channel_clicks = await db.fetchval(
        "SELECT COUNT(*) FROM analytics_events WHERE event_type='channel_link_clicked' AND ts >= $1 AND ts < $2",
        day_start_utc, day_end_utc,
    )

    if editorial_opens or editorial_saves or channel_clicks:
        lines.append("📣 Из канала:")
        if channel_clicks:
            lines.append(f"Переходов в Поляну: {channel_clicks}")
        if editorial_opens:
            lines.append(f"Открытий рецептов: {editorial_opens}")
        if editorial_saves:
            lines.append(f"Сохранили рецепты: {editorial_saves}")
        lines.append("")

    # ── Best editorial recipe ─────────────────────────────────────────────
    best = await db.fetchrow(
        """
        SELECT r.name, r.id,
               COUNT(*) FILTER (WHERE ae.event_type = 'editorial_recipe_opened') as opens,
               COUNT(*) FILTER (WHERE ae.event_type = 'editorial_recipe_saved') as saves
        FROM recipes r
        LEFT JOIN analytics_events ae ON ae.props->>'editorial_recipe_id' = r.id::text
            AND ae.ts >= $1 AND ae.ts < $2
            AND ae.event_type IN ('editorial_recipe_opened', 'editorial_recipe_saved')
        WHERE r.is_editorial = TRUE AND r.published_at >= $1 AND r.published_at < $2
        GROUP BY r.id, r.name
        ORDER BY opens DESC
        LIMIT 1
        """,
        day_start_utc, day_end_utc,
    )
    if best and (best["opens"] or best["saves"]):
        lines.append(f"🔥 Лучший рецепт:")
        lines.append(f"«{best['name']}»")
        parts = []
        if best["opens"]:
            parts.append(f"{best['opens']} открытий")
        if best["saves"]:
            parts.append(f"{best['saves']} сохранений")
        lines.append(" · ".join(parts))

    return "\n".join(lines)


async def send_daily_admin_digest(bot, db: asyncpg.Connection, admin_chat_id: int):
    """Build and send the daily digest to admin."""
    try:
        text = await build_daily_admin_digest(db)
        await bot.send_message(admin_chat_id, text)
        log.info("Daily digest sent to admin %s", admin_chat_id)
    except Exception as e:
        log.error("Failed to send daily digest: %s", e)


def _format_date_ru(d) -> str:
    """Format date in Russian: '27 августа'."""
    months = [
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ]
    return f"{d.day} {months[d.month - 1]}"
