"""
Telegram admin digest for Trend Scanner results.
"""

import logging
import os

import asyncpg

log = logging.getLogger("polyana.trend_scanner.telegram_digest")


async def send_trend_digest(
    db: asyncpg.Connection,
    candidates: list[dict],
    source_health: dict,
    run_id: str,
):
    """Send trend scan results to admin via Telegram."""
    # Read config
    admin_chat_id = _get_admin_chat_id()
    bot_token = _get_bot_token()

    if not admin_chat_id or not bot_token:
        log.warning("Cannot send digest: missing ADMIN_CHAT_ID or BOT_TOKEN")
        return

    # Build summary message
    lines = [
        "🔥 ПОЛЯНА — TREND SCAN",
        "",
        f"Найдено: {sum(s.get('count', 0) for s in source_health.values())}",
        f"Уникальных: {len(candidates)}",
        f"Лучших: {len(candidates)}",
        "",
    ]

    # Source health
    lines.append("Источники:")
    for source, info in source_health.items():
        status = "✅" if info.get("status") == "ok" else "❌"
        count = info.get("count", 0)
        lines.append(f"  {status} {source}: {count}")
    lines.append("")

    # Top candidates
    for i, c in enumerate(candidates[:10], 1):
        title = c.get("title", "?")[:60]
        score = c.get("trend_score", 0)
        sources = c.get("all_sources", [c.get("source_platform", "?")])
        freshness = c.get("freshness_score", 0)
        ru_fit = c.get("ru_availability_score", 0)
        reason = c.get("reason", "")

        lines.append(f"{i}. {title}")
        lines.append(f"   Trend Score: {score:.0f}")
        lines.append(f"   Источники: {', '.join(sources)}")
        lines.append(f"   Свежесть: {freshness:.0f} | RU Fit: {ru_fit:.0f}")
        if reason:
            lines.append(f"   {reason}")
        lines.append("")

    summary_text = "\n".join(lines)

    # Send summary
    await _send_telegram(bot_token, admin_chat_id, summary_text)

    # Send individual candidate cards with buttons
    for i, c in enumerate(candidates[:10], 1):
        card_text = _build_candidate_card(c, i)
        buttons = _build_candidate_buttons(c)
        await _send_telegram(bot_token, admin_chat_id, card_text, buttons)


def _build_candidate_card(candidate: dict, index: int) -> str:
    """Build a detailed card for a single candidate."""
    title = candidate.get("title", "?")
    platform = candidate.get("source_platform", "?")
    url = candidate.get("source_url", "")
    author = candidate.get("source_author", "")
    score = candidate.get("trend_score", 0)
    reason = candidate.get("reason", "")
    recipe_type = candidate.get("recipe_type", "")
    keywords = candidate.get("keywords", [])
    sources = candidate.get("all_sources", [platform])
    freshness = candidate.get("freshness_score", 0)
    engagement = candidate.get("engagement_score", 0)
    ru_fit = candidate.get("ru_availability_score", 0)
    poliana_fit = candidate.get("poliana_fit_score", 0)

    lines = [
        f"📋 КАНДИДАТ #{index}",
        "",
        f"<b>{_esc(title)}</b>",
        f"Платформа: {platform}",
    ]
    if author:
        lines.append(f"Автор: {_esc(author)}")
    if len(sources) > 1:
        lines.append(f"Источники: {', '.join(sources)}")
    lines.append("")
    lines.append(f"📊 Оценки:")
    lines.append(f"  Trend: {score:.0f} | Свежесть: {freshness:.0f}")
    lines.append(f"  Engagement: {engagement:.0f} | RU: {ru_fit:.0f}")
    lines.append(f"  Poliana Fit: {poliana_fit:.0f}")
    if reason:
        lines.append(f"\n💡 {_esc(reason)}")
    if keywords:
        lines.append(f"🏷 {', '.join(keywords)}")
    if url:
        lines.append(f"\n🔗 {_esc(url[:100])}")

    return "\n".join(lines)


def _build_candidate_buttons(candidate: dict) -> list:
    """Build inline buttons for a candidate."""
    candidate_id = candidate.get("id", 0)
    url = candidate.get("source_url", "")

    buttons = []

    if url:
        buttons.append({"text": "👁 Посмотреть", "url": url})

    buttons.append({"text": "✅ В работу", "callback_data": f"ts:approve:{candidate_id}"})
    buttons.append({"text": "❌ Пропустить", "callback_data": f"ts:reject:{candidate_id}"})

    return buttons


async def _send_telegram(bot_token: str, chat_id: int, text: str, buttons: list | None = None):
    """Send a message via Telegram Bot API."""
    import httpx

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if buttons:
        # Build inline keyboard
        keyboard = []
        row = []
        for btn in buttons:
            row.append(btn)
            if len(row) >= 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        payload["reply_markup"] = {"inline_keyboard": keyboard}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json=payload)
            if r.status_code != 200:
                log.warning("Telegram send failed: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.error("Telegram send error: %s", e)


def _esc(text: str) -> str:
    """Escape HTML special characters."""
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _get_admin_chat_id() -> int | None:
    """Get admin chat ID from env."""
    val = os.environ.get("ADMIN_CHAT_ID")
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    try:
        with open("/etc/polyana/env") as f:
            for line in f:
                if line.startswith("ADMIN_CHAT_ID="):
                    return int(line.strip().split("=", 1)[1])
    except (FileNotFoundError, ValueError):
        pass
    return None


def _get_bot_token() -> str | None:
    """Get bot token from env."""
    val = os.environ.get("BOT_TOKEN")
    if val:
        return val
    try:
        with open("/etc/polyana/env") as f:
            for line in f:
                if line.startswith("BOT_TOKEN="):
                    return line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    return None
