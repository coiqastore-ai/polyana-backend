"""
Polyana — Split Expenses Module
QR-коды = бесплатно, Фото = платно (LLM)
"""

import io
import json
import logging
import re
from urllib.parse import urlparse, parse_qs

import httpx
from PIL import Image
from pyzbar.pyzbar import decode as qr_decode

log = logging.getLogger("polyana.split")

# ─── QR Code Scanning ───────────────────────────────────────────────────────

async def scan_qr_from_image(image_bytes: bytes) -> str | None:
    """Decode QR code from image bytes. Returns QR data string or None."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        decoded = qr_decode(img)
        if decoded:
            return decoded[0].data.decode('utf-8')
    except Exception as e:
        log.warning(f"QR decode failed: {e}")
    return None


def parse_fns_qr(qr_data: str) -> dict | None:
    """
    Parse FNS QR code data.
    Format: https://check.ffd.ksrf.ru/...?fn=...&i=...&fp=...&t=...
    Or payload: t=...&s=...&fn=...&i=...&fp=...&n=...
    """
    try:
        if qr_data.startswith('http'):
            parsed = urlparse(qr_data)
            params = parse_qs(parsed.query)
        else:
            params = {}
            for item in qr_data.split('&'):
                if '=' in item:
                    key, value = item.split('=', 1)
                    params[key] = [value]

        # Extract required fields
        fn = params.get('fn', [None])[0]
        i = params.get('i', [None])[0]
        fp = params.get('fp', [None])[0]
        t = params.get('t', [None])[0]

        if all([fn, i, fp, t]):
            return {'fn': fn, 'i': i, 'fp': fp, 't': t}
    except Exception as e:
        log.warning(f"FNS QR parse failed: {e}")
    return None


async def fetch_fns_receipt(fn: str, i: str, fp: str, t: str) -> dict | None:
    """
    Fetch receipt data from FNS API.
    API: https://api-fns.ru/api/receipt
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api-fns.ru/api/receipt",
                params={'fn': fn, 'i': i, 'fp': fp, 't': t}
            )
            if resp.status_code == 200:
                data = resp.json()
                if 'json' in data:
                    return data['json']
    except Exception as e:
        log.warning(f"FNS API request failed: {e}")
    return None


def format_receipt_items(items: list) -> str:
    """Format receipt items for display."""
    lines = []
    for item in items:
        name = item.get('name', 'Товар')
        price = item.get('price', 0)
        qty = item.get('quantity', 1)
        if isinstance(price, (int, float)) and price > 100:
            # Price in kopecks
            price = price / 100
        lines.append(f"  {name} — {price:.0f}₽")
    return '\n'.join(lines)


# ─── Split Expenses Logic ───────────────────────────────────────────────────

async def create_split_event(db, chat_id: int, title: str, organizer_id: int) -> int:
    """Create a new split event. Returns event_id."""
    event_id = await db.fetchval(
        """INSERT INTO split_events (chat_id, title, organizer_id)
           VALUES ($1, $2, $3) RETURNING id""",
        chat_id, title, organizer_id
    )
    # Add organizer as participant
    await db.execute(
        """INSERT INTO split_participants (event_id, user_id, display_name, is_organizer)
           VALUES ($1, $2, (SELECT first_name FROM collaborators WHERE telegram_user_id = $2 LIMIT 1), TRUE)
           ON CONFLICT (event_id, user_id) DO NOTHING""",
        event_id, organizer_id
    )
    return event_id


async def add_participant(db, event_id: int, user_id: int, display_name: str = None) -> bool:
    """Add participant to split event."""
    try:
        await db.execute(
            """INSERT INTO split_participants (event_id, user_id, display_name)
               VALUES ($1, $2, $3)
               ON CONFLICT (event_id, user_id) DO NOTHING""",
            event_id, user_id, display_name
        )
        return True
    except Exception:
        return False


async def add_receipt_to_split(db, event_id: int, user_id: int, receipt_data: dict, source: str = 'qr') -> int:
    """
    Add receipt to split event.
    source: 'qr' (free) or 'photo' (paid)
    """
    items_json = json.dumps(receipt_data.get('items', []), ensure_ascii=False)
    receipt_id = await db.fetchval(
        """INSERT INTO split_receipts (event_id, uploaded_by, store_name, total, raw_json, source)
           VALUES ($1, $2, $3, $4, $5::jsonb, $6) RETURNING id""",
        event_id, user_id,
        receipt_data.get('store', 'Неизвестный магазин'),
        receipt_data.get('total', 0) / 100,  # Convert kopecks to rubles
        items_json,
        source
    )

    # Update event total
    await db.execute(
        """UPDATE split_events SET total = (
               SELECT COALESCE(SUM(total), 0) FROM split_receipts WHERE event_id = $1
           ) WHERE id = $1""",
        event_id
    )

    return receipt_id


async def set_contribution(db, event_id: int, user_id: int, amount: float) -> bool:
    """Set contribution amount for participant."""
    try:
        await db.execute(
            """INSERT INTO split_participants (event_id, user_id, contributed)
               VALUES ($1, $2, $3)
               ON CONFLICT (event_id, user_id)
               DO UPDATE SET contributed = $3""",
            event_id, user_id, amount
        )
        return True
    except Exception:
        return False


async def calculate_and_notify(db, event_id: int, bot) -> str:
    """
    Calculate debts and send notifications.
    Returns summary text.
    """
    # Get event info
    event = await db.fetchrow(
        "SELECT * FROM split_events WHERE id = $1", event_id
    )
    if not event:
        return "Событие не найдено"

    # Get participants
    participants = await db.fetch(
        "SELECT * FROM split_participants WHERE event_id = $1", event_id
    )
    if len(participants) < 2:
        return "Нужно минимум 2 участника"

    total = event['total']
    per_person = total / len(participants)

    # Calculate debts
    debts = []
    organizer_id = event['organizer_id']

    for p in participants:
        debt = per_person - (p['contributed'] or 0)
        if debt > 0.01:  # Ignore kopecks
            debts.append({
                'user_id': p['user_id'],
                'name': p['display_name'] or f"User {p['user_id']}",
                'amount': round(debt, 2),
                'is_organizer': p['is_organizer']
            })

    # Send notifications
    for debt in debts:
        if debt['is_organizer']:
            # Organizer gets summary
            total_owed = sum(d['amount'] for d in debts if not d['is_organizer'])
            if total_owed > 0:
                try:
                    await bot.send_message(
                        debt['user_id'],
                        f"💰 Итого по «{event['title']}»:\n"
                        f"Тебе должны: {total_owed:.0f}₽"
                    )
                except Exception:
                    pass
        else:
            # Others get individual debt
            try:
                await bot.send_message(
                    debt['user_id'],
                    f"💰 С тебя {debt['amount']:.0f}₽ за «{event['title']}»\n"
                    f"Договоритесь о способе оплаты."
                )
            except Exception:
                pass

    # Build summary
    summary_lines = [
        f"📊 Итоги: {event['title']}",
        f"Всего: {total:.0f}₽",
        f"Участников: {len(participants)}",
        f"На человека: {per_person:.0f}₽",
        "",
        "Долги:"
    ]
    for debt in debts:
        if not debt['is_organizer']:
            summary_lines.append(f"• {debt['name']}: {debt['amount']:.0f}₽")

    return '\n'.join(summary_lines)


# ─── Monetization: QR = Free, Photo = Paid ──────────────────────────────────

QR_SCAN_PRICE = 0      # Free
PHOTO_PARSE_PRICE = 1000  # 10₽ in kopecks

async def handle_receipt_photo(db, user_id: int, image_bytes: bytes, event_id: int, bot) -> tuple[str, bool]:
    """
    Handle receipt photo.
    Returns (message_text, is_free).
    """
    # Try QR first (free)
    qr_data = await scan_qr_from_image(image_bytes)

    if qr_data:
        # Parse FNS QR
        fns_params = parse_fns_qr(qr_data)
        if fns_params:
            # Fetch from FNS API (free)
            receipt = await fetch_fns_receipt(**fns_params)
            if receipt:
                # Save receipt
                await add_receipt_to_split(db, event_id, user_id, receipt, source='qr')

                # Format response
                items_text = format_receipt_items(receipt.get('items', []))
                total = receipt.get('totalSum', 0) / 100
                store = receipt.get('user', 'Магазин')

                msg = (
                    f"✅ Чек распознан (QR — бесплатно)\n\n"
                    f"🏪 {store}\n"
                    f"💰 Итого: {total:.0f}₽\n\n"
                    f"{items_text}\n\n"
                    f"Подтвердить?"
                )
                return msg, True

    # QR not found or invalid — offer photo parsing (paid)
    # Check balance
    from main import _get_balance, _debit
    balance = await _get_balance(db, user_id)

    if balance < PHOTO_PARSE_PRICE:
        return (
            f"📷 QR не распознан.\n\n"
            f"Для распознавания по фото нужно {PHOTO_PARSE_PRICE // 100}₽.\n"
            f"Твой баланс: {balance // 100}₽\n\n"
            f"Пополнить баланс: /balance",
            False
        )

    return (
        f"📷 QR не распознан.\n\n"
        f"Распознать по фото? Стоимость: {PHOTO_PARSE_PRICE // 100}₽\n"
        f"Твой баланс: {balance // 100}₽\n\n"
        f"[✅ Распознать за {PHOTO_PARSE_PRICE // 100}₽]",
        False
    )


# ─── Inline Keyboards ───────────────────────────────────────────────────────

def split_main_keyboard():
    """Main split menu keyboard."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый делёж", callback_data="split_new")],
        [InlineKeyboardButton(text="📋 Мои дележи", callback_data="split_list")],
        [InlineKeyboardButton(text="⭐ Premium", callback_data="split_premium")],
    ])


def split_event_keyboard(event_id: int):
    """Event action keyboard."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📸 Добавить чек", callback_data=f"split_add_{event_id}"),
            InlineKeyboardButton(text="👥 Участники", callback_data=f"split_members_{event_id}"),
        ],
        [
            InlineKeyboardButton(text="💰 Вклад", callback_data=f"split_contribute_{event_id}"),
            InlineKeyboardButton(text="📊 Итоги", callback_data=f"split_done_{event_id}"),
        ],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="split_back")],
    ])


def split_confirm_keyboard(receipt_id: int):
    """Receipt confirmation keyboard."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"split_confirm_{receipt_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"split_cancel_{receipt_id}"),
        ],
    ])


def split_pricing_keyboard():
    """Pricing info keyboard."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="balance_topup")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="split_back")],
    ])


def split_help_text():
    """Help text showing free vs paid features."""
    return (
        "💡 Как работает «Делёж»:\n\n"
        "🆓 Бесплатно:\n"
        "• Сканирование QR-кода на чеке\n"
        "• Распознавание через ФНС API\n"
        "• Расчёт долгов\n"
        "• Уведомления участникам\n\n"
        "⭐ Premium:\n"
        "• Распознавание чеков по фото\n"
        "• Умные рекомендации\n"
        "• Приоритетная поддержка\n\n"
        "📱 Команды:\n"
        "• /split <название> — новый делёж"
    )


def split_premium_text():
    """Premium features text."""
    return (
        "⭐ <b>Premium</b>\n\n"
        "Расширенные возможности для тех, кто готовится серьёзно:\n\n"
        "📸 <b>Распознавание по фото</b>\n"
        "Сфотографируй чек — бот распознает всё автоматически\n\n"
        "🧠 <b>Умные рекомендации</b>\n"
        "Персональные советы по рецептам и спискам покупок\n\n"
        "📊 <b>Статистика расходов</b>\n"
        "Анализ трат по событиям и категориям\n\n"
        " priority <b>Приоритетная поддержка</b>\n"
        "Быстрые ответы и помощь с настройкой\n\n"
        "💡 <b>Как получить:</b>\n"
        "Активируй Premium в мини-приложении ПОЛЯНА"
    )
