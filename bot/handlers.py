"""Polyana Telegram bot handlers — all @dp.message/@dp.callback decorators.

Imported for side-effect: importing this module registers handlers on core.dp.
"""
import logging

from fastapi import Depends
from aiogram import F
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardRemove, WebAppInfo,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, LabeledPrice,
)
from aiogram.fsm.context import FSMContext

from core import bot, dp, VoiceStates
import core
from config import FRONTEND_URL, ADMIN_CHAT_ID, SUPPORT_HANDLE, REFERRAL_PERCENT, STAR_RUB_RATE, PRICE_AI_INVITE, _URL_RE
from db import get_db, track
from parsing import parse_and_save_recipe, _save_parsed_recipe
from llm import _llm_parse_images, _transcribe_voice, _alert_admin, _openrouter_remaining_usd
from routes.balance import _credit, _get_bot_username

try:
    from split_module import (
        handle_receipt_photo,
        split_main_keyboard,
        split_event_keyboard,
        split_confirm_keyboard,
        split_pricing_keyboard,
        split_help_text,
        split_premium_text,
        calculate_and_notify,
    )
    SPLIT_AVAILABLE = True
except ImportError:
    SPLIT_AVAILABLE = False

log = logging.getLogger("polyana")


# ── Bot helpers ───────────────────────────────────────────────────────────────

async def _reply_recipe_saved(message: Message, recipe: dict, status_msg=None):
    """Send/edit recipe-saved confirmation with Open + AddToEvent buttons."""
    ct = recipe.get("cook_time_minutes")
    already = recipe.get("already_exists", False)
    header = "📚 Рецепт уже в библиотеке!" if already else "✅ <b>Сохранено в библиотеку!</b>"
    ct_str = f"⏱ {ct} мин · " if ct else ""
    cat_str = f"[{recipe['category']}] " if recipe.get("category") else ""
    serv_str = f"🍽 {recipe['servings']} порц. · " if recipe.get("servings") else ""
    body = (
        f"{header}\n\n"
        f"{recipe['emoji']} <b>{recipe['name']}</b>\n"
        f"{cat_str}{serv_str}{ct_str}"
        f"🥕 {recipe['ingredients_count']} ингр."
    )
    recipe_url = f"{FRONTEND_URL}?screen=recipe&id={recipe['id']}"
    add_url = f"{FRONTEND_URL}?screen=add_to_event&recipe_id={recipe['id']}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📖 Открыть", web_app=WebAppInfo(url=recipe_url)),
            InlineKeyboardButton(text="📅 В событие", web_app=WebAppInfo(url=add_url)),
        ],
        [
            InlineKeyboardButton(text="📤 Поделиться", callback_data=f"share_recipe_{recipe['id']}"),
        ],
    ])
    if status_msg:
        await status_msg.edit_text(body, reply_markup=kb)
    else:
        await message.answer(body, reply_markup=kb)


async def _reply_parse_error(status_msg, err: Exception, hint: str = "рецепт"):
    msg = str(err)
    if isinstance(err, ValueError):
        # User-facing ValueError: show the message directly, it's already human-readable
        await status_msg.edit_text(f"🤷 {msg}")
    elif "not_a_recipe" in msg or "Не удалось распознать" in msg:
        await status_msg.edit_text(f"🤷 Не смог найти {hint} в этом контенте.\nПришли ссылку или команду /add")
    elif "429" in msg or "rate-limit" in msg.lower() or "temporarily" in msg.lower():
        await status_msg.edit_text("⏳ Сервис распознавания перегружен. Попробуй через минуту.")
    else:
        log.error("parse error (%s): %s", hint, err)
        await status_msg.edit_text("❌ Не получилось разобрать. Попробуй ещё раз или пришли текст/ссылку.")


# ── /add command ──────────────────────────────────────────────────────────────

@dp.message(Command("add"))
async def cmd_add(message: Message):
    await message.answer(
        "📥 <b>Добавление рецепта</b>\n\n"
        "Пришлите мне:\n"
        "• 🔗 Ссылку на любой сайт с рецептом\n"
        "• 📝 Текст рецепта\n"
        "• 📸 Фото рецепта (из книги, экрана)\n"
        "• 🎙 Голосовое сообщение\n\n"
        "<i>Рецепт сохранится в вашу личную библиотеку.</i>"
    )


async def _send_referral(msg: Message, uid: int):
    async with core.pool.acquire() as db:
        invited = await db.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=$1", uid) or 0
        earned = await db.fetchval(
            "SELECT COALESCE(SUM(amount),0) FROM referral_bonuses WHERE referrer_id=$1 AND paid", uid) or 0
        pending = await db.fetchval(
            "SELECT COALESCE(SUM(amount),0) FROM referral_bonuses WHERE referrer_id=$1 AND NOT paid", uid) or 0
    username = await _get_bot_username()
    link = f"https://t.me/{username}?start=ref_{uid}" if username else "(ссылка недоступна)"
    text = (
        "💰 <b>Партнёрская программа</b>\n\n"
        f"Приглашай друзей и получай <b>{REFERRAL_PERCENT}%</b> с их трат в боте — "
        "бонусом на баланс (начисляется через 24 часа).\n\n"
        f"🔗 Твоя ссылка:\n{link}\n\n"
        f"👥 Приглашено: <b>{invited}</b>\n"
        f"✅ Заработано: <b>{int(earned/100)} ₽</b>\n"
        f"⏳ Ждёт зачисления: <b>{int(pending/100)} ₽</b>"
    )
    kb = None
    if username:
        share = f"https://t.me/share/url?url={link}&text=Попробуй%20ПОЛЯНУ%20%F0%9F%8C%BF"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share)
        ]])
    await msg.answer(text, reply_markup=kb)


@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    if not message.from_user or core.pool is None:
        return
    await _send_referral(message, message.from_user.id)


@dp.message(Command("terms"))
async def cmd_terms(message: Message):
    await message.answer(
        "📄 <b>Правила и документы</b>\n\n"
        "• Пользовательское соглашение\n"
        "• Политика конфиденциальности\n"
        "• Условия партнёрской программы\n\n"
        "Документы готовятся и будут опубликованы здесь до старта приёма оплат. "
        "Оплачивая услуги бота, вы соглашаетесь с ними.\n\n"
        "<i>Бонусы партнёрской программы начисляются на внутренний баланс, "
        "тратятся внутри бота и не выводятся.</i>"
    )


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    if message.from_user:
        await message.answer(f"Твой chat_id: <code>{message.from_user.id}</code>")


@dp.message(Command("opbalance"))
async def cmd_opbalance(message: Message):
    """Admin-only: check remaining OpenRouter credit on demand."""
    if not message.from_user or message.from_user.id != ADMIN_CHAT_ID:
        return
    rem = await _openrouter_remaining_usd()
    if rem is None:
        await message.answer("Не удалось получить остаток OpenRouter.")
    else:
        await message.answer(f"💳 OpenRouter остаток: <b>${rem:.2f}</b>")


# ── Text recipe buffering ─────────────────────────────────────────────────────
# A long recipe pasted into Telegram is auto-split into multiple messages
# (>4096 chars), or a user may send it in parts. We debounce: accumulate
# consecutive text messages for a few seconds, then parse them as one recipe.

_text_buffers: dict[int, dict] = {}
_TEXT_DEBOUNCE_SEC = 3.5


async def _flush_text_buffer(user_id: int):
    try:
        await asyncio.sleep(_TEXT_DEBOUNCE_SEC)
    except asyncio.CancelledError:
        return   # a new part arrived; a fresh task will handle the flush
    buf = _text_buffers.pop(user_id, None)
    if not buf:
        return
    combined = "\n".join(buf["parts"]).strip()
    status = buf["status_msg"]
    try:
        recipe = await parse_and_save_recipe(user_id, text=combined)
        await _reply_recipe_saved(status, recipe, status_msg=status)
    except ValueError as ve:
        # not_a_recipe — tell the user instead of vanishing silently
        msg = str(ve) if str(ve) else None
        try:
            if msg:
                await status.edit_text(f"🤷 {msg}")
            else:
                await status.edit_text(
                    "🤷 Не похоже на рецепт.\n\n"
                    "Пришли ссылку, фото, голос или текст с ингредиентами.\n"
                    "Команда /add — помощь по добавлению."
                )
        except Exception:
            pass
    except Exception as e:
        await _reply_parse_error(status, e, "рецепт")


# ── Text / URL handler ────────────────────────────────────────────────────────

@dp.message(F.text & ~F.text.startswith("/"), StateFilter(None))
async def handle_text_message(message: Message, state: FSMContext):
    if not message.from_user or core.pool is None:
        log.warning("text handler: early return — from_user=%s pool=%s", bool(message.from_user), core.pool)
        return
    text = message.text or ""
    log.info("text handler: user=%s len=%d text=%r", message.from_user.id, len(text), text[:80])
    url_match = _URL_RE.search(text)

    if url_match:
        url = url_match.group(0).rstrip(".,)")   # strip trailing punctuation
        status = await message.reply("⏳ Читаю рецепт по ссылке...")
        try:
            recipe = await parse_and_save_recipe(message.from_user.id, url=url)
            await _reply_recipe_saved(message, recipe, status)
        except Exception as e:
            await _reply_parse_error(status, e, "рецепт")
        return

    # Plain text — only try if it's long enough to be a recipe (skip greetings/commands)
    if len(text) < 15:
        return   # too short (greetings, random chatter) — silently ignore

    # Buffer it: a recipe split across several messages gets combined before parsing
    uid = message.from_user.id
    buf = _text_buffers.get(uid)
    if buf:
        buf["parts"].append(text)
        if buf.get("task"):
            buf["task"].cancel()
    else:
        status = await message.reply("⏳ Собираю рецепт…")
        buf = {"parts": [text], "status_msg": status, "task": None}
        _text_buffers[uid] = buf
    buf["task"] = asyncio.create_task(_flush_text_buffer(uid))


# ── Photo handler ─────────────────────────────────────────────────────────────

async def _download(file_id: str) -> bytes:
    f = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(f.file_path, buf)
    return buf.getvalue()


async def _process_photo_album(message: Message, file_ids: list[str]):
    """Send all album photos to vision in one call; save each detected recipe."""
    status = await message.reply(f"⏳ Читаю рецепты с фото ({len(file_ids)})...")
    try:
        images = [await _download(fid) for fid in file_ids]
        recipes = await _llm_parse_images(images)
        if not recipes:
            raise ValueError("Не удалось распознать рецепт на фото")
        saved = []
        for r in recipes:
            r.setdefault("source_photo_file_id", file_ids[0])
            saved.append(await _save_parsed_recipe(message.from_user.id, r))
        await _reply_recipe_saved(message, saved[0], status)
        for r in saved[1:]:
            await _reply_recipe_saved(message, r)
    except Exception as e:
        await _reply_parse_error(status, e, "рецепты на фото")


# ponytail: in-memory album buffer. Single worker (Procfile --workers 1), album
# lands in <2s, lost-on-restart is harmless. If multi-worker later → Redis keyed by media_group_id.
_albums: dict[str, list[str]] = {}



# ── Split Photo Handler ────────────────────────────────────────────────────
@dp.message(F.photo & F.chat.type.in_({"group", "supergroup", "private"}))
async def handle_photo_for_split(message: Message):
    """Handle photo - check if it's for split receipt."""
    if not SPLIT_AVAILABLE:
        return  # Let other handlers process
    if core.pool is None:
        return

    # Check if there's an active split in this chat
    async with core.pool.acquire() as db:
        event = await db.fetchrow(
            "SELECT id FROM split_events WHERE chat_id = $1 AND status = 'active' ORDER BY id DESC LIMIT 1",
            message.chat.id
        )
    if not event:
        return  # Not a split context, let other handlers process

    # Get photo bytes
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    photo_bytes = await message.bot.download_file(file.file_path)

    # Process receipt — new signature: (message_text, is_free, receipt_id_or_None)
    try:
        async with core.pool.acquire() as db:
            msg, is_free, receipt_id = await handle_receipt_photo(
                db, message.from_user.id, photo_bytes.read(), event['id'], message.bot
            )
    except Exception as e:
        log.exception("split receipt parse failed: %s", e)
        await message.answer("❌ Не удалось обработать чек. Попробуй другое фото.")
        return

    # If we got a receipt_id → show confirm/cancel keyboard; otherwise the event keyboard
    if receipt_id:
        kb = split_confirm_keyboard(receipt_id)
    else:
        kb = split_event_keyboard(event['id'])
    await message.answer(msg, reply_markup=kb)

@dp.message(F.photo)
async def handle_photo_message(message: Message):
    if not message.from_user or core.pool is None:
        return

    mgid = message.media_group_id
    if mgid:
        # Album: Telegram sends each photo as a separate message sharing media_group_id.
        # First message drives processing after a short wait; the rest just add their file_id.
        first = mgid not in _albums              # atomic: no await before setdefault
        _albums.setdefault(mgid, []).append(message.photo[-1].file_id)
        if not first:
            return
        await asyncio.sleep(2.0)                  # let the rest of the album arrive
        file_ids = _albums.pop(mgid, [])
        if len(file_ids) == 1:
            mgid = None                           # single photo wrongly flagged → normal path
        else:
            await _process_photo_album(message, file_ids)
            return

    status = await message.reply("⏳ Читаю рецепт с фото...")
    try:
        photo = message.photo[-1]   # largest size
        recipe = await parse_and_save_recipe(
            message.from_user.id, image_bytes=await _download(photo.file_id), image_file_id=photo.file_id
        )
        await _reply_recipe_saved(message, recipe, status)
    except Exception as e:
        await _reply_parse_error(status, e, "рецепт на фото")


# ── Voice handler (FSM) ───────────────────────────────────────────────────────

def _voice_transcript_kb(transcript: str) -> InlineKeyboardMarkup:
    """Keyboard shown after transcription: confirm / edit / cancel."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Верно, сохранить",   callback_data="voice_ok")],
        [InlineKeyboardButton(text="✏️ Исправить текст",    callback_data="voice_edit")],
        [InlineKeyboardButton(text="❌ Отмена",             callback_data="voice_cancel")],
    ])


@dp.message(F.voice)
async def handle_voice_message(message: Message, state: FSMContext):
    if not message.from_user or core.pool is None:
        return
    status = await message.reply("🎙 Распознаю голос…")
    try:
        file = await bot.get_file(message.voice.file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        transcript = await _transcribe_voice(buf.getvalue())
        log.info("Voice transcript: %s", transcript[:200])
    except Exception as e:
        await status.edit_text(f"❌ Не удалось распознать голос.\n<code>{str(e)[:200]}</code>")
        return

    if not transcript or len(transcript.strip()) < 5:
        await status.edit_text("🤷 Голосовое слишком короткое или тихое — ничего не разобрал.")
        return

    # Save transcript in FSM so callbacks can use it
    await state.update_data(transcript=transcript, user_id=message.from_user.id)

    preview = transcript[:400] + ("…" if len(transcript) > 400 else "")
    await status.edit_text(
        f"📝 <b>Распознанный текст:</b>\n\n<i>{preview}</i>\n\nВсё верно?",
        reply_markup=_voice_transcript_kb(transcript),
    )


@dp.callback_query(F.data == "voice_ok")
async def voice_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    transcript = data.get("transcript", "")
    user_id = data.get("user_id") or (callback.from_user.id if callback.from_user else None)
    if not transcript or not user_id:
        await callback.answer("Сессия истекла, пришли голосовое снова", show_alert=True)
        return
    await callback.message.edit_text("⏳ Разбираю рецепт…", reply_markup=None)
    try:
        recipe = await parse_and_save_recipe(
            user_id,
            text=f"[Голосовое сообщение, расшифровка Whisper]\n\n{transcript}",
        )
        await state.clear()
        await _reply_recipe_saved(callback.message, recipe)
    except Exception as e:
        await _reply_parse_error(callback.message, e, "рецепт из голосового")
        await state.clear()
    await callback.answer()


@dp.callback_query(F.data == "voice_edit")
async def voice_edit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(VoiceStates.editing)
    await callback.message.edit_text(
        "✏️ Отправьте исправленный текст рецепта (можно дополнить/поправить):",
        reply_markup=None,
    )
    await callback.answer()


@dp.message(VoiceStates.editing, F.text)
async def voice_edited_text(message: Message, state: FSMContext):
    if not message.from_user or core.pool is None:
        return
    edited = (message.text or "").strip()
    if len(edited) < 10:
        await message.reply("Текст слишком короткий, попробуй ещё раз.")
        return
    await state.update_data(transcript=edited)
    status = await message.reply("⏳ Разбираю рецепт…")
    try:
        recipe = await parse_and_save_recipe(message.from_user.id, text=edited)
        await state.clear()
        await _reply_recipe_saved(message, recipe, status)
    except Exception as e:
        await _reply_parse_error(status, e, "рецепт из голосового")
        await state.clear()


@dp.callback_query(F.data == "voice_cancel")
async def voice_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отменено.", reply_markup=None)
    await callback.answer()


# ── Telegram Stars payments ─────────────────────────────────────────────────────

@dp.pre_checkout_query()
async def on_pre_checkout(query):
    # Top-up disabled — decline any lingering Stars invoice so no money is taken.
    try:
        await bot.answer_pre_checkout_query(
            query.id, ok=False, error_message="Пополнение временно отключено")
    except Exception:
        log.exception("pre_checkout answer failed")


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    sp = message.successful_payment
    payload = sp.invoice_payload or ""
    if not payload.startswith("topup:") or core.pool is None:
        return
    try:
        _, uid_s, rub_s = payload.split(":")
        uid = int(uid_s)
        kopecks = int(rub_s) * 100
    except Exception:
        log.warning("bad stars payload: %s", payload)
        return
    charge_id = sp.telegram_payment_charge_id   # idempotency key
    async with core.pool.acquire() as db:
        new_bal = await _credit(db, uid, kopecks, "topup_stars", ref=charge_id,
                                meta={"stars": sp.total_amount})
    await track(uid, "payment_succeeded",
                props={"kopecks": kopecks, "method": "stars", "stars": sp.total_amount, "ref": charge_id})
    try:
        await message.answer(
            f"✅ Баланс пополнен на {int(kopecks/100)} ₽ (⭐ {sp.total_amount}).\n"
            f"Текущий баланс: {int(new_bal/100)} ₽"
        )
    except Exception:
        pass


# ── /start command ────────────────────────────────────────────────────────────


# ── Split Command ──────────────────────────────────────────────────────────
@dp.message(Command("split"))
async def cmd_split(message: Message):
    """Main split command."""
    if not SPLIT_AVAILABLE:
        await message.answer("Модуль «Делёж» пока не подключён.")
        return
    if core.pool is None:
        await message.answer("Сервис запускается, попробуй через минуту.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        # Create new split event
        title = args[1].strip()
        async with core.pool.acquire() as db:
            event_id = await create_split_event(db, message.chat.id, title, message.from_user.id)
        await message.answer(
            f"✅ Делёж «{title}» создан!\n\n"
            f"Добавь участников командой /split_add @username\n"
            f"Или отправь фото чека для сканирования.",
            reply_markup=split_event_keyboard(event_id)
        )
    else:
        # Show main menu
        await message.answer(
            "💰 Делёж расходов\n\n"
            "Сканируй QR-код на чеке — бесплатно\n\n"
            "Создай новый делёж или выбери существующий:",
            reply_markup=split_main_keyboard()
        )


@dp.message(Command("split_add"))
async def cmd_split_add(message: Message):
    """Add participant to split event."""
    if not SPLIT_AVAILABLE:
        await message.answer("Модуль «Делёж» пока не подключён.")
        return
    if core.pool is None:
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /split_add @username или /split_add user_id")
        return

    # Get active split event for this chat
    async with core.pool.acquire() as db:
        event = await db.fetchrow(
            "SELECT id FROM split_events WHERE chat_id = $1 AND status = 'active' ORDER BY id DESC LIMIT 1",
            message.chat.id
        )
        if not event:
            await message.answer("Нет активного дележа. Создай: /split Название")
            return

        # Parse participant
        target = args[1]
        if target.startswith('@'):
            # Username - need to resolve
            await message.answer(f"Добавь @{target[1:]} в чат, затем он сможет присоединиться командой /split_join")
        else:
            # User ID
            try:
                user_id = int(target)
                await add_participant(db, event['id'], user_id, f"User {user_id}")
                await message.answer(f"✅ Участник добавлен!")
            except ValueError:
                await message.answer("Неверный формат. Используй @username или user_id")


@dp.message(Command("split_join"))
async def cmd_split_join(message: Message):
    """Join active split event."""
    if not SPLIT_AVAILABLE:
        await message.answer("Модуль «Делёж» пока не подключён.")
        return
    if core.pool is None:
        return

    async with core.pool.acquire() as db:
        event = await db.fetchrow(
            "SELECT id, title FROM split_events WHERE chat_id = $1 AND status = 'active' ORDER BY id DESC LIMIT 1",
            message.chat.id
        )
        if not event:
            await message.answer("Нет активного дележа в этом чате.")
            return

        added = await add_participant(db, event['id'], message.from_user.id, message.from_user.first_name)
    if added:
        await message.answer(
            f"✅ Ты присоединился к «{event['title']}»!\n\n"
            f"Отправь фото чека для сканирования."
        )
    else:
        await message.answer("Ты уже в этом дележе.")


@dp.message(Command("split_done"))
async def cmd_split_done(message: Message):
    """Calculate and send debts."""
    if not SPLIT_AVAILABLE:
        await message.answer("Модуль «Делёж» пока не подключён.")
        return
    if core.pool is None:
        return

    async with core.pool.acquire() as db:
        event = await db.fetchrow(
            "SELECT id FROM split_events WHERE chat_id = $1 AND status = 'active' ORDER BY id DESC LIMIT 1",
            message.chat.id
        )
        if not event:
            await message.answer("Нет активного дележа.")
            return

        summary = await calculate_and_notify(db, event['id'], message.bot)
        await message.answer(summary)

        # Close event
        await db.execute(
            "UPDATE split_events SET status = 'closed' WHERE id = $1",
            event['id']
        )


# ── Split Callbacks ────────────────────────────────────────────────────────
@dp.callback_query(F.data == "split_new")
async def cb_split_new(callback: CallbackQuery):
    """Prompt for new split event name."""
    await callback.message.answer("Введи название дележа:\n\nПример: /split Шашлык на даче")
    await callback.answer()


@dp.callback_query(F.data == "split_list")
async def cb_split_list(callback: CallbackQuery):
    """List user's split events."""
    if core.pool is None:
        await callback.answer()
        return
    async with core.pool.acquire() as db:
        events = await db.fetch(
            "SELECT id, title, total, status FROM split_events WHERE organizer_id = $1 ORDER BY id DESC LIMIT 5",
            callback.from_user.id
        )
    if not events:
        await callback.message.answer("У тебя пока нет дележей.\nСоздай: /split Название")
    else:
        lines = ["📋 Твои дележи:\n"]
        for e in events:
            status = "🟢" if e['status'] == 'active' else "⚫"
            lines.append(f"{status} {e['title']} — {e['total']:.0f}₽")
        await callback.message.answer("\n".join(lines))
    await callback.answer()


@dp.callback_query(F.data == "split_help")
async def cb_split_help(callback: CallbackQuery):
    """Show help."""
    await callback.message.answer(split_help_text(), reply_markup=split_pricing_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "split_premium")
async def cb_split_premium(callback: CallbackQuery):
    """Show premium features."""
    from split_module import split_premium_text
    await callback.message.answer(split_premium_text(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "split_back")
async def cb_split_back(callback: CallbackQuery):
    """Back to main menu."""
    await callback.message.edit_text(
        "💰 Делёж расходов\n\n"
        "Создай новый делёж или выбери существующий:",
        reply_markup=split_main_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("split_add_"))
async def cb_split_add(callback: CallbackQuery):
    """Prompt for receipt photo."""
    event_id = int(callback.data.split("_")[2])
    await callback.message.answer(
        "📸 Отправь фото чека\n\n"
        "🆓 QR-код на чеке — бесплатно\n"
        "💰 Фото без QR — 10₽",
        reply_markup=split_event_keyboard(event_id)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("split_members_"))
async def cb_split_members(callback: CallbackQuery):
    """Show event members."""
    if core.pool is None:
        await callback.answer()
        return
    event_id = int(callback.data.split("_")[2])
    async with core.pool.acquire() as db:
        participants = await db.fetch(
            "SELECT display_name, contributed, is_organizer FROM split_participants WHERE event_id = $1",
            event_id
        )
    if not participants:
        await callback.message.answer("Пока нет участников.")
    else:
        lines = ["👥 Участники:\n"]
        for p in participants:
            role = "👑" if p['is_organizer'] else "👤"
            lines.append(f"{role} {p['display_name']} — вложил {p['contributed']:.0f}₽")
        await callback.message.answer("\n".join(lines))
    await callback.answer()


@dp.callback_query(F.data.startswith("split_contribute_"))
async def cb_split_contribute(callback: CallbackQuery):
    """Prompt for contribution amount."""
    event_id = int(callback.data.split("_")[2])
    await callback.message.answer(
        "💰 Введи сумму своего вклада:\n\n"
        "Пример: 500"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("split_done_"))
async def cb_split_done(callback: CallbackQuery):
    """Calculate and notify."""
    if core.pool is None:
        await callback.answer()
        return
    event_id = int(callback.data.split("_")[2])
    async with core.pool.acquire() as db:
        summary = await calculate_and_notify(db, event_id, callback.message.bot)
        await callback.message.answer(summary)

        # Close event
        await db.execute(
            "UPDATE split_events SET status = 'closed' WHERE id = $1",
            event_id
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("split_confirm_"))
async def cb_split_confirm(callback: CallbackQuery):
    """Receipt confirmed — it's already saved; just acknowledge and refresh event total."""
    if core.pool is None:
        await callback.answer()
        return
    try:
        receipt_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.answer("Неверный чек", show_alert=True)
        return
    async with core.pool.acquire() as db:
        # Recompute event total from all receipts (idempotent)
        event_id = await db.fetchval(
            "SELECT event_id FROM split_receipts WHERE id=$1", receipt_id
        )
        if event_id:
            await db.execute(
                """UPDATE split_events SET total = (
                       SELECT COALESCE(SUM(total), 0) FROM split_receipts WHERE event_id = $1
                   ) WHERE id = $1""",
                event_id
            )
    await callback.message.edit_text(
        f"✅ Чек #{receipt_id} принят."
        + (f"\nОткрыть делёж: /split" if event_id else "")
    )
    await callback.answer("Принято ✓")


@dp.callback_query(F.data.startswith("split_cancel_"))
async def cb_split_cancel(callback: CallbackQuery):
    """Receipt cancelled — delete it and refund is NOT done (Vision already ran).
    We delete the row but keep the debit (the LLM cost was real)."""
    if core.pool is None:
        await callback.answer()
        return
    try:
        receipt_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.answer("Неверный чек", show_alert=True)
        return
    async with core.pool.acquire() as db:
        event_id = await db.fetchval(
            "SELECT event_id FROM split_receipts WHERE id=$1", receipt_id
        )
        await db.execute("DELETE FROM split_receipts WHERE id=$1", receipt_id)
        if event_id:
            await db.execute(
                """UPDATE split_events SET total = (
                       SELECT COALESCE(SUM(total), 0) FROM split_receipts WHERE event_id = $1
                   ) WHERE id = $1""",
                event_id
            )
    await callback.message.edit_text(f"❌ Чек #{receipt_id} отменён.")
    await callback.answer("Отменено")


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not message.from_user:
        return
    user = message.from_user
    text = message.text or ""
    arg = text.split(maxsplit=1)[1] if " " in text else None

    # Analytics: top-of-funnel + attribution source (ref_<id> / event_<id> / organic)
    await track(user.id, "user_start", src_payload=(arg or "organic"))

    # Referral capture: ?start=ref_<referrer_id> (only for a brand-new referee)
    if arg and arg.startswith("ref_") and core.pool is not None:
        try:
            referrer_id = int(arg.replace("ref_", ""))
        except ValueError:
            referrer_id = 0
        if referrer_id and referrer_id != user.id:
            try:
                async with core.pool.acquire() as db:
                    await db.execute(
                        "INSERT INTO referrals (referee_id, referrer_id) VALUES ($1,$2) "
                        "ON CONFLICT (referee_id) DO NOTHING",
                        user.id, referrer_id,
                    )
            except Exception:
                log.exception("referral capture failed")
        # fall through to the normal welcome below

    if arg and arg.startswith("event_"):
        try:
            event_id = int(arg.replace("event_", ""))
        except ValueError:
            await message.answer("Неверная ссылка.", reply_markup=ReplyKeyboardRemove())
            return

        if core.pool is None:
            await message.answer("Сервис запускается, попробуйте через минуту.", reply_markup=ReplyKeyboardRemove())
            return

        async with core.pool.acquire() as db:
            event = await db.fetchrow("SELECT * FROM events WHERE id=$1", event_id)

        if not event:
            await message.answer("Событие не найдено или удалено.", reply_markup=ReplyKeyboardRemove())
            return

        async with core.pool.acquire() as db:
            was_new = not await db.fetchval(
                "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user.id
            )
            await db.execute(
                """
                INSERT INTO collaborators (event_id, telegram_user_id, first_name, username, role)
                VALUES ($1,$2,$3,$4,'collaborator')
                ON CONFLICT (event_id, telegram_user_id) DO UPDATE SET first_name=EXCLUDED.first_name
                """,
                event_id, user.id, user.first_name, user.username or "",
            )
        if was_new and event["telegram_user_id"] != user.id:
            await track(user.id, "guest_joined",
                        props={"event_id": event_id, "owner_id": event["telegram_user_id"], "via": "bot"},
                        event_ref=event_id)

        ev_date = "дата не указана"
        if event["event_date"]:
            try:
                d = event["event_date"]
                months = ["янв","фев","мар","апр","мая","июн","июл","авг","сен","окт","ноя","дек"]
                ev_date = f"{d.day} {months[d.month-1]}, {d.hour:02d}:{d.minute:02d}"
            except Exception:
                ev_date = str(event["event_date"])[:16].replace("T", " ")

        miniapp_url = f"{FRONTEND_URL}?startapp=event_{event_id}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🌿 Открыть ПОЛЯНУ", web_app=WebAppInfo(url=miniapp_url))
        ]])

        await message.answer(
            f"🎉 <b>{user.first_name}</b>, вас пригласили!\n\n"
            f"<b>{event['name']}</b>\n"
            f"📅 {ev_date}\n\n"
            f"Нажмите кнопку, чтобы открыть ПОЛЯНУ:",
            reply_markup=kb,
        )
    elif arg and arg.startswith("recipe_"):
        # Viral recipe sharing: ?start=recipe_<token> → clone recipe into user's library
        token = arg.replace("recipe_", "")
        if core.pool is None or not token or len(token) > 64:
            await message.answer("Сервис запускается, попробуйте через минуту.")
            return

        import secrets as _secrets
        async with core.pool.acquire() as db:
            src = await db.fetchrow("SELECT * FROM recipes WHERE share_token=$1", token)
            if not src:
                await message.answer("Рецепт не найден или удалён.")
                return

            # Idempotent: skip if this user already has a recipe with the same source_url+name
            existing = None
            if src["source_url"]:
                existing = await db.fetchrow(
                    "SELECT id, share_token FROM recipes WHERE user_id=$1 AND source_url=$2",
                    user.id, src["source_url"],
                )
            if not existing:
                existing = await db.fetchrow(
                    "SELECT id, share_token FROM recipes WHERE user_id=$1 AND name=$2",
                    user.id, src["name"],
                )

            if existing:
                # Already in library — show it, don't duplicate
                ing_count = await db.fetchval(
                    "SELECT COUNT(*) FROM ingredients WHERE recipe_id=$1", existing["id"]
                )
                await _reply_recipe_saved(message, {
                    "id": existing["id"], "name": src["name"], "emoji": src["emoji"],
                    "servings": src["servings"], "cook_time_minutes": src["cook_time_minutes"],
                    "category": src["category"], "ingredients_count": ing_count or 0,
                    "already_exists": True,
                })
                await track(user.id, "recipe_imported_via_share",
                            props={"token": token, "original_user_id": int(src["user_id"] or 0),
                                   "duplicate": True})
                return

            # Clone: new row owned by this user, new share_token, copy photo + metadata
            new_token = _secrets.token_urlsafe(16)
            cloned = await db.fetchrow(
                """
                INSERT INTO recipes
                    (user_id, name, name_original, emoji, source_url, source_type, original_language,
                     servings, cook_time_minutes, category, source_photo_file_id, share_token)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                RETURNING id
                """,
                user.id, src["name"], src["name_original"], src["emoji"] or "🍽",
                src["source_url"], (src["source_type"] or "shared") if not src["source_type"] else src["source_type"],
                src["original_language"], src["servings"], src["cook_time_minutes"],
                src["category"], src["source_photo_file_id"], new_token,
            )
            new_id = cloned["id"]

            # Copy ingredients
            src_ings = await db.fetch(
                "SELECT name, qty, unit, category, sort_order FROM ingredients WHERE recipe_id=$1 ORDER BY sort_order, id",
                src["id"],
            )
            for ing in src_ings:
                await db.execute(
                    "INSERT INTO ingredients (recipe_id, name, qty, unit, category, sort_order) "
                    "VALUES ($1,$2,$3,$4,$5,$6)",
                    new_id, ing["name"], ing["qty"], ing["unit"], ing["category"], ing["sort_order"],
                )

            # Copy steps
            src_steps = await db.fetch(
                "SELECT step_number, text FROM recipe_steps WHERE recipe_id=$1 ORDER BY step_number",
                src["id"],
            )
            for st in src_steps:
                await db.execute(
                    "INSERT INTO recipe_steps (recipe_id, step_number, text) VALUES ($1,$2,$3)",
                    new_id, st["step_number"], st["text"],
                )

            await track(user.id, "recipe_imported_via_share",
                        props={"token": token, "original_user_id": int(src["user_id"] or 0),
                               "recipe_id": new_id, "duplicate": False})

            await _reply_recipe_saved(message, {
                "id": new_id, "name": src["name"], "emoji": src["emoji"],
                "servings": src["servings"], "cook_time_minutes": src["cook_time_minutes"],
                "category": src["category"], "ingredients_count": len(src_ings),
                "already_exists": False,
            })
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌿 Открыть ПОЛЯНУ", web_app=WebAppInfo(url=FRONTEND_URL))],
            [
                InlineKeyboardButton(text="💰 Партнёрам", callback_data="show_ref"),
                InlineKeyboardButton(text="📄 Правила", callback_data="show_terms"),
            ],
        ]) if FRONTEND_URL else None
        await message.answer(
            f"🌿 <b>Привет, {user.first_name}!</b>\n\n"
            f"ПОЛЯНА — твой кулинарный штаб:\n\n"
            f"📋 <b>Собирай рецепты</b> — пришли ссылку, фото, голос или текст\n"
            f"🎉 <b>Планируй застолья</b> с друзьями\n"
            f"🛒 <b>Общая корзина</b> — ингредиенты всех рецептов в одном списке\n"
            f"💰 <b>Делите расходы</b> — фото чека, и бот посчитает кто кому должен\n"
            f"📤 <b>Делись рецептами</b> — друг жмёт одну кнопку, и рецепт у него\n\n"
            f"<i>С чего начать?</i> Пришли мне ссылку на рецепт или /add\n\n"
            f"👇 Или открой ПОЛЯНУ",
            reply_markup=kb,
        )


@dp.callback_query(F.data == "show_ref")
async def cb_show_ref(callback: CallbackQuery):
    if core.pool is not None and callback.from_user and callback.message:
        await _send_referral(callback.message, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data.startswith("share_recipe_"))
async def cb_share_recipe(callback: CallbackQuery):
    """Generate a forwardable recipe card with a 'Save' deep-link button.
    The url= button survives Telegram forward, so the friend can install the
    recipe into their own library with one tap."""
    if not callback.from_user or core.pool is None:
        await callback.answer()
        return
    try:
        recipe_id = int(callback.data.replace("share_recipe_", ""))
    except ValueError:
        await callback.answer("Ошибка", show_alert=True)
        return
    async with core.pool.acquire() as db:
        rec = await db.fetchrow("SELECT * FROM recipes WHERE id=$1", recipe_id)
        if not rec:
            await callback.answer("Рецепт не найден", show_alert=True)
            return
        token = rec["share_token"]
        # Backfill token for old recipes created before the migration
        if not token:
            import secrets as _s
            token = _s.token_urlsafe(16)
            await db.execute("UPDATE recipes SET share_token=$1 WHERE id=$2", token, recipe_id)
        ing_count = await db.fetchval("SELECT COUNT(*) FROM ingredients WHERE recipe_id=$1", recipe_id)
        owner_name = callback.from_user.first_name or "Друг"

    username = await _get_bot_username()
    save_url = f"https://t.me/{username}/?start=recipe_{token}" if username else None
    if not save_url:
        await callback.answer("Бот недоступен", show_alert=True)
        return

    # Forwardable card: text + url= button (survives forward, unlike web_app)
    ct_str = f"⏱ {rec['cook_time_minutes']} мин · " if rec["cook_time_minutes"] else ""
    cat_str = f"[{rec['category']}] " if rec["category"] else ""
    serv_str = f"🍽 {rec['servings']} порц. · " if rec["servings"] else ""
    body = (
        f"🍲 <b>{rec['name']}</b>\n"
        f"{cat_str}{serv_str}{ct_str}🥕 {ing_count or 0} ингр.\n\n"
        f"<i>{owner_name} делится рецептом из ПОЛЯНЫ.</i>\n\n"
        f"Нажми кнопку, чтобы сохранить рецепт в свою книгу 👇"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💾 Сохранить в книгу рецептов", url=save_url),
    ]])
    await callback.message.answer(body, reply_markup=kb)
    await track(callback.from_user.id, "recipe_shared",
                props={"recipe_id": recipe_id, "token": token})
    await callback.answer("Карточка готова — перешли её другу ✂️")


@dp.callback_query(F.data == "show_terms")
async def cb_show_terms(callback: CallbackQuery):
    await cmd_terms(callback.message)
    await callback.answer()
