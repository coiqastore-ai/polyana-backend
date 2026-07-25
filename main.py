import os, hashlib, hmac, json, asyncio, secrets, time, logging, io, re, base64, urllib, string, uuid
import httpx
import invite
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl
import asyncpg
from fastapi import FastAPI, HTTPException, Header, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.types import (
    BotCommand, BotCommandScopeAllPrivateChats,
    MenuButtonWebApp, Message, CallbackQuery,
    ReplyKeyboardRemove, WebAppInfo,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, LabeledPrice,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
    SwitchInlineQueryChosenChat,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ── Split Expenses Module ─────────────────────────────────────────────────
try:
    from split_module import (
        scan_qr_from_image, parse_fns_qr, fetch_fns_receipt,
        format_receipt_items, create_split_event, add_participant,
        add_receipt_to_split, set_contribution, calculate_and_notify,
        handle_receipt_photo, split_main_keyboard, split_event_keyboard,
        split_confirm_keyboard, split_pricing_keyboard, split_help_text,
        PHOTO_PARSE_PRICE
    )
    SPLIT_AVAILABLE = True
except ImportError:
    SPLIT_AVAILABLE = False
    log.warning("split_module not found — split features disabled")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("polyana")

ENV = os.environ.get
BOT_TOKEN = ENV("BOT_TOKEN", "")
DATABASE_URL = ENV("DATABASE_URL", "")
FRONTEND_URL = ENV("FRONTEND_URL", "")
INTERNAL_API_KEY = ENV("INTERNAL_API_KEY", "")
PORT = int(ENV("PORT", "8000"))
OPENROUTER_KEY = ENV("OPENROUTER_API_KEY", "")
OPENROUTER_PROXY_URL = ENV("OPENROUTER_PROXY_URL", "")
OPENROUTER_PROXY_SECRET = ENV("OPENROUTER_PROXY_SECRET", "")
YOOKASSA_SHOP_ID = ENV("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = ENV("YOOKASSA_SECRET_KEY", "")
# 54-ФЗ receipt: VAT code (1 = без НДС for ИП на УСН/патенте). Set to "" to skip receipts.
YOOKASSA_VAT_CODE = ENV("YOOKASSA_VAT_CODE", "1")

# Admin alerts (low-balance / outages) go to this Telegram chat id. @chigra89.
ADMIN_CHAT_ID = int(ENV("ADMIN_CHAT_ID", "257938367") or 0)
SUPPORT_HANDLE = ENV("SUPPORT_HANDLE", "@chigra89")
OPENROUTER_LOW_BALANCE_USD = float(ENV("OPENROUTER_LOW_BALANCE_USD", "5"))

# Prices in kopecks
PRICE_AI_INVITE = 4900   # 49 ₽ — AI invitation (includes 1 free reroll)

# Telegram Stars: how many ₽ of balance one Star credits. Buyer pays ~1.7-2₽
# per Star in-app, so crediting ~1.7₽/Star keeps it roughly fair. TUNE THIS.
STAR_RUB_RATE = 1.7

# Referral program config
REFERRAL_ENABLED = ENV("REFERRAL_ENABLED", "true").lower() == "true"
REFERRAL_REWARD_PERCENT_BP = int(ENV("REFERRAL_REWARD_PERCENT_BP", "1000"))  # 1000 = 10%
REFERRAL_HOLD_DAYS = int(ENV("REFERRAL_HOLD_DAYS", "7"))
REFERRAL_REWARD_LIFETIME_MONTHS = int(ENV("REFERRAL_REWARD_LIFETIME_MONTHS", "0"))  # 0 = unlimited
REFERRAL_MAX_REWARD_PER_PAYMENT_POINTS = ENV("REFERRAL_MAX_REWARD_PER_PAYMENT_POINTS")
REFERRAL_MAX_REWARD_PER_MONTH_POINTS = ENV("REFERRAL_MAX_REWARD_PER_MONTH_POINTS")
REFERRAL_MIN_PAYMENT_AMOUNT_MINOR = int(ENV("REFERRAL_MIN_PAYMENT_AMOUNT_MINOR", "0"))
POINTS_PER_RUBLE = int(ENV("POINTS_PER_RUBLE", "1"))

# Legacy aliases (for backward compat with existing code)
REFERRAL_PERCENT = REFERRAL_REWARD_PERCENT_BP // 100  # 10
REFERRAL_HOLD_HOURS = REFERRAL_HOLD_DAYS * 24

# Legal document config
LEGAL_OPERATOR_FULL_NAME = ENV("LEGAL_OPERATOR_FULL_NAME", "")
LEGAL_OPERATOR_SHORT_NAME = ENV("LEGAL_OPERATOR_SHORT_NAME", "")
LEGAL_OPERATOR_STATUS = ENV("LEGAL_OPERATOR_STATUS", "")  # ИП / ООО / Самозанятый
LEGAL_INN = ENV("LEGAL_INN", "")
LEGAL_OGRN_OR_OGRNIP = ENV("LEGAL_OGRN_OR_OGRNIP", "")
LEGAL_LEGAL_ADDRESS = ENV("LEGAL_LEGAL_ADDRESS", "")
LEGAL_CONTACT_EMAIL = ENV("LEGAL_CONTACT_EMAIL", "")
LEGAL_PRIVACY_EMAIL = ENV("LEGAL_PRIVACY_EMAIL", "")
LEGAL_SUPPORT_TELEGRAM = ENV("LEGAL_SUPPORT_TELEGRAM", "@chigra89")

# Data retention config
TEMP_FILE_RETENTION_HOURS = int(ENV("TEMP_FILE_RETENTION_HOURS", "24"))
RAW_IMPORT_RETENTION_DAYS = int(ENV("RAW_IMPORT_RETENTION_DAYS", "30"))
AI_LOG_RETENTION_DAYS = int(ENV("AI_LOG_RETENTION_DAYS", "90"))
DELETED_ACCOUNT_RETENTION_DAYS = int(ENV("DELETED_ACCOUNT_RETENTION_DAYS", "30"))

# Onboarding config
ONBOARDING_VERSION = "1.0"

# Feature flags — controls what's shown in welcome/onboarding
FEATURE_RECIPE_IMPORT_IMAGE = ENV("FEATURE_RECIPE_IMPORT_IMAGE", "true").lower() == "true"
FEATURE_RECIPE_IMPORT_VOICE = ENV("FEATURE_RECIPE_IMPORT_VOICE", "true").lower() == "true"
FEATURE_RECIPE_IMPORT_URL = ENV("FEATURE_RECIPE_IMPORT_URL", "true").lower() == "true"
FEATURE_AI_RECIPE_GENERATION = ENV("FEATURE_AI_RECIPE_GENERATION", "true").lower() == "true"
FEATURE_AI_IMAGE_GENERATION = ENV("FEATURE_AI_IMAGE_GENERATION", "true").lower() == "true"
FEATURE_EVENTS = ENV("FEATURE_EVENTS", "true").lower() == "true"
FEATURE_SHOPPING_LIST = ENV("FEATURE_SHOPPING_LIST", "true").lower() == "true"
FEATURE_EXPENSE_SPLIT = ENV("FEATURE_EXPENSE_SPLIT", "true").lower() == "true"
FEATURE_RECEIPT_RECOGNITION = ENV("FEATURE_RECEIPT_RECOGNITION", "true").lower() == "true"
FEATURE_REFERRALS = ENV("FEATURE_REFERRALS", "true").lower() == "true"
FEATURE_PAYMENTS = ENV("FEATURE_PAYMENTS", "true").lower() == "true"
FEATURE_PAYMENTS_STARS = ENV("FEATURE_PAYMENTS_STARS", "true").lower() == "true"
FEATURE_PAYMENTS_YOOKASSA_WEB = ENV("FEATURE_PAYMENTS_YOOKASSA_WEB", "true").lower() == "true"
FEATURE_BALANCE = ENV("FEATURE_BALANCE", "true").lower() == "true"
FEATURE_AI_BILLING = ENV("FEATURE_AI_BILLING", "true").lower() == "true"
FEATURE_PAYMENT_RECONCILIATION = ENV("FEATURE_PAYMENT_RECONCILIATION", "true").lower() == "true"
WELCOME_POINTS = int(ENV("WELCOME_POINTS", "0"))

# Feature lists for welcome screen (flag, icon, title)
FREE_FEATURES = [
    {"flag": True, "icon": "📚", "title": "хранение и поиск рецептов"},
    {"flag": True, "icon": "✏️", "title": "редактирование и избранное"},
    {"flag": True, "icon": "🍽", "title": "пересчёт ингредиентов на нужное число порций"},
    {"flag": True, "icon": "📤", "title": "отправка рецептов друзьям"},
    {"flag": True, "icon": "💾", "title": "сохранение рецептов друзей"},
    {"flag": FEATURE_EVENTS, "icon": "🎉", "title": "создание меню для событий"},
    {"flag": FEATURE_SHOPPING_LIST, "icon": "🛒", "title": "единый список покупок"},
    {"flag": FEATURE_EXPENSE_SPLIT, "icon": "💰", "title": "распределение покупок и расчёт расходов"},
]

AI_FEATURES = [
    {"flag": FEATURE_RECIPE_IMPORT_IMAGE, "icon": "✨", "title": "распознавание фото, голоса и сложных ссылок"},
    {"flag": FEATURE_AI_RECIPE_GENERATION, "icon": "🍲", "title": "создание нового рецепта по вашему описанию"},
    {"flag": FEATURE_AI_IMAGE_GENERATION, "icon": "🖼", "title": "создание изображения блюда"},
    {"flag": True, "icon": "💡", "title": "умные рекомендации и подбор меню"},
    {"flag": FEATURE_RECEIPT_RECOGNITION, "icon": "🧾", "title": "распознавание чеков"},
]

# ── HTML escape helper ────────────────────────────────────────────────────────
import html as _html_mod

def _esc(text: str) -> str:
    """Escape text for Telegram HTML parse_mode."""
    return _html_mod.escape(str(text)) if text else ""


# ── Wallet Service ────────────────────────────────────────────────────────────

class WalletService:
    """Manages paid_points, bonus_points, pending_bonus_points, bonus_debt_points.
    All mutations go through ledger entries."""

    @staticmethod
    async def get_balance(db, user_id: int) -> dict:
        row = await db.fetchrow(
            "SELECT * FROM wallets WHERE user_id=$1", user_id
        )
        if not row:
            return {
                "paid_points": 0, "bonus_points": 0,
                "pending_bonus_points": 0, "bonus_debt_points": 0,
                "total_available_points": 0,
            }
        paid = row["paid_points"] or 0
        bonus = row["bonus_points"] or 0
        return {
            "paid_points": paid,
            "bonus_points": bonus,
            "pending_bonus_points": row["pending_bonus_points"] or 0,
            "bonus_debt_points": row["bonus_debt_points"] or 0,
            "total_available_points": paid + bonus,
        }

    @staticmethod
    async def credit_paid_points(db, user_id: int, points: int,
                                  reference_type: str = None, reference_id: str = None,
                                  idempotency_key: str = None, metadata: dict = None) -> int:
        """Credit paid points. Returns new paid balance."""
        meta_json = json.dumps(metadata) if metadata else None
        async with db.transaction():
            # Ensure wallet exists
            await db.execute(
                "INSERT INTO wallets (user_id, paid_points) VALUES ($1, 0) ON CONFLICT DO NOTHING",
                user_id
            )
            row = await db.fetchrow(
                "UPDATE wallets SET paid_points = paid_points + $2, updated_at = NOW() "
                "WHERE user_id = $1 RETURNING paid_points",
                user_id, points
            )
            new_paid = row["paid_points"]
            # Ledger entry
            try:
                await db.execute(
                    "INSERT INTO wallet_ledger "
                    "(user_id, wallet_type, amount, transaction_type, reference_type, reference_id, "
                    "idempotency_key, balance_after, metadata) "
                    "VALUES ($1, 'paid', $2, 'payment_credit', $3, $4, $5, $6, $7)",
                    user_id, points, reference_type, reference_id,
                    idempotency_key, new_paid, meta_json
                )
            except asyncpg.UniqueViolationError:
                pass  # Idempotent — already recorded
            return new_paid

    @staticmethod
    async def credit_bonus_points(db, user_id: int, points: int,
                                   reference_type: str = None, reference_id: str = None,
                                   idempotency_key: str = None, metadata: dict = None) -> int:
        """Credit bonus points. Returns new bonus balance."""
        meta_json = json.dumps(metadata) if metadata else None
        async with db.transaction():
            await db.execute(
                "INSERT INTO wallets (user_id, bonus_points) VALUES ($1, 0) ON CONFLICT DO NOTHING",
                user_id
            )
            row = await db.fetchrow(
                "UPDATE wallets SET bonus_points = bonus_points + $2, updated_at = NOW() "
                "WHERE user_id = $1 RETURNING bonus_points",
                user_id, points
            )
            new_bonus = row["bonus_points"]
            try:
                await db.execute(
                    "INSERT INTO wallet_ledger "
                    "(user_id, wallet_type, amount, transaction_type, reference_type, reference_id, "
                    "idempotency_key, balance_after, metadata) "
                    "VALUES ($1, 'bonus', $2, 'referral_activated', $3, $4, $5, $6, $7)",
                    user_id, points, reference_type, reference_id,
                    idempotency_key, new_bonus, meta_json
                )
            except asyncpg.UniqueViolationError:
                pass
            return new_bonus

    @staticmethod
    async def credit_pending_bonus(db, user_id: int, points: int,
                                    reference_type: str = None, reference_id: str = None,
                                    idempotency_key: str = None, metadata: dict = None) -> int:
        """Credit pending bonus points. Returns new pending balance."""
        meta_json = json.dumps(metadata) if metadata else None
        async with db.transaction():
            await db.execute(
                "INSERT INTO wallets (user_id, pending_bonus_points) VALUES ($1, 0) ON CONFLICT DO NOTHING",
                user_id
            )
            row = await db.fetchrow(
                "UPDATE wallets SET pending_bonus_points = pending_bonus_points + $2, updated_at = NOW() "
                "WHERE user_id = $1 RETURNING pending_bonus_points",
                user_id, points
            )
            new_pending = row["pending_bonus_points"]
            try:
                await db.execute(
                    "INSERT INTO wallet_ledger "
                    "(user_id, wallet_type, amount, transaction_type, reference_type, reference_id, "
                    "idempotency_key, balance_after, metadata) "
                    "VALUES ($1, 'pending_bonus', $2, 'referral_pending', $3, $4, $5, $6, $7)",
                    user_id, points, reference_type, reference_id,
                    idempotency_key, new_pending, meta_json
                )
            except asyncpg.UniqueViolationError:
                pass
            return new_pending

    @staticmethod
    async def debit_for_ai(db, user_id: int, cost_points: int,
                            reference_type: str = None, reference_id: str = None,
                            metadata: dict = None) -> tuple[bool, int]:
        """Debit points for AI usage: bonus first, then paid. Returns (success, remaining_paid)."""
        meta_json = json.dumps(metadata) if metadata else None
        async with db.transaction():
            row = await db.fetchrow(
                "SELECT * FROM wallets WHERE user_id=$1 FOR UPDATE", user_id
            )
            if not row:
                return False, 0
            bonus = row["bonus_points"] or 0
            paid = row["paid_points"] or 0
            total = bonus + paid
            if total < cost_points:
                return False, paid

            # Deduct bonus first
            bonus_used = min(bonus, cost_points)
            remaining = cost_points - bonus_used
            paid_used = remaining

            new_bonus = bonus - bonus_used
            new_paid = paid - paid_used

            await db.execute(
                "UPDATE wallets SET bonus_points=$2, paid_points=$3, updated_at=NOW() "
                "WHERE user_id=$1",
                user_id, new_bonus, new_paid
            )

            # Ledger entries
            if bonus_used > 0:
                try:
                    await db.execute(
                        "INSERT INTO wallet_ledger "
                        "(user_id, wallet_type, amount, transaction_type, reference_type, reference_id, "
                        "balance_after, metadata) "
                        "VALUES ($1, 'bonus', $2, 'ai_usage', $3, $4, $5, $6)",
                        user_id, -bonus_used, reference_type, reference_id,
                        new_bonus, meta_json
                    )
                except asyncpg.UniqueViolationError:
                    pass
            if paid_used > 0:
                try:
                    await db.execute(
                        "INSERT INTO wallet_ledger "
                        "(user_id, wallet_type, amount, transaction_type, reference_type, reference_id, "
                        "balance_after, metadata) "
                        "VALUES ($1, 'paid', $2, 'ai_usage', $3, $4, $5, $6)",
                        user_id, -paid_used, reference_type, reference_id,
                        new_paid, meta_json
                    )
                except asyncpg.UniqueViolationError:
                    pass

            return True, new_paid

    @staticmethod
    async def refund_ai_charge(db, user_id: int, bonus_used: int, paid_used: int,
                                reference_type: str = None, reference_id: str = None,
                                metadata: dict = None) -> None:
        """Refund points after AI failure."""
        meta_json = json.dumps(metadata) if metadata else None
        async with db.transaction():
            await db.execute(
                "UPDATE wallets SET "
                "bonus_points = bonus_points + $2, "
                "paid_points = paid_points + $3, "
                "updated_at = NOW() "
                "WHERE user_id=$1",
                user_id, bonus_used, paid_used
            )
            if bonus_used > 0:
                try:
                    await db.execute(
                        "INSERT INTO wallet_ledger "
                        "(user_id, wallet_type, amount, transaction_type, reference_type, reference_id, "
                        "metadata) "
                        "VALUES ($1, 'bonus', $2, 'ai_usage_refund', $3, $4, $5)",
                        user_id, bonus_used, reference_type, reference_id, meta_json
                    )
                except asyncpg.UniqueViolationError:
                    pass
            if paid_used > 0:
                try:
                    await db.execute(
                        "INSERT INTO wallet_ledger "
                        "(user_id, wallet_type, amount, transaction_type, reference_type, reference_id, "
                        "metadata) "
                        "VALUES ($1, 'paid', $2, 'ai_usage_refund', $3, $4, $5)",
                        user_id, paid_used, reference_type, reference_id, meta_json
                    )
                except asyncpg.UniqueViolationError:
                    pass

    @staticmethod
    async def activate_pending_rewards(db, user_id: int) -> int:
        """Move matured pending bonus to available bonus. Returns count activated."""
        async with db.transaction():
            # Find matured rewards
            rows = await db.fetch(
                "SELECT id, reward_points FROM referral_rewards "
                "WHERE referrer_user_id=$1 AND status='pending' AND available_at <= NOW() "
                "FOR UPDATE",
                user_id
            )
            if not rows:
                return 0

            total_to_activate = sum(r["reward_points"] for r in rows)

            # Debit pending, credit bonus
            await db.execute(
                "UPDATE wallets SET "
                "pending_bonus_points = pending_bonus_points - $2, "
                "bonus_points = bonus_points + $2, "
                "updated_at = NOW() "
                "WHERE user_id = $1",
                user_id, total_to_activate
            )

            # Update reward statuses
            reward_ids = [r["id"] for r in rows]
            await db.execute(
                "UPDATE referral_rewards SET status='available', activated_at=NOW(), updated_at=NOW() "
                "WHERE id = ANY($1)",
                reward_ids
            )

            # Ledger
            try:
                await db.execute(
                    "INSERT INTO wallet_ledger "
                    "(user_id, wallet_type, amount, transaction_type, reference_type, "
                    "balance_after, metadata) "
                    "VALUES ($1, 'bonus', $2, 'referral_activated', 'referral_rewards', $3, $4)",
                    user_id, total_to_activate,
                    json.dumps({"reward_ids": [str(rid) for rid in reward_ids]}),
                    (await db.fetchrow("SELECT bonus_points FROM wallets WHERE user_id=$1", user_id))["bonus_points"]
                )
            except asyncpg.UniqueViolationError:
                pass

            return len(rows)

    @staticmethod
    async def get_available_balance(db, user_id: int) -> dict:
        """Get available balance breakdown including reservations."""
        row = await db.fetchrow("SELECT * FROM wallets WHERE user_id=$1", user_id)
        if not row:
            return {"paid": 0, "bonus": 0, "reserved": 0, "available": 0,
                    "paid_debt": 0, "bonus_debt": 0, "pending": 0}
        paid = row["paid_points"] or 0
        bonus = row["bonus_points"] or 0
        reserved = row["reserved_points"] or 0
        return {
            "paid": paid,
            "bonus": bonus,
            "reserved": reserved,
            "available": max(0, paid + bonus - reserved),
            "paid_debt": row["paid_debt_points"] or 0,
            "bonus_debt": row["bonus_debt_points"] or 0,
            "pending": row["pending_bonus_points"] or 0,
        }

    @staticmethod
    async def reserve_for_ai(db, user_id: int, points: int,
                              operation_type: str, operation_id) -> bool:
        """Reserve points for an AI operation. Returns True if reserved."""
        now = datetime.now(timezone.utc)
        async with db.transaction():
            row = await db.fetchrow(
                "SELECT * FROM wallets WHERE user_id=$1 FOR UPDATE", user_id)
            if not row:
                return False
            paid = row["paid_points"] or 0
            bonus = row["bonus_points"] or 0
            reserved = row["reserved_points"] or 0
            available = paid + bonus - reserved
            if available < points:
                return False
            # Deduct from bonus first, then paid
            bonus_deduct = min(bonus, points)
            paid_deduct = points - bonus_deduct
            await db.execute(
                "UPDATE wallets SET "
                "bonus_points = bonus_points - $2, "
                "paid_points = paid_points - $3, "
                "reserved_points = reserved_points + $4, "
                "updated_at = NOW() "
                "WHERE user_id = $1",
                user_id, bonus_deduct, paid_deduct, points)
            await db.execute(
                "INSERT INTO wallet_reservations "
                "(user_id, operation_type, operation_id, points, status, expires_at, created_at) "
                "VALUES ($1,$2,$3,$4,'reserved',$5,$6)",
                user_id, operation_type, str(operation_id), points,
                now + timedelta(minutes=10), now)
            # Ledger entries
            if bonus_deduct > 0:
                try:
                    await db.execute(
                        "INSERT INTO wallet_ledger "
                        "(user_id, wallet_type, amount, transaction_type, idempotency_key, "
                        "paid_balance_after, bonus_balance_after, reserved_balance_after, created_at) "
                        "VALUES ($1,'bonus',$2,'ai_reservation',$3,$4,$5,$6,$7)",
                        user_id, -bonus_deduct, f"reserve:{operation_id}",
                        paid - paid_deduct, bonus - bonus_deduct, reserved + points, now)
                except asyncpg.UniqueViolationError:
                    pass
            if paid_deduct > 0:
                try:
                    await db.execute(
                        "INSERT INTO wallet_ledger "
                        "(user_id, wallet_type, amount, transaction_type, idempotency_key, "
                        "paid_balance_after, bonus_balance_after, reserved_balance_after, created_at) "
                        "VALUES ($1,'paid',$2,'ai_reservation',$3,$4,$5,$6,$7)",
                        user_id, -paid_deduct, f"reserve_paid:{operation_id}",
                        paid - paid_deduct, bonus - bonus_deduct, reserved + points, now)
                except asyncpg.UniqueViolationError:
                    pass
        return True

    @staticmethod
    async def commit_reservation(db, operation_id, actual_cost_usd: float = None) -> bool:
        """Commit a reservation — points are consumed. Returns True if committed."""
        now = datetime.now(timezone.utc)
        async with db.transaction():
            res = await db.fetchrow(
                "SELECT * FROM wallet_reservations WHERE operation_id=$1 FOR UPDATE",
                str(operation_id))
            if not res or res["status"] != "reserved":
                return False
            await db.execute(
                "UPDATE wallet_reservations SET status='committed', committed_at=$2 "
                "WHERE operation_id=$1",
                str(operation_id), now)
            await db.execute(
                "UPDATE wallets SET reserved_points = GREATEST(0, reserved_points - $2), "
                "updated_at = NOW() WHERE user_id=$1",
                res["user_id"], res["points"])
        return True

    @staticmethod
    async def release_reservation(db, operation_id, reason: str = "error") -> bool:
        """Release a reservation — points returned to available balance."""
        now = datetime.now(timezone.utc)
        async with db.transaction():
            res = await db.fetchrow(
                "SELECT * FROM wallet_reservations WHERE operation_id=$1 FOR UPDATE",
                str(operation_id))
            if not res or res["status"] != "reserved":
                return False
            points = res["points"]
            await db.execute(
                "UPDATE wallet_reservations SET status='released', released_at=$2 "
                "WHERE operation_id=$1",
                str(operation_id), now)
            # Return points: bonus first, then paid (reverse of reserve)
            row = await db.fetchrow(
                "SELECT * FROM wallets WHERE user_id=$1 FOR UPDATE", res["user_id"])
            bonus = row["bonus_points"] or 0
            bonus_restore = min(points, points)  # restore proportionally
            paid_restore = points - bonus_restore
            await db.execute(
                "UPDATE wallets SET "
                "bonus_points = bonus_points + $2, "
                "paid_points = paid_points + $3, "
                "reserved_points = GREATEST(0, reserved_points - $4), "
                "updated_at = NOW() "
                "WHERE user_id = $1",
                res["user_id"], bonus_restore, paid_restore, points)
            try:
                await db.execute(
                    "INSERT INTO wallet_ledger "
                    "(user_id, wallet_type, amount, transaction_type, idempotency_key, "
                    "paid_balance_after, bonus_balance_after, reserved_balance_after, metadata, created_at) "
                    "VALUES ($1,'bonus',$2,'ai_reservation_release',$3,$4,$5,$6,$7,$8)",
                    res["user_id"], points, f"release:{operation_id}",
                    (row["paid_points"] or 0) + paid_restore,
                    (row["bonus_points"] or 0) + bonus_restore,
                    max(0, (row["reserved_points"] or 0) - points),
                    json.dumps({"reason": reason}), now)
            except asyncpg.UniqueViolationError:
                pass
        return True

    @staticmethod
    async def credit_purchase(db, order) -> dict:
        """Unified credit for any successful payment order.
        Handles debt repayment, grant creation, and ledger entries.
        Returns new balance dict."""
        now = datetime.now(timezone.utc)
        uid = order["user_id"]
        total_pts = order["total_points"]
        async with db.transaction():
            row = await db.fetchrow(
                "SELECT * FROM wallets WHERE user_id=$1 FOR UPDATE", uid)
            if not row:
                await db.execute(
                    "INSERT INTO wallets (user_id, paid_points) VALUES ($1,0) ON CONFLICT DO NOTHING", uid)
                row = await db.fetchrow(
                    "SELECT * FROM wallets WHERE user_id=$1 FOR UPDATE", uid)
            paid_debt = row["paid_debt_points"] or 0
            # Repay debt first
            debt_repay = min(paid_debt, total_pts)
            remaining = total_pts - debt_repay
            if debt_repay > 0:
                await db.execute(
                    "UPDATE wallets SET paid_debt_points = paid_debt_points - $2, updated_at=NOW() "
                    "WHERE user_id=$1", uid, debt_repay)
                try:
                    await db.execute(
                        "INSERT INTO wallet_ledger "
                        "(user_id, wallet_type, amount, transaction_type, idempotency_key, metadata, created_at) "
                        "VALUES ($1,'paid',$2,'debt_repayment',$3,$4,$5)",
                        uid, -debt_repay, f"debt_repay:{order['id']}",
                        json.dumps({"order_id": str(order["id"])}), now)
                except asyncpg.UniqueViolationError:
                    pass
            # Credit remaining as paid_points
            if remaining > 0:
                await db.execute(
                    "UPDATE wallets SET paid_points = paid_points + $2, updated_at=NOW() "
                    "WHERE user_id=$1", uid, remaining)
                try:
                    await db.execute(
                        "INSERT INTO wallet_grants "
                        "(user_id, source_type, source_id, wallet_type, initial_points, "
                        "remaining_points, status, created_at, metadata) "
                        "VALUES ($1,$2,$3,'paid',$4,$5,'active',$6,$7)",
                        uid, f"{order['provider']}_purchase", str(order["id"]),
                        remaining, remaining, now,
                        json.dumps({"order_id": str(order["id"]), "package": order.get("package_code", "")}))
                except asyncpg.UniqueViolationError:
                    pass
                try:
                    row2 = await db.fetchrow(
                        "SELECT paid_points, bonus_points, reserved_points FROM wallets WHERE user_id=$1", uid)
                    await db.execute(
                        "INSERT INTO wallet_ledger "
                        "(user_id, wallet_type, amount, transaction_type, idempotency_key, "
                        "paid_balance_after, bonus_balance_after, reserved_balance_after, metadata, created_at) "
                        "VALUES ($1,'paid',$2,'purchase_credit',$3,$4,$5,$6,$7,$8)",
                        uid, remaining, f"purchase:{order['id']}",
                        row2["paid_points"], row2["bonus_points"], row2["reserved_points"],
                        json.dumps({"order_id": str(order["id"]), "debt_repaid": debt_repay}), now)
                except asyncpg.UniqueViolationError:
                    pass
            # Credit promo points as bonus
            promo = order.get("promo_points", 0) or 0
            if promo > 0:
                await db.execute(
                    "UPDATE wallets SET bonus_points = bonus_points + $2, updated_at=NOW() "
                    "WHERE user_id=$1", uid, promo)
                try:
                    await db.execute(
                        "INSERT INTO wallet_grants "
                        "(user_id, source_type, source_id, wallet_type, initial_points, "
                        "remaining_points, status, created_at, metadata) "
                        "VALUES ($1,$2,$3,'bonus',$4,$5,'active',$6,$7)",
                        uid, "promo_credit", str(order["id"]),
                        promo, promo, now,
                        json.dumps({"order_id": str(order["id"])}))
                except asyncpg.UniqueViolationError:
                    pass
                try:
                    row3 = await db.fetchrow(
                        "SELECT paid_points, bonus_points, reserved_points FROM wallets WHERE user_id=$1", uid)
                    await db.execute(
                        "INSERT INTO wallet_ledger "
                        "(user_id, wallet_type, amount, transaction_type, idempotency_key, "
                        "paid_balance_after, bonus_balance_after, reserved_balance_after, metadata, created_at) "
                        "VALUES ($1,'bonus',$2,'promo_credit',$3,$4,$5,$6,$7,$8)",
                        uid, promo, f"promo:{order['id']}",
                        row3["paid_points"], row3["bonus_points"], row3["reserved_points"],
                        json.dumps({"order_id": str(order["id"])}), now)
                except asyncpg.UniqueViolationError:
                    pass
        return await WalletService.get_available_balance(db, uid)


# ── Referral Service ──────────────────────────────────────────────────────────

class ReferralService:
    """Handles referral codes, relations, reward calculation, and activation."""

    @staticmethod
    async def generate_code(user_id: int) -> str:
        """Generate a unique URL-safe referral code for a user."""
        import string
        chars = string.ascii_uppercase + string.digits
        for _ in range(20):
            code = ''.join(secrets.choice(chars) for _ in range(8))
            async with pool.acquire() as db:
                exists = await db.fetchval(
                    "SELECT 1 FROM referral_codes WHERE code=$1", code
                )
                if not exists:
                    await db.execute(
                        "INSERT INTO referral_codes (user_id, code) VALUES ($1,$2) "
                        "ON CONFLICT (user_id) DO UPDATE SET code=EXCLUDED.code",
                        user_id, code
                    )
                    return code
        raise RuntimeError("Failed to generate unique referral code")

    @staticmethod
    async def get_or_create_code(user_id: int) -> str:
        """Get existing code or create new one."""
        async with pool.acquire() as db:
            row = await db.fetchrow(
                "SELECT code FROM referral_codes WHERE user_id=$1", user_id
            )
            if row:
                return row["code"]
        return await ReferralService.generate_code(user_id)

    @staticmethod
    async def get_referrer(db, referred_user_id: int) -> int | None:
        """Get referrer for a user, or None."""
        return await db.fetchval(
            "SELECT referrer_user_id FROM referral_relations WHERE referred_user_id=$1",
            referred_user_id
        )

    @staticmethod
    async def bind_referrer(db, referred_user_id: int, referrer_user_id: int,
                             source_type: str = "referral_link",
                             source_id: str = None) -> bool:
        """Bind a referrer to a user. Returns True if bound, False if already has referrer or self-referral."""
        if referred_user_id == referrer_user_id:
            log.warning("self_referral_attempt: user=%s", referred_user_id)
            await track(referred_user_id, "referral_self_attempt",
                       props={"referrer_id": referrer_user_id})
            return False

        # Check if already has referrer
        existing = await ReferralService.get_referrer(db, referred_user_id)
        if existing:
            return False  # Already bound — don't change

        try:
            await db.execute(
                "INSERT INTO referral_relations "
                "(referrer_user_id, referred_user_id, source_type, source_id) "
                "VALUES ($1,$2,$3,$4)",
                referrer_user_id, referred_user_id, source_type, source_id
            )
            await track(referred_user_id, "referral_bound",
                       props={"referrer_id": referrer_user_id, "source_type": source_type,
                              "source_id": source_id})
            # Notify referrer
            try:
                await bot.send_message(
                    referrer_user_id,
                    "👋 Ваш друг присоединился к ПОЛЯНЕ\n\n"
                    "Когда он пополнит баланс, вы получите 10%\n"
                    "бонусными баллами.")
            except Exception:
                pass
            return True
        except asyncpg.UniqueViolationError:
            return False  # Race condition — already bound

    @staticmethod
    async def bind_referrer_from_recipe_share(db, recipient_user_id: int,
                                               owner_user_id: int,
                                               share_id: str) -> bool:
        """Bind referrer via recipe share. Only for truly new users."""
        return await ReferralService.bind_referrer(
            db, recipient_user_id, owner_user_id,
            source_type="recipe_share", source_id=share_id
        )

    @staticmethod
    async def is_active_user(db, user_id: int) -> bool:
        """Check if user is 'active' (has recipes, AI usage, events, or payments)."""
        has_recipe = await db.fetchval(
            "SELECT 1 FROM recipes WHERE user_id=$1 LIMIT 1", user_id
        )
        if has_recipe:
            return True
        has_event = await db.fetchval(
            "SELECT 1 FROM events WHERE telegram_user_id=$1 LIMIT 1", user_id
        )
        if has_event:
            return True
        has_payment = await db.fetchval(
            "SELECT 1 FROM payment_txns WHERE telegram_user_id=$1 AND kind LIKE 'topup%' LIMIT 1",
            user_id
        )
        if has_payment:
            return True
        has_ai = await db.fetchval(
            "SELECT 1 FROM wallet_ledger WHERE user_id=$1 AND transaction_type='ai_usage' LIMIT 1",
            user_id
        )
        return bool(has_ai)

    @staticmethod
    async def process_successful_payment(db, payment_id: str, user_id: int,
                                          cash_amount_minor: int,
                                          metadata: dict = None) -> int | None:
        """Process a successful payment for referral rewards. Returns reward_points or None."""
        if not REFERRAL_ENABLED:
            return None

        # Check minimum payment
        if cash_amount_minor < REFERRAL_MIN_PAYMENT_AMOUNT_MINOR:
            return None

        # Find referrer
        referrer_id = await ReferralService.get_referrer(db, user_id)
        if not referrer_id or referrer_id == user_id:
            return None

        # Check idempotency — one reward per payment
        existing = await db.fetchval(
            "SELECT id FROM referral_rewards WHERE payment_id=$1", payment_id
        )
        if existing:
            return None  # Already processed

        # Calculate reward per T.Z. formula:
        # reward_points = floor(cash_amount_minor × percent_bp / 10_000 / 100)
        # 100 converts kopecks to rubles for the final points calculation
        reward_points = (cash_amount_minor * REFERRAL_REWARD_PERCENT_BP) // 10_000 // 100
        if reward_points <= 0:
            return None

        # Apply max per payment limit
        if REFERRAL_MAX_REWARD_PER_PAYMENT_POINTS:
            max_per = int(REFERRAL_MAX_REWARD_PER_PAYMENT_POINTS)
            reward_points = min(reward_points, max_per)

        # Apply max per month limit
        if REFERRAL_MAX_REWARD_PER_MONTH_POINTS:
            max_month = int(REFERRAL_MAX_REWARD_PER_MONTH_POINTS)
            month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            month_total = await db.fetchval(
                "SELECT COALESCE(SUM(reward_points),0) FROM referral_rewards "
                "WHERE referrer_user_id=$1 AND created_at >= $2 AND status != 'cancelled'",
                referrer_id, month_start
            ) or 0
            remaining = max_month - month_total
            if remaining <= 0:
                return None
            reward_points = min(reward_points, remaining)

        # Create reward record
        available_at = datetime.now(timezone.utc) + timedelta(days=REFERRAL_HOLD_DAYS)
        try:
            await db.execute(
                "INSERT INTO referral_rewards "
                "(referrer_user_id, referred_user_id, payment_id, cash_amount_minor, "
                "reward_percent_bp, reward_points, status, available_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,'pending',$7)",
                referrer_id, user_id, payment_id, cash_amount_minor,
                REFERRAL_REWARD_PERCENT_BP, reward_points, available_at
            )
        except asyncpg.UniqueViolationError:
            return None  # Idempotent

        # Credit pending points
        await WalletService.credit_pending_bonus(
            db, referrer_id, reward_points,
            reference_type="referral_reward", reference_id=payment_id,
            idempotency_key=f"ref_pending:{payment_id}",
            metadata={"referred_user_id": user_id, "cash_amount_minor": cash_amount_minor}
        )

        # Update activated_at if first payment
        await db.execute(
            "UPDATE referral_relations SET first_payment_at = COALESCE(first_payment_at, NOW()) "
            "WHERE referred_user_id=$1 AND first_payment_at IS NULL",
            user_id
        )

        # Analytics
        await track(referrer_id, "referral_reward_created",
                   props={"referred_user_id": user_id, "payment_id": payment_id,
                          "reward_points": reward_points, "cash_amount_minor": cash_amount_minor})
        await track(referrer_id, "referral_reward_pending",
                   props={"reward_points": reward_points, "available_at": available_at.isoformat()})

        # Notify referrer
        try:
            await bot.send_message(
                referrer_id,
                f"🎁 Вам начислено {reward_points} бонусных баллов\n\n"
                "Друг пополнил баланс ПОЛЯНЫ.\n"
                f"Баллы станут доступны через {REFERRAL_HOLD_DAYS} дн.")
        except Exception:
            pass

        return reward_points

    @staticmethod
    async def process_refund(db, payment_id: str) -> int | None:
        """Process a refund for a referral reward. Returns reversed points or None."""
        reward = await db.fetchrow(
            "SELECT * FROM referral_rewards WHERE payment_id=$1", payment_id
        )
        if not reward:
            return None

        # Idempotent — already reversed/cancelled
        if reward["status"] in ("cancelled", "reversed", "partially_reversed"):
            return None

        points = reward["reward_points"]
        referrer_id = reward["referrer_user_id"]

        if reward["status"] == "pending":
            # Cancel before activation
            await db.execute(
                "UPDATE referral_rewards SET status='cancelled', cancelled_at=NOW(), updated_at=NOW() "
                "WHERE id=$1",
                reward["id"]
            )
            # Debit pending
            await db.execute(
                "UPDATE wallets SET pending_bonus_points = GREATEST(0, pending_bonus_points - $2), "
                "updated_at = NOW() WHERE user_id=$1",
                referrer_id, points
            )
            try:
                await db.execute(
                    "INSERT INTO wallet_ledger "
                    "(user_id, wallet_type, amount, transaction_type, reference_type, reference_id, metadata) "
                    "VALUES ($1, 'pending_bonus', $2, 'referral_cancelled', 'referral_reward', $3, $4)",
                    referrer_id, -points, payment_id,
                    json.dumps({"reason": "refund_before_activation"})
                )
            except asyncpg.UniqueViolationError:
                pass
            await track(referrer_id, "referral_reward_cancelled",
                       props={"payment_id": payment_id, "reward_points": points})
            return points
        else:
            # After activation — reverse
            await db.execute(
                "UPDATE referral_rewards SET status='reversed', reversed_at=NOW(), updated_at=NOW() "
                "WHERE id=$1",
                reward["id"]
            )

            # Check if bonus was spent
            bonus_balance = await db.fetchval(
                "SELECT bonus_points FROM wallets WHERE user_id=$1", referrer_id
            ) or 0

            if bonus_balance >= points:
                # Enough bonus — just reverse
                await db.execute(
                    "UPDATE wallets SET bonus_points = bonus_points - $2, updated_at=NOW() "
                    "WHERE user_id=$1",
                    referrer_id, points
                )
                try:
                    await db.execute(
                        "INSERT INTO wallet_ledger "
                        "(user_id, wallet_type, amount, transaction_type, reference_type, reference_id, metadata) "
                        "VALUES ($1, 'bonus', $2, 'referral_reversed', 'referral_reward', $3, $4)",
                        referrer_id, -points, payment_id,
                        json.dumps({"reason": "refund_after_activation"})
                    )
                except asyncpg.UniqueViolationError:
                    pass
            else:
                # Not enough bonus — create debt
                spent = points - bonus_balance
                await db.execute(
                    "UPDATE wallets SET "
                    "bonus_points = 0, "
                    "bonus_debt_points = bonus_debt_points + $2, "
                    "updated_at = NOW() "
                    "WHERE user_id=$1",
                    referrer_id, spent
                )
                # Partial reversal in ledger
                if bonus_balance > 0:
                    try:
                        await db.execute(
                            "INSERT INTO wallet_ledger "
                            "(user_id, wallet_type, amount, transaction_type, reference_type, reference_id, metadata) "
                            "VALUES ($1, 'bonus', $2, 'referral_reversed', 'referral_reward', $3, $4)",
                            referrer_id, -bonus_balance, payment_id,
                            json.dumps({"reason": "refund_partial_reversal"})
                        )
                    except asyncpg.UniqueViolationError:
                        pass
                try:
                    await db.execute(
                        "INSERT INTO wallet_ledger "
                        "(user_id, wallet_type, amount, transaction_type, reference_type, reference_id, metadata) "
                        "VALUES ($1, 'bonus_debt', $2, 'referral_debt_repayment', 'referral_reward', $3, $4)",
                        referrer_id, spent, payment_id,
                        json.dumps({"reason": "refund_bonus_debt"})
                    )
                except asyncpg.UniqueViolationError:
                    pass

            await track(referrer_id, "referral_reward_reversed",
                       props={"payment_id": payment_id, "reward_points": points,
                              "had_debt": bonus_balance < points})
            # Notify referrer
            try:
                await bot.send_message(
                    referrer_id,
                    "Вознаграждение по платежу было отменено\n"
                    "из-за возврата оплаты.")
            except Exception:
                pass
            return points

    @staticmethod
    async def get_dashboard(db, user_id: int) -> dict:
        """Get referral dashboard data."""
        code = await ReferralService.get_or_create_code(user_id)
        username = await _get_bot_username()
        link = f"https://t.me/{username}?start=ref_{code}" if username else ""

        stats = await db.fetchrow(
            "SELECT "
            "  COUNT(*) as invited, "
            "  COUNT(*) FILTER (WHERE activated_at IS NOT NULL) as activated, "
            "  COUNT(*) FILTER (WHERE first_payment_at IS NOT NULL) as paying "
            "FROM referral_relations WHERE referrer_user_id=$1",
            user_id
        )

        totals = await db.fetchrow(
            "SELECT "
            "  COALESCE(SUM(reward_points),0) as total_reward_points "
            "FROM referral_rewards WHERE referrer_user_id=$1 AND status != 'cancelled'",
            user_id
        )

        wallet = await WalletService.get_balance(db, user_id)

        return {
            "referral_code": code,
            "referral_url": link,
            "balance": wallet,
            "stats": {
                "invited": stats["invited"] or 0,
                "activated": stats["activated"] or 0,
                "paying": stats["paying"] or 0,
                "total_reward_points": totals["total_reward_points"] or 0,
            },
        }

    @staticmethod
    async def get_history(db, user_id: int, limit: int = 50, offset: int = 0) -> list:
        """Get referral reward history."""
        rows = await db.fetch(
            "SELECT rr.*, "
            "  COALESCE(u.first_name, 'Пользователь ПОЛЯНЫ') as ref_first_name, "
            "  u.username as ref_username "
            "FROM referral_rewards rr "
            "LEFT JOIN users u ON u.telegram_user_id = rr.referred_user_id "
            "WHERE rr.referrer_user_id=$1 "
            "ORDER BY rr.created_at DESC LIMIT $2 OFFSET $3",
            user_id, limit, offset
        )
        return [
            {
                "id": str(r["id"]),
                "reward_points": r["reward_points"],
                "status": r["status"],
                "referred_name": _anonymize_name(r["ref_first_name"], r["ref_username"]),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "available_at": r["available_at"].isoformat() if r["available_at"] else None,
            }
            for r in rows
        ]


def _anonymize_name(first_name: str | None, username: str | None) -> str:
    """Show 'Name K.' or @username, never full name."""
    if username:
        return f"@{username}"
    if first_name and len(first_name) > 1:
        return f"{first_name[0]}{first_name[1:]}"
    return "Пользователь ПОЛЯНЫ"


# ── User Service ──────────────────────────────────────────────────────────────

class UserService:
    """Manages user records — creation, onboarding state, first-value tracking."""

    @staticmethod
    async def get_or_create_user(db, telegram_user, acquisition_source="organic",
                                  source_token=None, referrer_user_id=None) -> dict:
        """Get existing user or create new one. Returns user dict."""
        uid = telegram_user.id
        row = await db.fetchrow("SELECT * FROM users WHERE telegram_user_id=$1", uid)
        if row:
            # Update profile fields on each /start
            await db.execute(
                "UPDATE users SET first_name=$2, last_name=$3, username=$4, "
                "language_code=$5, updated_at=NOW() WHERE telegram_user_id=$1",
                uid, telegram_user.first_name, telegram_user.last_name,
                telegram_user.username, getattr(telegram_user, 'language_code', None),
            )
            return dict(row)
        # New user
        await db.execute(
            "INSERT INTO users (telegram_user_id, first_name, last_name, username, "
            "language_code, acquisition_source, acquisition_source_token, referrer_user_id, "
            "onboarding_status, onboarding_version) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'not_started',$9)",
            uid, telegram_user.first_name, telegram_user.last_name,
            telegram_user.username, getattr(telegram_user, 'language_code', None),
            acquisition_source, source_token, referrer_user_id, ONBOARDING_VERSION,
        )
        return dict(await db.fetchrow("SELECT * FROM users WHERE telegram_user_id=$1", uid))

    @staticmethod
    async def get_user(db, user_id: int):
        return await db.fetchrow("SELECT * FROM users WHERE telegram_user_id=$1", user_id)

    @staticmethod
    async def update_onboarding_step(db, user_id: int, step: int, status: str = None):
        if status:
            await db.execute(
                "UPDATE users SET onboarding_step=$2, onboarding_status=$3, updated_at=NOW() "
                "WHERE telegram_user_id=$1", user_id, step, status)
        else:
            await db.execute(
                "UPDATE users SET onboarding_step=$2, updated_at=NOW() "
                "WHERE telegram_user_id=$1", user_id, step)

    @staticmethod
    async def complete_onboarding(db, user_id: int):
        await db.execute(
            "UPDATE users SET onboarding_status='completed', "
            "onboarding_completed_at=NOW(), updated_at=NOW() "
            "WHERE telegram_user_id=$1", user_id)

    @staticmethod
    async def set_first_value_action(db, user_id: int, action: str):
        await db.execute(
            "UPDATE users SET first_value_action=$2, first_value_at=NOW(), updated_at=NOW() "
            "WHERE telegram_user_id=$1 AND first_value_action IS NULL",
            user_id, action)

    @staticmethod
    async def delete_user(db, user_id: int):
        """Anonymize user data. Financial records preserved in ledger."""
        await db.execute(
            "UPDATE users SET first_name='Удалён', last_name=NULL, username=NULL, "
            "language_code=NULL, updated_at=NOW() WHERE telegram_user_id=$1", user_id)
        # Anonymize recipes (keep them but remove owner association for non-financial)
        # Delete personal data: events owned, shopping items, collaborators
        await db.execute("DELETE FROM shopping_items WHERE event_id IN "
                         "(SELECT id FROM events WHERE telegram_user_id=$1)", user_id)
        await db.execute("DELETE FROM event_recipes WHERE event_id IN "
                         "(SELECT id FROM events WHERE telegram_user_id=$1)", user_id)
        await db.execute("DELETE FROM collaborators WHERE telegram_user_id=$1", user_id)
        await db.execute("DELETE FROM events WHERE telegram_user_id=$1", user_id)
        await db.execute("DELETE FROM recipes WHERE user_id=$1", user_id)
        # Revoke all legal acceptances
        await db.execute(
            "UPDATE user_legal_acceptances SET revoked_at=NOW(), action='revoked' "
            "WHERE user_id=$1 AND action='accepted'", user_id)


# ── Legal Consent Service ────────────────────────────────────────────────────

# In-memory cache for consent checks: {(user_id, doc_type): (has_consent, timestamp)}
_consent_cache: dict[tuple[int, str], tuple[bool, float]] = {}
_CONSENT_CACHE_TTL = 300  # 5 minutes


class LegalConsentService:
    """Manages legal document acceptances and consent gates."""

    REQUIRED_TYPES = ("terms", "personal_data_consent")
    AI_TYPE = "ai_processing_consent"
    ALL_TYPES = ("terms", "privacy_policy", "personal_data_consent",
                 "ai_processing_consent", "referral_terms")

    @staticmethod
    async def get_active_document(db, document_type: str):
        """Get the currently active document of a given type."""
        return await db.fetchrow(
            "SELECT * FROM legal_documents WHERE document_type=$1 AND is_active=TRUE "
            "ORDER BY published_at DESC LIMIT 1", document_type)

    @staticmethod
    async def get_user_acceptance_status(db, user_id: int) -> dict:
        """Return {doc_type: {accepted, revoked, version, accepted_at}} for all types."""
        rows = await db.fetch(
            "SELECT la.document_type, la.document_version, la.action, la.accepted_at, "
            "la.revoked_at, ld.content_hash "
            "FROM user_legal_acceptances la "
            "JOIN legal_documents ld ON ld.id = la.document_id "
            "WHERE la.user_id=$1 ORDER BY la.accepted_at DESC", user_id)
        result = {}
        for r in rows:
            dt = r["document_type"]
            if dt not in result:
                result[dt] = {
                    "accepted": False, "revoked": False,
                    "version": None, "accepted_at": None, "content_hash": None,
                }
            if r["action"] == "accepted" and not result[dt]["accepted"]:
                result[dt]["accepted"] = True
                result[dt]["version"] = r["document_version"]
                result[dt]["accepted_at"] = r["accepted_at"]
                result[dt]["content_hash"] = r["content_hash"]
            if r["action"] == "revoked":
                result[dt]["revoked"] = True
                result[dt]["accepted"] = False
        return result

    @staticmethod
    async def has_current_acceptance(db, user_id: int, document_type: str) -> bool:
        """Check if user has an active (accepted, not revoked) acceptance for a document type."""
        cache_key = (user_id, document_type)
        cached = _consent_cache.get(cache_key)
        if cached and (time.time() - cached[1]) < _CONSENT_CACHE_TTL:
            return cached[0]

        row = await db.fetchrow(
            "SELECT la.id FROM user_legal_acceptances la "
            "JOIN legal_documents ld ON ld.id = la.document_id "
            "WHERE la.user_id=$1 AND la.document_type=$2 AND la.action='accepted' "
            "AND la.revoked_at IS NULL "
            "AND ld.is_active=TRUE "
            "ORDER BY la.accepted_at DESC LIMIT 1", user_id, document_type)
        has_it = row is not None
        _consent_cache[cache_key] = (has_it, time.time())
        return has_it

    @staticmethod
    async def accept_document(db, user_id: int, document_type: str, version: str,
                               context: dict = None) -> dict:
        """Accept a specific document version. Idempotent. Returns acceptance record."""
        import hashlib
        doc = await db.fetchrow(
            "SELECT * FROM legal_documents WHERE document_type=$1 AND version=$2 AND is_active=TRUE",
            document_type, version)
        if not doc:
            raise ValueError(f"Document {document_type} v{version} not found or not active")

        # Check for existing acceptance of this document
        existing = await db.fetchrow(
            "SELECT id FROM user_legal_acceptances "
            "WHERE user_id=$1 AND document_id=$2 AND action='accepted' AND revoked_at IS NULL",
            user_id, doc["id"])
        if existing:
            return {"id": str(existing["id"]), "already_accepted": True}

        now = datetime.now(timezone.utc)
        acceptance_id = str(uuid.uuid4())
        source = (context or {}).get("source", "telegram_bot")
        msg_id = (context or {}).get("message_id")
        chat_id = (context or {}).get("chat_id")
        session_id = (context or {}).get("session_id")

        await db.execute(
            "INSERT INTO user_legal_acceptances "
            "(id, user_id, document_id, document_type, document_version, content_hash, "
            "action, source, telegram_message_id, telegram_chat_id, mini_app_session_id, accepted_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,'accepted',$7,$8,$9,$10,$11)",
            uuid.UUID(acceptance_id), user_id, doc["id"], document_type, version,
            doc["content_hash"], source, msg_id, chat_id, session_id, now)

        # Invalidate cache
        _consent_cache.pop((user_id, document_type), None)

        # Track analytics
        event_name = {
            "terms": "terms_accepted",
            "personal_data_consent": "personal_data_consent_accepted",
            "ai_processing_consent": "ai_consent_accepted",
        }.get(document_type, "legal_document_accepted")
        asyncio.create_task(track(user_id, event_name, {
            "document_type": document_type, "document_version": version,
        }))

        return {"id": acceptance_id, "already_accepted": False}

    @staticmethod
    async def revoke_document(db, user_id: int, document_type: str,
                               context: dict = None) -> dict:
        """Revoke acceptance for a document type."""
        now = datetime.now(timezone.utc)
        result = await db.execute(
            "UPDATE user_legal_acceptances SET revoked_at=$2, action='revoked' "
            "WHERE user_id=$1 AND document_type=$3 AND action='accepted' AND revoked_at IS NULL",
            user_id, now, document_type)
        _consent_cache.pop((user_id, document_type), None)

        asyncio.create_task(track(user_id, "consent_revoked", {
            "document_type": document_type,
        }))
        return {"revoked": True}

    @staticmethod
    async def require_basic_access(db, user_id: int) -> bool:
        """Check basic consent. Raises ValueError if not accepted."""
        for dt in LegalConsentService.REQUIRED_TYPES:
            if not await LegalConsentService.has_current_acceptance(db, user_id, dt):
                raise ValueError(f"consent_required:{dt}")
        return True

    @staticmethod
    async def require_ai_access(db, user_id: int) -> bool:
        """Check AI consent. Raises ValueError if not accepted."""
        if not await LegalConsentService.has_current_acceptance(db, user_id, LegalConsentService.AI_TYPE):
            raise ValueError(f"consent_required:{LegalConsentService.AI_TYPE}")
        return True


# ── AI Operation Catalog ─────────────────────────────────────────────────────

AI_OPERATION_CATALOG = {
    "recipe_text_parse": {
        "title": "Распознавание текста рецепта",
        "points": 5,
        "model": "google/gemini-2.5-flash",
        "fallback_model": None,
        "enabled": True,
    },
    "recipe_url_parse": {
        "title": "Разбор рецепта по ссылке",
        "points": 5,
        "model": "google/gemini-2.5-flash",
        "fallback_model": None,
        "enabled": True,
    },
    "recipe_image_parse": {
        "title": "Распознавание рецепта по фото",
        "points": 10,
        "model": "google/gemini-2.5-flash",
        "fallback_model": "qwen/qwen2.5-vl-72b-instruct",
        "enabled": True,
    },
    "recipe_voice_parse": {
        "title": "Разбор голосового сообщения",
        "points": 10,
        "model": "openai/whisper-large-v3",
        "fallback_model": None,
        "enabled": True,
    },
    "recipe_image_generate": {
        "title": "Генерация изображения блюда",
        "points": 20,
        "model": "openai/gpt-5.4-image-2",
        "fallback_model": None,
        "enabled": FEATURE_AI_IMAGE_GENERATION,
    },
    "recipe_normalize": {
        "title": "Нормализация ингредиентов",
        "points": 3,
        "model": "google/gemini-2.5-flash",
        "fallback_model": None,
        "enabled": True,
    },
}


# ── Payment Service ──────────────────────────────────────────────────────────

class PaymentService:
    """Unified payment service for Stars and YooKassa."""

    @staticmethod
    async def get_available_packages(db, provider: str = None) -> list:
        """Get active packages for a provider."""
        now = datetime.now(timezone.utc)
        if provider == "telegram_stars":
            rows = await db.fetch(
                "SELECT * FROM payment_packages WHERE active_for_stars=TRUE "
                "AND (starts_at IS NULL OR starts_at <= $1) "
                "AND (ends_at IS NULL OR ends_at > $1) "
                "ORDER BY sort_order", now)
        elif provider == "yookassa":
            rows = await db.fetch(
                "SELECT * FROM payment_packages WHERE active_for_yookassa=TRUE "
                "AND (starts_at IS NULL OR starts_at <= $1) "
                "AND (ends_at IS NULL OR ends_at > $1) "
                "ORDER BY sort_order", now)
        else:
            rows = await db.fetch(
                "SELECT * FROM payment_packages "
                "WHERE (starts_at IS NULL OR starts_at <= $1) "
                "AND (ends_at IS NULL OR ends_at > $1) "
                "ORDER BY sort_order", now)
        return [dict(r) for r in rows]

    @staticmethod
    async def create_order(db, user_id: int, package_code: str, provider: str) -> dict:
        """Create a payment order. Returns order dict."""
        pkg = await db.fetchrow(
            "SELECT * FROM payment_packages WHERE code=$1", package_code)
        if not pkg:
            raise ValueError("Пакет не найден")
        if provider == "telegram_stars" and not pkg["active_for_stars"]:
            raise ValueError("Пакет недоступен для Telegram Stars")
        if provider == "yookassa" and not pkg["active_for_yookassa"]:
            raise ValueError("Пакет недоступен для ЮKassa")
        if provider == "telegram_stars":
            currency = "XTR"
            amount = pkg["stars_amount"]
        elif provider == "yookassa":
            currency = "RUB"
            amount = pkg["rub_amount_minor"]
        else:
            raise ValueError("Неизвестный провайдер")
        now = datetime.now(timezone.utc)
        order_id = uuid.uuid4()
        payload = f"po:{secrets.token_urlsafe(16)}"
        idempotency_key = f"{provider}:{order_id}"
        referral_base = pkg["base_points"]
        try:
            await db.execute(
                "INSERT INTO payment_orders "
                "(id, user_id, package_id, provider, status, currency, amount, "
                "base_points, promo_points, total_points, referral_base_points, "
                "invoice_payload, idempotency_key, created_at, expires_at) "
                "VALUES ($1,$2,$3,$4,'created',$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
                order_id, user_id, pkg["id"], provider, currency, amount,
                pkg["base_points"], pkg["promo_points"],
                pkg["base_points"] + pkg["promo_points"],
                referral_base, payload, idempotency_key, now,
                now + timedelta(hours=1))
        except asyncpg.UniqueViolationError:
            raise ValueError("Заказ уже создан")
        return {
            "order_id": str(order_id),
            "package_code": pkg["code"],
            "title": pkg["title"],
            "base_points": pkg["base_points"],
            "promo_points": pkg["promo_points"],
            "total_points": pkg["base_points"] + pkg["promo_points"],
            "currency": currency,
            "amount": amount,
            "invoice_payload": payload,
            "provider": provider,
        }

    @staticmethod
    async def find_order_by_payload(db, payload: str):
        """Find order by invoice payload."""
        return await db.fetchrow(
            "SELECT * FROM payment_orders WHERE invoice_payload=$1", payload)

    @staticmethod
    async def find_order_by_id(db, order_id):
        """Find order by ID."""
        return await db.fetchrow(
            "SELECT * FROM payment_orders WHERE id=$1", uuid.UUID(order_id) if isinstance(order_id, str) else order_id)

    @staticmethod
    async def mark_order_paid(db, order_id, external_payment_id: str) -> bool:
        """Mark order as paid. Returns True if this is the first time."""
        now = datetime.now(timezone.utc)
        result = await db.execute(
            "UPDATE payment_orders SET status='succeeded', "
            "external_payment_id=$2, paid_at=$3 "
            "WHERE id=$1 AND status NOT IN ('succeeded','refunded','cancelled')",
            order_id, external_payment_id, now)
        return result.endswith("1")


# ── AI Usage Billing Service ─────────────────────────────────────────────────

class AIUsageBillingService:
    """Manages AI operation pricing, reservation, and usage logging."""

    @staticmethod
    def get_operation_price(operation_type: str) -> int | None:
        """Get price in points for an operation. Returns None if disabled."""
        op = AI_OPERATION_CATALOG.get(operation_type)
        if not op or not op.get("enabled"):
            return None
        return op["points"]

    @staticmethod
    async def check_balance(db, user_id: int, operation_type: str) -> tuple[bool, int, int]:
        """Check if user has enough balance. Returns (ok, needed, available)."""
        price = AIUsageBillingService.get_operation_price(operation_type)
        if price is None:
            return False, 0, 0
        bal = await WalletService.get_available_balance(db, user_id)
        return bal["available"] >= price, price, bal["available"]

    @staticmethod
    async def reserve_points(db, user_id: int, operation_type: str) -> tuple[bool, str | None]:
        """Reserve points for an AI operation. Returns (success, operation_id)."""
        price = AIUsageBillingService.get_operation_price(operation_type)
        if price is None:
            return False, None
        operation_id = uuid.uuid4()
        ok = await WalletService.reserve_for_ai(db, user_id, price, operation_type, operation_id)
        return ok, str(operation_id) if ok else None

    @staticmethod
    async def commit_charge(db, operation_id: str, provider: str, model: str,
                             input_tokens: int = None, output_tokens: int = None,
                             provider_cost_usd: float = None, latency_ms: int = None,
                             status: str = "success", error_code: str = None) -> bool:
        """Commit a reservation after successful AI operation."""
        ok = await WalletService.commit_reservation(db, operation_id)
        if not ok:
            return False
        res = await db.fetchrow(
            "SELECT * FROM wallet_reservations WHERE operation_id=$1", operation_id)
        if not res:
            return False
        try:
            await db.execute(
                "INSERT INTO ai_usage_log "
                "(user_id, operation_type, reservation_id, provider, model, "
                "input_tokens, output_tokens, provider_cost_usd, latency_ms, "
                "status, error_code, charged_points, created_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,NOW())",
                res["user_id"], res["operation_type"], operation_id,
                provider, model, input_tokens, output_tokens,
                provider_cost_usd, latency_ms, status, error_code, res["points"])
        except Exception:
            log.exception("Failed to log AI usage")
        return True

    @staticmethod
    async def release_reservation(db, operation_id: str, reason: str = "error") -> bool:
        """Release reservation on error."""
        return await WalletService.release_reservation(db, operation_id, reason)


# ── Welcome Service ───────────────────────────────────────────────────────────

class WelcomeService:
    """Centralized text building for start screens and onboarding."""

    @staticmethod
    def _features_text(features: list) -> str:
        """Build a feature list from config, excluding disabled flags."""
        lines = []
        for f in features:
            if f["flag"]:
                lines.append(f"{f['icon']} {f['title']}")
        return "\n".join(lines)

    @staticmethod
    def build_new_user_welcome(first_name: str, source: str = "organic",
                                referrer_name: str = None,
                                welcome_points: int = 0) -> str:
        name = _esc(first_name) if first_name else ""
        ref_line = ""
        if source == "referral" and referrer_name:
            ref_line = f"\nВы присоединились по приглашению {_esc(referrer_name)}."

        free_list = WelcomeService._features_text(FREE_FEATURES)
        ai_list = WelcomeService._features_text(AI_FEATURES)

        points_block = ""
        if welcome_points > 0:
            points_block = f"\n🎁 На старте вам доступно: <b>{welcome_points} AI-баллов</b>\n"

        return (
            f"🌿 <b>Добро пожаловать в ПОЛЯНУ, {name}!</b>\n\n"
            f"ПОЛЯНА — это ваша личная библиотека рецептов и помощник\n"
            f"для домашних ужинов, праздников и встреч с друзьями.\n\n"
            f"<b>Просто отправьте в этот чат рецепт:</b>\n"
            f"📷 фотографией или скриншотом\n"
            f"🔗 ссылкой на сайт\n"
            f"📝 обычным текстом\n"
            f"🎙 голосовым сообщением\n"
            f"📩 пересланным сообщением\n\n"
            f"Бот распознает рецепт, аккуратно оформит его\n"
            f"и сохранит в вашей библиотеке.\n\n"
            f"<b>Бесплатно в ПОЛЯНЕ:</b>\n{free_list}\n\n"
            f"<b>AI-баллы используются только для функций с ИИ:</b>\n{ai_list}\n"
            f"{points_block}"
            f"<b>Обязательной подписки нет.</b>\n"
            f"Пополняйте AI-баланс только тогда, когда нужен ИИ.\n"
            f"Баллы также можно получать бесплатно за приглашение друзей.\n\n"
            f"Отправьте первый рецепт прямо в этот чат\n"
            f"или откройте свою ПОЛЯНУ 👇"
            f"{ref_line}"
        )

    @staticmethod
    def build_returning_user_dashboard(first_name: str, recipes_count: int,
                                        events_count: int, total_points: int,
                                        events_enabled: bool = True) -> str:
        name = _esc(first_name) if first_name else ""
        events_line = ""
        if events_enabled:
            events_line = f"Активных событий: <b>{events_count}</b>\n"
        return (
            f"🌿 <b>ПОЛЯНА</b>\n\n"
            f"Рецептов в библиотеке: <b>{recipes_count}</b>\n"
            f"{events_line}"
            f"AI-баланс: <b>{total_points}</b>\n\n"
            f"Отправьте сюда новый рецепт\n"
            f"или откройте библиотеку."
        )

    @staticmethod
    def build_referral_welcome(first_name: str, referrer_name: str,
                                welcome_points: int = 0) -> str:
        name = _esc(first_name) if first_name else ""
        ref = _esc(referrer_name) if referrer_name else "друг"

        free_list = WelcomeService._features_text(FREE_FEATURES)
        ai_list = WelcomeService._features_text(AI_FEATURES)

        points_block = ""
        if welcome_points > 0:
            points_block = f"\n🎁 На старте вам доступно: <b>{welcome_points} AI-баллов</b>\n"

        return (
            f"🌿 <b>{ref} пригласил вас в ПОЛЯНУ</b>\n\n"
            f"Это личная библиотека рецептов прямо в Telegram.\n\n"
            f"<b>Просто отправьте в этот чат рецепт:</b>\n"
            f"📷 фотографией или скриншотом\n"
            f"🔗 ссылкой на сайт\n"
            f"📝 обычным текстом\n"
            f"🎙 голосовым сообщением\n"
            f"📩 пересланным сообщением\n\n"
            f"Бот распознает рецепт, аккуратно оформит его\n"
            f"и сохранит в вашей библиотеке.\n\n"
            f"<b>Бесплатно в ПОЛЯНЕ:</b>\n{free_list}\n\n"
            f"<b>AI-баллы используются только для функций с ИИ:</b>\n{ai_list}\n"
            f"{points_block}"
            f"<b>Обязательной подписки нет.</b>\n"
            f"Пополняйте AI-баланс только тогда, когда нужен ИИ.\n"
            f"Баллы также можно получать бесплатно за приглашение друзей.\n\n"
            f"Вы присоединились по приглашению {ref}.\n"
            f"Отправьте первый рецепт прямо в этот чат\n"
            f"или откройте свою ПОЛЯНУ 👇"
        )

    @staticmethod
    def build_recipe_share_welcome(first_name: str, sender_name: str,
                                    recipe_title: str) -> str:
        sender = _esc(sender_name) if sender_name else "друг"
        title = _esc(recipe_title) if recipe_title else "рецепт"

        free_list = WelcomeService._features_text(FREE_FEATURES)

        return (
            f"🍲 <b>{sender} поделился с вами рецептом</b>\n\n"
            f"<b>{title}</b>\n\n"
            f"Сохраните его в личную библиотеку ПОЛЯНЫ,\n"
            f"чтобы рецепт не потерялся.\n\n"
            f"В ПОЛЯНЕ можно бесплатно:\n{free_list}\n\n"
            f"ИИ помогает распознавать фотографии, голосовые сообщения,\n"
            f"придумывать новые блюда и создавать изображения.\n"
            f"Эти действия оплачиваются AI-баллами.\n\n"
            f"Обязательной подписки нет."
        )

    @staticmethod
    def build_channel_welcome(first_name: str) -> str:
        return (
            f"🌿 <b>Сохраните рецепт из канала в ПОЛЯНУ</b>\n\n"
            f"ПОЛЯНА — личная библиотека рецептов в Telegram.\n\n"
            f"Сохранённый рецепт можно:\n"
            f"— быстро найти\n"
            f"— изменить\n"
            f"— пересчитать на нужное количество порций\n"
            f"— добавить в меню\n"
            f"— превратить в список покупок\n"
            f"— отправить друзьям\n\n"
            f"Основная библиотека и обычные инструменты бесплатны.\n"
            f"AI-баллы нужны только для распознавания и генерации."
        )

    @staticmethod
    def build_how_to_add_recipe() -> str:
        return (
            f"📎 <b>Как сохранить рецепт</b>\n\n"
            f"Просто отправьте его в этот чат.\n\n"
            f"Подойдут:\n"
            f"— фотография страницы из книги\n"
            f"— скриншот из Instagram или другого приложения\n"
            f"— ссылка на сайт\n"
            f"— скопированный текст\n"
            f"— голосовое сообщение\n"
            f"— пересланный рецепт из другого Telegram-канала или чата\n\n"
            f"Выбирать специальный режим не нужно.\n"
            f"ПОЛЯНА сама определит, что вы отправили.\n\n"
            f"Попробуйте прямо сейчас 👇"
        )

    @staticmethod
    def build_example() -> str:
        return (
            f"<b>Например, вы увидели рецепт в социальной сети.</b>\n\n"
            f"Делаете скриншот и отправляете его боту.\n\n"
            f"ПОЛЯНА создаёт аккуратную карточку:\n\n"
            f"🍝 Паста с курицей\n"
            f"⏱ 35 минут\n"
            f"🍽 4 порции\n\n"
            f"Ингредиенты:\n"
            f"— куриное филе — 500 г\n"
            f"— паста — 300 г\n"
            f"— сливки — 200 мл\n\n"
            f"После этого рецепт можно:\n"
            f"— открыть в библиотеке\n"
            f"— изменить\n"
            f"— пересчитать на другое количество порций\n"
            f"— добавить в меню\n"
            f"— отправить другу"
        )

    @staticmethod
    def build_ai_functions(total_points: int) -> str:
        return (
            f"✨ <b>Что делает ИИ в ПОЛЯНЕ</b>\n\n"
            f"AI-баллы расходуются только на функции,\n"
            f"для которых требуется работа искусственного интеллекта.\n\n"
            f"За баллы можно:\n\n"
            f"📷 распознать рецепт по фото или скриншоту\n"
            f"🎙 разобрать голосовое сообщение\n"
            f"🔗 обработать сложную страницу сайта\n"
            f"🍲 придумать рецепт по вашим пожеланиям\n"
            f"🖼 создать изображение блюда\n"
            f"💡 получить рекомендации из вашей библиотеки\n"
            f"🎉 составить меню для события\n"
            f"🧾 распознать чек\n\n"
            f"Обычные функции библиотеки, событий,\n"
            f"списков покупок и расчёта расходов остаются бесплатными.\n\n"
            f"Ваш баланс: <b>{total_points} AI-баллов</b>"
        )

    @staticmethod
    def build_get_points(invited_count: int, bonus_points: int,
                          pending_bonus_points: int) -> str:
        return (
            f"🎁 <b>Пользуйтесь ИИ бесплатно</b>\n\n"
            f"Приглашайте друзей в ПОЛЯНУ и получайте\n"
            f"<b>10% от их пополнений AI-баллами</b>.\n\n"
            f"Баллы можно тратить внутри ПОЛЯНЫ:\n"
            f"на распознавание рецептов, генерацию блюд,\n"
            f"изображения и другие AI-функции.\n\n"
            f"Баллы нельзя вывести или обменять на деньги.\n\n"
            f"Приглашено друзей: <b>{invited_count}</b>\n"
            f"Доступно бонусных баллов: <b>{bonus_points}</b>\n"
            f"Ожидают начисления: <b>{pending_bonus_points}</b>"
        )


pool = None
_db_ready = False
_db_error: str | None = None


async def get_db():
    if pool is None:
        raise HTTPException(503, "Сервис запускается, попробуйте через секунду")
    async with pool.acquire() as c:
        yield c


async def track(user_id, event_type, props=None, event_ref=None, src_payload=None):
    """Fire-and-forget analytics. Own connection, swallows errors — never breaks a request.
    Server-truth for North Star (K-factor), activation and the viral loop."""
    if pool is None or not event_type:
        return
    try:
        async with pool.acquire() as c:
            await c.execute(
                "INSERT INTO analytics_events (user_id, event_type, props, event_ref, src_payload) "
                "VALUES ($1,$2,$3::jsonb,$4,$5)",
                user_id, str(event_type)[:64], json.dumps(props or {}),
                event_ref, (str(src_payload)[:128] if src_payload else None),
            )
    except Exception:
        log.exception("analytics track failed: %s", event_type)


async def init_db():
    global pool, _db_ready
    pool = await asyncio.wait_for(
        asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, command_timeout=30),
        timeout=30,
    )
    async with pool.acquire() as c:

        # ── Create tables (target schema for fresh deploys) ───────────────────
        await c.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id               SERIAL PRIMARY KEY,
                name             TEXT NOT NULL,
                event_date       TIMESTAMPTZ,
                location         TEXT,
                description      TEXT,
                template         TEXT,
                share_token      TEXT UNIQUE,
                guests_count     INT DEFAULT 1,
                telegram_user_id BIGINT NOT NULL,
                created_at       TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS collaborators (
                id               SERIAL PRIMARY KEY,
                event_id         INT REFERENCES events(id) ON DELETE CASCADE,
                telegram_user_id BIGINT NOT NULL,
                first_name       TEXT,
                username         TEXT,
                role             TEXT DEFAULT 'collaborator',
                joined_at        TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(event_id, telegram_user_id)
            );

            -- Personal recipe library (user-owned, not event-bound)
            CREATE TABLE IF NOT EXISTS recipes (
                id                SERIAL PRIMARY KEY,
                user_id           BIGINT NOT NULL DEFAULT 0,
                name              TEXT NOT NULL,
                name_original     TEXT,
                emoji             TEXT DEFAULT '🍽',
                source_url        TEXT,
                source_type       TEXT DEFAULT 'manual',
                original_language TEXT,
                servings          INT DEFAULT 4,
                cook_time_minutes INT,
                category          TEXT,
                tags              TEXT[] DEFAULT '{}',
                times_cooked      INT DEFAULT 0,
                rating            INT,
                notes             TEXT,
                created_at        TIMESTAMPTZ DEFAULT NOW(),
                updated_at        TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS ingredients (
                id          SERIAL PRIMARY KEY,
                recipe_id   INT REFERENCES recipes(id) ON DELETE CASCADE,
                name        TEXT NOT NULL,
                qty         FLOAT,
                unit        TEXT,
                category    TEXT,
                sort_order  INT DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS recipe_steps (
                id          SERIAL PRIMARY KEY,
                recipe_id   INT REFERENCES recipes(id) ON DELETE CASCADE,
                step_number INT NOT NULL,
                text        TEXT NOT NULL
            );

            -- M2M: which recipes appear in which events
            CREATE TABLE IF NOT EXISTS event_recipes (
                id                  SERIAL PRIMARY KEY,
                event_id            INT REFERENCES events(id) ON DELETE CASCADE,
                recipe_id           INT REFERENCES recipes(id) ON DELETE CASCADE,
                servings_multiplier FLOAT DEFAULT 1.0,
                added_by_id         BIGINT NOT NULL DEFAULT 0,
                added_at            TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(event_id, recipe_id)
            );

            CREATE TABLE IF NOT EXISTS shopping_items (
                id       SERIAL PRIMARY KEY,
                event_id INT REFERENCES events(id) ON DELETE CASCADE,
                name     TEXT NOT NULL,
                quantity TEXT,
                bought   BOOLEAN DEFAULT FALSE
            );

            CREATE TABLE IF NOT EXISTS login_tokens (
                id               SERIAL PRIMARY KEY,
                token            TEXT UNIQUE NOT NULL,
                telegram_user_id BIGINT NOT NULL,
                used             BOOLEAN DEFAULT FALSE,
                created_at       TIMESTAMPTZ DEFAULT NOW(),
                expires_at       TIMESTAMPTZ NOT NULL
            );
        """)

        # ── Migration A: Column renames & additions ───────────────────────────
        await c.execute("""
            DO $$
            BEGIN
                -- events: title→name
                IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='title')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='name')
                THEN ALTER TABLE events RENAME COLUMN title TO name; END IF;

                -- events: date→event_date
                IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='date')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='event_date')
                THEN ALTER TABLE events RENAME COLUMN date TO event_date; END IF;

                -- events: add missing columns
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='share_token')
                    THEN ALTER TABLE events ADD COLUMN share_token TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='location')
                    THEN ALTER TABLE events ADD COLUMN location TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='description')
                    THEN ALTER TABLE events ADD COLUMN description TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='template')
                    THEN ALTER TABLE events ADD COLUMN template TEXT; END IF;

                -- collaborators
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='collaborators' AND column_name='first_name')
                    THEN ALTER TABLE collaborators ADD COLUMN first_name TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='collaborators' AND column_name='username')
                    THEN ALTER TABLE collaborators ADD COLUMN username TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='collaborators' AND column_name='role')
                    THEN ALTER TABLE collaborators ADD COLUMN role TEXT DEFAULT 'collaborator'; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='collaborators' AND column_name='joined_at')
                    THEN ALTER TABLE collaborators ADD COLUMN joined_at TIMESTAMPTZ DEFAULT NOW(); END IF;

                -- shopping_items: ingredient_name→name
                IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='shopping_items' AND column_name='ingredient_name')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='shopping_items' AND column_name='name')
                THEN ALTER TABLE shopping_items RENAME COLUMN ingredient_name TO name; END IF;

                -- recipes: title→name (very old schema)
                IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='title')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='name')
                THEN ALTER TABLE recipes RENAME COLUMN title TO name; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='name')
                    THEN ALTER TABLE recipes ADD COLUMN name TEXT NOT NULL DEFAULT ''; END IF;
            END $$;
        """)

        # ── Migration B: Recipes → user_id-based personal library ────────────
        await c.execute("""
            DO $$
            BEGIN
                -- Add user_id if missing (old schema used event_id instead)
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='recipes' AND column_name='user_id')
                THEN ALTER TABLE recipes ADD COLUMN user_id BIGINT; END IF;

                -- Backfill user_id from added_by_user_id
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name='recipes' AND column_name='added_by_user_id') THEN
                    UPDATE recipes SET user_id = added_by_user_id
                    WHERE user_id IS NULL AND added_by_user_id IS NOT NULL;
                END IF;

                -- Backfill user_id from event owner for recipes linked to events
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name='recipes' AND column_name='event_id') THEN
                    UPDATE recipes r SET user_id = e.telegram_user_id
                    FROM events e
                    WHERE r.event_id = e.id AND r.user_id IS NULL;
                END IF;

                -- Default any remaining nulls to -1 (orphan marker — not a real Telegram ID)
                UPDATE recipes SET user_id = -1 WHERE user_id IS NULL;

                -- Set NOT NULL now that every row has a value
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='recipes' AND column_name='user_id' AND is_nullable = 'YES'
                ) THEN
                    ALTER TABLE recipes ALTER COLUMN user_id SET NOT NULL;
                END IF;

                -- Rename cook_time_min → cook_time_minutes
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name='recipes' AND column_name='cook_time_min')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_name='recipes' AND column_name='cook_time_minutes')
                THEN ALTER TABLE recipes RENAME COLUMN cook_time_min TO cook_time_minutes; END IF;

                -- Add new columns (no-op if already present)
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='cook_time_minutes')
                    THEN ALTER TABLE recipes ADD COLUMN cook_time_minutes INT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='name_original')
                    THEN ALTER TABLE recipes ADD COLUMN name_original TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='source_type')
                    THEN ALTER TABLE recipes ADD COLUMN source_type TEXT DEFAULT 'manual'; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='original_language')
                    THEN ALTER TABLE recipes ADD COLUMN original_language TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='category')
                    THEN ALTER TABLE recipes ADD COLUMN category TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='tags')
                    THEN ALTER TABLE recipes ADD COLUMN tags TEXT[] DEFAULT '{}'; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='times_cooked')
                    THEN ALTER TABLE recipes ADD COLUMN times_cooked INT DEFAULT 0; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='rating')
                    THEN ALTER TABLE recipes ADD COLUMN rating INT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='notes')
                    THEN ALTER TABLE recipes ADD COLUMN notes TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='updated_at')
                    THEN ALTER TABLE recipes ADD COLUMN updated_at TIMESTAMPTZ DEFAULT NOW(); END IF;
            END $$;
        """)

        # ── Migration E: Ensure event_recipes has all required columns ──────────
        # Handles the case where event_recipes was created with an older/partial schema
        await c.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='event_recipes' AND column_name='servings_multiplier')
                    THEN ALTER TABLE event_recipes ADD COLUMN servings_multiplier FLOAT DEFAULT 1.0; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='event_recipes' AND column_name='added_by_id')
                    THEN ALTER TABLE event_recipes ADD COLUMN added_by_id BIGINT NOT NULL DEFAULT 0; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='event_recipes' AND column_name='added_at')
                    THEN ALTER TABLE event_recipes ADD COLUMN added_at TIMESTAMPTZ DEFAULT NOW(); END IF;
            END $$;
        """)

        # ── Migration G: Extend shopping_items for aggregated list ──────────────
        await c.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='shopping_items' AND column_name='qty')
                    THEN ALTER TABLE shopping_items ADD COLUMN qty FLOAT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='shopping_items' AND column_name='unit')
                    THEN ALTER TABLE shopping_items ADD COLUMN unit TEXT DEFAULT ''; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='shopping_items' AND column_name='category')
                    THEN ALTER TABLE shopping_items ADD COLUMN category TEXT DEFAULT 'прочее'; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='shopping_items' AND column_name='is_generated')
                    THEN ALTER TABLE shopping_items ADD COLUMN is_generated BOOLEAN DEFAULT FALSE; END IF;
            END $$;
        """)

        # ── Migration F: Ensure ingredients has all required columns ──────────
        await c.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='ingredients' AND column_name='qty')
                    THEN ALTER TABLE ingredients ADD COLUMN qty FLOAT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='ingredients' AND column_name='unit')
                    THEN ALTER TABLE ingredients ADD COLUMN unit TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='ingredients' AND column_name='category')
                    THEN ALTER TABLE ingredients ADD COLUMN category TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='ingredients' AND column_name='sort_order')
                    THEN ALTER TABLE ingredients ADD COLUMN sort_order INT DEFAULT 0; END IF;
            END $$;
        """)

        # ── Migration H: shopping_items — add added_by column ─────────────────
        await c.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='shopping_items' AND column_name='added_by')
                    THEN ALTER TABLE shopping_items ADD COLUMN added_by BIGINT; END IF;
            END $$;
        """)

        # ── Migration I: recipes — store source photo file_id ─────────────────
        await c.execute("""
            ALTER TABLE recipes ADD COLUMN IF NOT EXISTS source_photo_file_id TEXT;
        """)

        # ── Migration J: shopping_items — add `quantity` (legacy table had
        #    total_grams/total_display instead). ALL inserts write `quantity`,
        #    so without this column generation and manual-add both fail. ───────
        await c.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='shopping_items' AND column_name='quantity')
                THEN
                    ALTER TABLE shopping_items ADD COLUMN quantity TEXT;
                    -- Backfill from the legacy display column if it exists
                    IF EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='shopping_items' AND column_name='total_display')
                    THEN
                        UPDATE shopping_items SET quantity = total_display
                        WHERE quantity IS NULL AND total_display IS NOT NULL;
                    END IF;
                END IF;
            END $$;
        """)

        # ── Migration K: payments — balance, ledger, invite grants ────────────
        await c.execute("""
            CREATE TABLE IF NOT EXISTS user_balance (
                telegram_user_id BIGINT PRIMARY KEY,
                balance          INT NOT NULL DEFAULT 0,   -- kopecks
                updated_at       TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS payment_txns (
                id               SERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                kind             TEXT NOT NULL,
                amount           INT NOT NULL,             -- kopecks: +credit / -debit
                balance_after    INT,
                ref              TEXT,                     -- external id (idempotency)
                meta             JSONB,
                created_at       TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_txn_kind_ref
                ON payment_txns(kind, ref) WHERE ref IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_txn_user ON payment_txns(telegram_user_id);
            CREATE TABLE IF NOT EXISTS invite_grants (
                id               SERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                event_id         INT NOT NULL,
                remaining        INT NOT NULL DEFAULT 0,
                created_at       TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_grant_user_event
                ON invite_grants(telegram_user_id, event_id);
        """)

        # ── Migration L: referrals (legacy, kept for compatibility) ──────────────
        await c.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referee_id   BIGINT PRIMARY KEY,
                referrer_id  BIGINT NOT NULL,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_ref_referrer ON referrals(referrer_id);
            CREATE TABLE IF NOT EXISTS referral_bonuses (
                id            SERIAL PRIMARY KEY,
                referrer_id   BIGINT NOT NULL,
                referee_id    BIGINT NOT NULL,
                source_ref    TEXT UNIQUE,        -- idempotency per charge
                amount        INT NOT NULL,       -- kopecks
                available_at  TIMESTAMPTZ NOT NULL,
                paid          BOOLEAN DEFAULT FALSE,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_refbonus_due
                ON referral_bonuses(available_at) WHERE NOT paid;
        """)

        # ── Migration M: referral codes (URL-safe, non-user_id) ────────────────
        await c.execute("""
            CREATE TABLE IF NOT EXISTS referral_codes (
                id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id    BIGINT NOT NULL UNIQUE,
                code       VARCHAR(32) NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        # ── Migration N: referral_relations (expanded from referrals) ──────────
        await c.execute("""
            CREATE TABLE IF NOT EXISTS referral_relations (
                id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                referrer_user_id  BIGINT NOT NULL,
                referred_user_id  BIGINT NOT NULL UNIQUE,
                source_type       VARCHAR(32) NOT NULL DEFAULT 'referral_link',
                source_id         TEXT,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                activated_at      TIMESTAMPTZ,
                first_payment_at  TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS idx_rr_referrer ON referral_relations(referrer_user_id);
            -- Self-referral check: prevent referrer_user_id = referred_user_id
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'no_self_referral') THEN
                    ALTER TABLE referral_relations ADD CONSTRAINT no_self_referral
                        CHECK (referrer_user_id <> referred_user_id);
                END IF;
            END $$;
        """)

        # ── Migration O: referral_rewards (expanded from referral_bonuses) ─────
        await c.execute("""
            CREATE TABLE IF NOT EXISTS referral_rewards (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                referrer_user_id    BIGINT NOT NULL,
                referred_user_id    BIGINT NOT NULL,
                payment_id          UUID NOT NULL UNIQUE,
                cash_amount_minor   BIGINT NOT NULL,
                reward_percent_bp   INTEGER NOT NULL,
                reward_points       BIGINT NOT NULL,
                status              VARCHAR(32) NOT NULL DEFAULT 'pending',
                available_at        TIMESTAMPTZ,
                activated_at        TIMESTAMPTZ,
                cancelled_at        TIMESTAMPTZ,
                reversed_at         TIMESTAMPTZ,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_rr_referrer ON referral_rewards(referrer_user_id);
            CREATE INDEX IF NOT EXISTS idx_rr_referred ON referral_rewards(referred_user_id);
            CREATE INDEX IF NOT EXISTS idx_rr_status_available
                ON referral_rewards(status, available_at) WHERE status = 'pending';
        """)

        # ── Migration P: wallets (paid/bonus/pending/debt) ────────────────────
        await c.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                user_id              BIGINT PRIMARY KEY,
                paid_points          BIGINT NOT NULL DEFAULT 0,
                bonus_points         BIGINT NOT NULL DEFAULT 0,
                pending_bonus_points BIGINT NOT NULL DEFAULT 0,
                bonus_debt_points    BIGINT NOT NULL DEFAULT 0,
                updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        # ── Migration Q: wallet_ledger (audit trail for all point changes) ────
        await c.execute("""
            CREATE TABLE IF NOT EXISTS wallet_ledger (
                id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id         BIGINT NOT NULL,
                wallet_type     VARCHAR(32) NOT NULL,
                amount          BIGINT NOT NULL,
                transaction_type VARCHAR(64) NOT NULL,
                reference_type  VARCHAR(32),
                reference_id    TEXT,
                idempotency_key VARCHAR(255) UNIQUE,
                balance_after   BIGINT,
                metadata        JSONB,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_wl_user ON wallet_ledger(user_id);
            CREATE INDEX IF NOT EXISTS idx_wl_user_type ON wallet_ledger(user_id, wallet_type);
        """)

        # ── Analytics: append-only event log (North Star / K-factor / funnel) ──
        await c.execute("""
            CREATE TABLE IF NOT EXISTS analytics_events (
                id          BIGSERIAL PRIMARY KEY,
                ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                user_id     BIGINT,
                event_type  TEXT NOT NULL,
                props       JSONB NOT NULL DEFAULT '{}',
                event_ref   BIGINT,
                src_payload TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ae_type_ts ON analytics_events(event_type, ts);
            CREATE INDEX IF NOT EXISTS idx_ae_user    ON analytics_events(user_id);
            CREATE INDEX IF NOT EXISTS idx_ae_ref     ON analytics_events(event_ref);
        """)

        # ── Migration C: Seed event_recipes from old recipes.event_id ────────
        await c.execute("""
            DO $$
            BEGIN
                -- Migrate existing event_id links on recipes → event_recipes
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name='recipes' AND column_name='event_id') THEN
                    INSERT INTO event_recipes
                        (event_id, recipe_id, servings_multiplier, added_by_id, added_at)
                    SELECT r.event_id, r.id, 1.0,
                           COALESCE(r.user_id, 0),
                           COALESCE(r.created_at, NOW())
                    FROM recipes r
                    WHERE r.event_id IS NOT NULL
                    ON CONFLICT (event_id, recipe_id) DO NOTHING;
                END IF;

                -- Also migrate from event_menu_items if that legacy table still exists
                IF EXISTS (SELECT 1 FROM information_schema.tables
                           WHERE table_name='event_menu_items') THEN
                    -- Insert unique items into recipe library
                    INSERT INTO recipes
                        (user_id, name, emoji, servings, source_type, created_at)
                    SELECT COALESCE(m.added_by_user_id, e.telegram_user_id, 0),
                           m.name, m.emoji, m.servings, 'manual', m.added_at
                    FROM event_menu_items m
                    JOIN events e ON e.id = m.event_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM recipes r2
                        WHERE r2.user_id = COALESCE(m.added_by_user_id, e.telegram_user_id, 0)
                          AND r2.name = m.name
                          AND r2.created_at = m.added_at
                    );

                    -- Link them to events
                    INSERT INTO event_recipes
                        (event_id, recipe_id, servings_multiplier, added_by_id, added_at)
                    SELECT m.event_id, r.id, 1.0,
                           COALESCE(m.added_by_user_id, e.telegram_user_id, 0),
                           m.added_at
                    FROM event_menu_items m
                    JOIN events e ON e.id = m.event_id
                    JOIN recipes r
                        ON r.name = m.name
                       AND r.user_id = COALESCE(m.added_by_user_id, e.telegram_user_id, 0)
                       AND r.created_at = m.added_at
                    ON CONFLICT (event_id, recipe_id) DO NOTHING;
                END IF;
            END $$;
        """)

        # ── Migration D: Constraints ──────────────────────────────────────────
        await c.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'collaborators_event_user_uniq'
                ) THEN
                    DELETE FROM collaborators a USING collaborators b
                    WHERE a.id > b.id
                      AND a.event_id = b.event_id
                      AND a.telegram_user_id = b.telegram_user_id;
                    ALTER TABLE collaborators
                        ADD CONSTRAINT collaborators_event_user_uniq
                        UNIQUE (event_id, telegram_user_id);
                END IF;
            END $$;
        """)

        # ── Indexes ───────────────────────────────────────────────────────────
        await c.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_user        ON events(telegram_user_id);
            CREATE INDEX IF NOT EXISTS idx_collab_event       ON collaborators(event_id);
            CREATE INDEX IF NOT EXISTS idx_collab_user        ON collaborators(telegram_user_id);
            CREATE INDEX IF NOT EXISTS idx_recipes_user_id    ON recipes(user_id);
            CREATE INDEX IF NOT EXISTS idx_recipes_user_name  ON recipes(user_id, name);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_recipes_user_source_url
                ON recipes(user_id, source_url) WHERE source_url IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_event_recipes_evt  ON event_recipes(event_id);
            CREATE INDEX IF NOT EXISTS idx_event_recipes_rec  ON event_recipes(recipe_id);
            CREATE INDEX IF NOT EXISTS idx_ingredients_rec    ON ingredients(recipe_id);
            CREATE INDEX IF NOT EXISTS idx_shopping_event     ON shopping_items(event_id);

            -- Recipe shares: snapshot sent via inline with save button
            CREATE TABLE IF NOT EXISTS recipe_shares (
                id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                token             TEXT UNIQUE NOT NULL,
                source_recipe_id  INT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                owner_user_id     BIGINT NOT NULL,
                snapshot          JSONB NOT NULL,
                created_at        TIMESTAMPTZ DEFAULT NOW(),
                revoked_at        TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS idx_shares_token ON recipe_shares(token);

            CREATE TABLE IF NOT EXISTS recipe_share_saves (
                share_id          UUID NOT NULL REFERENCES recipe_shares(id),
                recipient_user_id BIGINT NOT NULL,
                created_recipe_id INT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                saved_at          TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(share_id, recipient_user_id)
            );

            -- ── Migration R: Users table ─────────────────────────────────────
            CREATE TABLE IF NOT EXISTS users (
                telegram_user_id    BIGINT PRIMARY KEY,
                first_name          TEXT,
                last_name           TEXT,
                username            TEXT,
                language_code       TEXT,
                onboarding_status   VARCHAR(32) DEFAULT 'not_started',
                onboarding_step     INT DEFAULT 0,
                onboarding_version  VARCHAR(16),
                onboarding_completed_at TIMESTAMPTZ,
                first_value_action  VARCHAR(32),
                first_value_at      TIMESTAMPTZ,
                acquisition_source  VARCHAR(32) DEFAULT 'organic',
                acquisition_source_token TEXT,
                referrer_user_id    BIGINT,
                created_at          TIMESTAMPTZ DEFAULT NOW(),
                updated_at          TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_users_onboarding ON users(onboarding_status);

            -- ── Migration S: Legal documents ─────────────────────────────────
            CREATE TABLE IF NOT EXISTS legal_documents (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                document_type       VARCHAR(64) NOT NULL,
                version             VARCHAR(32) NOT NULL,
                title               TEXT NOT NULL,
                content             TEXT NOT NULL,
                content_hash        VARCHAR(128) NOT NULL,
                changelog           TEXT,
                published_at        TIMESTAMPTZ NOT NULL,
                effective_from      TIMESTAMPTZ NOT NULL,
                is_active           BOOLEAN NOT NULL DEFAULT TRUE,
                requires_acceptance BOOLEAN NOT NULL DEFAULT FALSE,
                created_at          TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(document_type, version)
            );
            CREATE INDEX IF NOT EXISTS idx_legal_docs_type_active ON legal_documents(document_type, is_active);

            -- ── Migration T: User legal acceptances ──────────────────────────
            CREATE TABLE IF NOT EXISTS user_legal_acceptances (
                id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id               BIGINT NOT NULL REFERENCES users(telegram_user_id),
                document_id           UUID NOT NULL REFERENCES legal_documents(id),
                document_type         VARCHAR(64) NOT NULL,
                document_version      VARCHAR(32) NOT NULL,
                content_hash          VARCHAR(128) NOT NULL,
                action                VARCHAR(32) NOT NULL DEFAULT 'accepted',
                source                VARCHAR(32) NOT NULL DEFAULT 'telegram_bot',
                telegram_message_id   BIGINT,
                telegram_chat_id      BIGINT,
                mini_app_session_id   UUID,
                accepted_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                revoked_at            TIMESTAMPTZ,
                metadata              JSONB,
                UNIQUE(user_id, document_id, action)
            );
            CREATE INDEX IF NOT EXISTS idx_acceptances_user ON user_legal_acceptances(user_id);

            -- ── Migration U: Pending onboarding actions ──────────────────────
            CREATE TABLE IF NOT EXISTS pending_onboarding_actions (
                id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id       BIGINT NOT NULL REFERENCES users(telegram_user_id),
                action_type   VARCHAR(32) NOT NULL,
                token_hash    VARCHAR(128),
                payload       JSONB,
                expires_at    TIMESTAMPTZ NOT NULL,
                completed_at  TIMESTAMPTZ,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_pending_actions_user ON pending_onboarding_actions(user_id, completed_at);

            -- ── Migration V: Payment system ────────────────────────────────────

            -- Payment packages catalog
            CREATE TABLE IF NOT EXISTS payment_packages (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                code                VARCHAR(64) NOT NULL UNIQUE,
                title               TEXT NOT NULL,
                description         TEXT,
                base_points         BIGINT NOT NULL,
                promo_points        BIGINT NOT NULL DEFAULT 0,
                stars_amount        BIGINT,
                rub_amount_minor    BIGINT,
                active_for_stars    BOOLEAN NOT NULL DEFAULT FALSE,
                active_for_yookassa BOOLEAN NOT NULL DEFAULT FALSE,
                sort_order          INTEGER NOT NULL DEFAULT 0,
                starts_at           TIMESTAMPTZ,
                ends_at             TIMESTAMPTZ,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            -- Payment orders
            CREATE TABLE IF NOT EXISTS payment_orders (
                id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id                 BIGINT NOT NULL,
                package_id              UUID NOT NULL REFERENCES payment_packages(id),
                provider                VARCHAR(32) NOT NULL,
                status                  VARCHAR(32) NOT NULL DEFAULT 'created',
                currency                VARCHAR(8) NOT NULL,
                amount                  BIGINT NOT NULL,
                base_points             BIGINT NOT NULL,
                promo_points            BIGINT NOT NULL,
                total_points            BIGINT NOT NULL,
                referral_base_points    BIGINT NOT NULL,
                external_payment_id     TEXT,
                invoice_payload         TEXT UNIQUE NOT NULL,
                idempotency_key         VARCHAR(64) NOT NULL UNIQUE,
                created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at              TIMESTAMPTZ,
                paid_at                 TIMESTAMPTZ,
                cancelled_at            TIMESTAMPTZ,
                refunded_at             TIMESTAMPTZ,
                metadata                JSONB
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_po_provider_ext
                ON payment_orders(provider, external_payment_id) WHERE external_payment_id IS NOT NULL;

            -- Wallet grants (lot tracking for FIFO consumption)
            CREATE TABLE IF NOT EXISTS wallet_grants (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id             BIGINT NOT NULL,
                source_type         VARCHAR(32) NOT NULL,
                source_id           TEXT NOT NULL,
                wallet_type         VARCHAR(16) NOT NULL,
                initial_points      BIGINT NOT NULL,
                remaining_points    BIGINT NOT NULL,
                status              VARCHAR(32) NOT NULL DEFAULT 'active',
                expires_at          TIMESTAMPTZ,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                reversed_at         TIMESTAMPTZ,
                metadata            JSONB
            );
            CREATE INDEX IF NOT EXISTS idx_wg_user_status ON wallet_grants(user_id, status, wallet_type);

            -- Wallet reservations (hold points during AI operations)
            CREATE TABLE IF NOT EXISTS wallet_reservations (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id             BIGINT NOT NULL,
                operation_type      VARCHAR(64) NOT NULL,
                operation_id        UUID NOT NULL UNIQUE,
                points              BIGINT NOT NULL,
                status              VARCHAR(32) NOT NULL DEFAULT 'reserved',
                expires_at          TIMESTAMPTZ NOT NULL,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                committed_at        TIMESTAMPTZ,
                released_at         TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS idx_wr_user_status ON wallet_reservations(user_id, status);

            -- Add reserved_points and paid_debt_points to wallets
            ALTER TABLE wallets ADD COLUMN IF NOT EXISTS reserved_points BIGINT NOT NULL DEFAULT 0;
            ALTER TABLE wallets ADD COLUMN IF NOT EXISTS paid_debt_points BIGINT NOT NULL DEFAULT 0;

            -- Add reserved_balance_after to wallet_ledger
            ALTER TABLE wallet_ledger ADD COLUMN IF NOT EXISTS reserved_balance_after BIGINT;

            -- AI usage log
            CREATE TABLE IF NOT EXISTS ai_usage_log (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id             BIGINT NOT NULL,
                operation_type      VARCHAR(64) NOT NULL,
                reservation_id      UUID,
                provider            VARCHAR(32) NOT NULL,
                model               VARCHAR(128) NOT NULL,
                input_tokens        INTEGER,
                output_tokens       INTEGER,
                provider_cost_usd   NUMERIC(12,6),
                attempts            INTEGER DEFAULT 1,
                fallback_used       BOOLEAN DEFAULT FALSE,
                latency_ms          INTEGER,
                status              VARCHAR(32) NOT NULL,
                error_code          VARCHAR(64),
                pricing_version     VARCHAR(16),
                charged_points      BIGINT,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_aiul_user ON ai_usage_log(user_id, created_at);

            -- Seed default payment packages
            INSERT INTO payment_packages (code, title, description, base_points, promo_points, stars_amount, rub_amount_minor, active_for_stars, active_for_yookassa, sort_order)
            VALUES
                ('points_100', 'Старт', '100 AI-баллов', 100, 0, 99, 9900, TRUE, TRUE, 1),
                ('points_300', 'Оптимальный', '300 AI-баллов + 20 бонусных', 300, 20, 299, 29900, TRUE, TRUE, 2),
                ('points_1000', 'Большой', '1000 AI-баллов + 100 бонусных', 1000, 100, 899, 89900, TRUE, TRUE, 3)
            ON CONFLICT (code) DO NOTHING;
        """)

        # Drop stale duplicate indexes from old schema (simple DROP, no CONCURRENTLY needed for small DB)
        await c.execute("DROP INDEX IF EXISTS idx_recipes_user")
        await c.execute("DROP INDEX IF EXISTS idx_recipes_event")

        # ── Backfill share_token ──────────────────────────────────────────────
        rows = await c.fetch("SELECT id FROM events WHERE share_token IS NULL")
        for row in rows:
            await c.execute(
                "UPDATE events SET share_token=$1 WHERE id=$2",
                secrets.token_urlsafe(16), row["id"]
            )

        # ── Backfill users from existing data ────────────────────────────────
        await c.execute("""
            INSERT INTO users (telegram_user_id, first_name, username, onboarding_status)
            SELECT DISTINCT ON (r.user_id) r.user_id, NULL, NULL, 'completed'
            FROM recipes r
            WHERE r.user_id > 0
            ON CONFLICT (telegram_user_id) DO NOTHING
        """)
        await c.execute("""
            INSERT INTO users (telegram_user_id, first_name, username, onboarding_status)
            SELECT DISTINCT ON (w.user_id) w.user_id, NULL, NULL, 'completed'
            FROM wallets w
            ON CONFLICT (telegram_user_id) DO NOTHING
        """)
        await c.execute("""
            INSERT INTO users (telegram_user_id, first_name, username, onboarding_status)
            SELECT DISTINCT ON (c.telegram_user_id) c.telegram_user_id, c.first_name, c.username, 'completed'
            FROM collaborators c
            ON CONFLICT (telegram_user_id) DO NOTHING
        """)

        # ── Seed legal documents ─────────────────────────────────────────────
        import legal_docs
        missing = legal_docs.check_legal_config()
        if missing:
            log.warning("Legal document configuration is incomplete. Missing: %s. "
                        "Documents will use placeholder values.", ", ".join(missing))

        for doc_type, doc_title in legal_docs.DOCUMENT_TYPES.items():
            existing = await c.fetchrow(
                "SELECT id FROM legal_documents WHERE document_type=$1 AND is_active=TRUE",
                doc_type,
            )
            if existing:
                continue
            render_fn = {
                "terms": legal_docs.render_terms,
                "privacy_policy": legal_docs.render_privacy_policy,
                "personal_data_consent": legal_docs.render_personal_data_consent,
                "ai_processing_consent": legal_docs.render_ai_consent,
                "referral_terms": legal_docs.render_referral_terms,
            }.get(doc_type)
            if not render_fn:
                continue
            content = render_fn("1.0")
            c_hash = legal_docs.content_hash(content)
            now = datetime.now(timezone.utc)
            await c.execute(
                "INSERT INTO legal_documents "
                "(document_type, version, title, content, content_hash, published_at, "
                "effective_from, is_active, requires_acceptance) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE, $8)",
                doc_type, "1.0", doc_title, content, c_hash, now, now,
                legal_docs.REQUIRES_ACCEPTANCE.get(doc_type, False),
            )
        log.info("Legal documents seeded ✓")

        # ── Migration: migrate legacy user_balance to new wallet system ──────
        try:
            legacy_users = await c.fetch(
                "SELECT telegram_user_id, balance FROM user_balance WHERE balance > 0")
            migrated = 0
            for u in legacy_users:
                uid = u["telegram_user_id"]
                kopecks = u["balance"]
                points = kopecks // max(1, POINTS_PER_RUBLE)
                if points <= 0:
                    continue
                # Check if wallet already has points (from dual-write)
                existing = await c.fetchrow(
                    "SELECT paid_points FROM wallets WHERE user_id=$1", uid)
                if existing and (existing["paid_points"] or 0) >= points:
                    continue  # Already migrated
                # Create or update wallet
                await c.execute(
                    "INSERT INTO wallets (user_id, paid_points) VALUES ($1, $2) "
                    "ON CONFLICT (user_id) DO UPDATE SET "
                    "paid_points = GREATEST(wallets.paid_points, $2), updated_at=NOW()",
                    uid, points)
                # Create grant
                try:
                    await c.execute(
                        "INSERT INTO wallet_grants "
                        "(user_id, source_type, source_id, wallet_type, initial_points, "
                        "remaining_points, status, created_at, metadata) "
                        "VALUES ($1,'migration','legacy_balance','paid',$2,$2,'active',NOW(),$3)",
                        uid, points, json.dumps({"kopecks": kopecks}))
                except asyncpg.UniqueViolationError:
                    pass
                # Create ledger entry
                try:
                    await c.execute(
                        "INSERT INTO wallet_ledger "
                        "(user_id, wallet_type, amount, transaction_type, idempotency_key, "
                        "paid_balance_after, metadata, created_at) "
                        "VALUES ($1,'paid',$2,'manual_adjustment',$3,$4,$5,NOW())",
                        uid, points, f"migration:{uid}",
                        points, json.dumps({"source": "legacy_user_balance", "kopecks": kopecks}))
                except asyncpg.UniqueViolationError:
                    pass
                migrated += 1
            if migrated:
                log.info("Migrated %d users from legacy user_balance", migrated)
        except Exception as e:
            log.warning("Legacy balance migration skipped: %s", e)

    _db_ready = True
    log.info("DB ready ✓  (recipes-as-library schema v3)")


# ── Ingredient auto-categorisation ───────────────────────────────────────────

INGREDIENT_CATEGORIES: dict[str, list[str]] = {
    "мясо":     ["говядина","свинина","курица","баранина","телятина","фарш","шашлык","колбаса","сосиска","бекон","ветчина","карбонад","шейка","индейка","утка"],
    "рыба":     ["рыба","лосось","тунец","треска","семга","форель","икра","креветк","мидии","кальмар","скумбрия","сельдь","минтай","горбуша","судак"],
    "овощи":    ["картофель","морковь","лук","помидор","огурец","капуста","свекла","чеснок","перец","баклажан","кабачок","тыква","шпинат","салат","редис","зелень","кинза","укроп","петрушка","базилик"],
    "фрукты":   ["яблоко","банан","апельсин","лимон","груша","виноград","слива","персик","клубник","малин","черник","вишня","черешня","абрикос","манго"],
    "молочное": ["молоко","сыр","масло сливочное","сметана","кефир","творог","йогурт","сливки","ряженка","пармезан","моцарелла","брынза"],
    "крупы":    ["рис","гречка","макарон","паста","пшено","овсянка","геркулес","перловка","манка","булгур","кускус","полба","киноа"],
    "специи":   ["соль","перец молот","паприка","куркума","тимьян","розмарин","лавровый","корица","имбирь","мускатный","ванилин","зира","карри","аджика сух"],
    "консервы": ["тушенка","консервы","горошек","кукуруза","фасоль","нут","маслин","оливк","томат пасто","томат конс"],
    "напитки":  ["вода","сок","вино","пиво","водка","шампанское","лимонад","квас","компот","чай","кофе","коньяк"],
    "хлеб":     ["хлеб","батон","булка","лаваш","пита","тост","сухар","багет","лепёшк","хлебцы"],
    "яйца":     ["яйцо","яйца"],
    "соусы":    ["майонез","кетчуп","соевый соус","горчица","хрен","уксус","сальса","ткемали","терияки","табаско","вустерск"],
    "грибы":    ["гриб","шампиньон","лисичк","опят","белый гриб","вешенк","маслят"],
    "масло":    ["масло растительное","масло подсолнечное","масло оливковое","масло кунжутное"],
    "мука":     ["мука","крахмал","разрыхлитель","дрожжи","сода","панировка","манная крупа"],
    "орехи":    ["орех","грецкий","миндаль","кешью","фундук","арахис","фисташк","кедровый","кунжут","семечк"],
    "сахар":    ["сахар","мёд","варенье","джем","сироп","шоколад","какао","ваниль","карамель","глазур"],
}


def categorize_ingredient(name: str) -> str:
    n = name.lower()
    for cat, keywords in INGREDIENT_CATEGORIES.items():
        for kw in keywords:
            if kw in n:
                return cat
    return "прочее"


# ── LLM Recipe Parsing ────────────────────────────────────────────────────────

_URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)

RECIPE_SYSTEM_PROMPT = """Ты — кулинарный редактор. Из присланного контента извлеки рецепт и верни строго JSON.
Если это НЕ рецепт — верни {"not_a_recipe": true}.

JSON-схема (все поля опциональны кроме name):
{
  "name": "название на русском",
  "name_original": "оригинал если не русский",
  "emoji": "одна эмодзи",
  "servings": null,
  "cook_time_minutes": 90,
  "category": "ужин",
  "original_language": "ru",
  "ingredients": [{"name": "Свинина шейка", "qty": 1.5, "unit": "кг"}],
  "steps": [{"text": "Нарезать мясо кусками по 4-5 см"}]
}

category: завтрак|обед|ужин|десерт|суп|салат|закуска|напиток|выпечка|другое
unit: г/кг/мл/л/шт/ст.л/ч.л/щепотка/по вкусу
qty: только число (1.5, 200, 3)
servings: число порций ТОЛЬКО если оно явно указано в рецепте; если не указано — верни null, НЕ угадывай.
Переведи название на русский если оригинал не русский."""

_or_client = None


def _get_or_client():
    global _or_client
    if _or_client is None:
        if not OPENROUTER_KEY:
            raise RuntimeError("OPENROUTER_API_KEY не задан в env")
        from openai import AsyncOpenAI
        _base = OPENROUTER_PROXY_URL.rstrip("/") + "/api/v1" if OPENROUTER_PROXY_URL else "https://openrouter.ai/api/v1"
        _or_client = AsyncOpenAI(
            api_key=OPENROUTER_KEY,
            base_url=_base,
            default_headers={"X-Proxy-Secret": OPENROUTER_PROXY_SECRET} if OPENROUTER_PROXY_SECRET else {},
        )
    return _or_client


async def _llm_normalize_ingredients(raw_strings: list[str]) -> list[dict]:
    """
    Post-process raw ingredient strings from recipe-scrapers
    (e.g. '500г свинины шейки') into structured dicts with qty/unit/category.
    Falls back gracefully: if LLM fails, returns original strings as name-only.
    """
    if not raw_strings:
        return []
    # Skip if already very few items — not worth an LLM call
    # Build a numbered list for the LLM
    numbered = "\n".join(f"{i+1}. {s}" for i, s in enumerate(raw_strings))
    client = _get_or_client()
    prompt = (
        "Нормализуй список ингредиентов рецепта. "
        "Каждая строка содержит количество, единицу и название вместе. "
        "Верни JSON-массив объектов строго в том же порядке:\n"
        '[{"name":"Свинина шейка","qty":500,"unit":"г","category":"мясо"}]\n\n'
        "Правила:\n"
        "- name: только название продукта, без количества\n"
        "- qty: только число (float) или null\n"
        "- unit: г/кг/мл/л/шт/ст.л/ч.л/щепотка/по вкусу  или \"\"\n"
        "- category: мясо|рыба|овощи|фрукты|молочное|яйца|крупы|мука|масло|соусы|специи|орехи|сахар|консервы|хлеб|грибы|напитки|прочее\n"
        "- Не выдумывай, не меняй состав\n\n"
        f"Список ({len(raw_strings)} шт.):\n{numbered}"
    )
    try:
        resp = await client.chat.completions.create(
            model="google/gemini-2.5-flash",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=2000,
        )
        data = json.loads(resp.choices[0].message.content)
        # Response may be {"items": [...]} or just [...]
        items = data if isinstance(data, list) else data.get("items") or data.get("ingredients") or []
        if len(items) == len(raw_strings):
            result = []
            for item in items:
                raw_qty = item.get("qty")
                qty_val = None
                if raw_qty not in (None, "", 0):
                    try:
                        qty_val = float(str(raw_qty).replace(",", "."))
                    except (TypeError, ValueError):
                        pass
                result.append({
                    "name": (item.get("name") or "").strip() or raw_strings[len(result)],
                    "qty": qty_val,
                    "unit": (item.get("unit") or "").strip(),
                    "category": item.get("category") or "прочее",
                })
            return result
    except Exception as e:
        log.warning("_llm_normalize_ingredients failed: %s", e)
    # Fallback: return as name-only
    return [{"name": s.strip(), "qty": None, "unit": "", "category": "прочее"} for s in raw_strings]


async def _llm_parse_text(text: str, source_type: str = "text") -> dict | None:
    """Parse recipe from text using Gemini 2.5 Flash via OpenRouter."""
    client = _get_or_client()
    resp = await client.chat.completions.create(
        model="google/gemini-2.5-flash",
        messages=[
            {"role": "system", "content": RECIPE_SYSTEM_PROMPT},
            {"role": "user", "content": f"Контент:\n\n{text[:8000]}"},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=2500,
    )
    raw = resp.choices[0].message.content
    data = json.loads(raw)
    if data.get("not_a_recipe"):
        return None
    data["source_type"] = source_type
    return data


# Vision models tried in order — gemini first (multimodal, reliable), qwen fallback.
_VISION_MODELS = ["google/gemini-2.5-flash", "qwen/qwen2.5-vl-72b-instruct"]


async def _llm_parse_image(image_bytes: bytes) -> dict | None:
    """Parse recipe from image. Tries several vision models so one provider being
    rate-limited (429) doesn't kill the import."""
    import base64
    client = _get_or_client()
    b64 = base64.b64encode(image_bytes).decode()
    messages = [
        {"role": "system", "content": RECIPE_SYSTEM_PROMPT},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": "Это изображение рецепта. Извлеки и верни JSON."},
        ]},
    ]
    last_err = None
    for model in _VISION_MODELS:
        try:
            resp = await client.chat.completions.create(
                model=model, messages=messages,
                response_format={"type": "json_object"},
                temperature=0.1, max_tokens=2500,
            )
            data = json.loads(resp.choices[0].message.content)
            if data.get("not_a_recipe"):
                return None
            data["source_type"] = "photo"
            return data
        except Exception as e:
            last_err = e
            log.warning("vision parse via %s failed: %s", model, e)
            continue
    log.error("all vision models failed: %s", last_err)
    raise ValueError(
        "Сервис распознавания фото сейчас перегружен. Попробуй через минуту "
        "или пришли рецепт текстом / ссылкой."
    )


async def _llm_parse_images(images: list[bytes]) -> list[dict]:
    """Parse 1..N recipes from photos sent as one album. The model decides:
    several pages of ONE recipe → merge into one; distinct dishes → separate."""
    import base64
    client = _get_or_client()
    content: list = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(b).decode()}"}}
        for b in images
    ]
    content.append({"type": "text", "text": (
        f"На фото {len(images)} изображени(й). Это могут быть страницы ОДНОГО рецепта "
        "или НЕСКОЛЬКО РАЗНЫХ рецептов. Реши сам и верни JSON вида "
        '{"recipes": [<рецепт>, ...]} — по одному объекту на КАЖДЫЙ отдельный рецепт '
        "(схема рецепта как в системном промпте). Один рецепт на нескольких фото → массив из одного объекта."
    )})
    resp = await client.chat.completions.create(
        model="qwen/qwen2.5-vl-72b-instruct",
        messages=[
            {"role": "system", "content": RECIPE_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=4000,
    )
    data = json.loads(resp.choices[0].message.content)
    recipes = data.get("recipes")
    if recipes is None:  # model ignored the wrapper → treat whole object as one recipe
        recipes = [] if data.get("not_a_recipe") else [data]
    out = []
    for r in recipes:
        if isinstance(r, dict) and r.get("name") and not r.get("not_a_recipe"):
            r["source_type"] = "photo"
            out.append(r)
    return out


_WHISPER_PROMPT = (
    "Кулинарный рецепт. Точно распознай названия продуктов, цифры и единицы измерения: "
    "граммы, килограммы, штуки, ложки, стаканы. "
    "Пример правильного ввода: «возьмите 500 граммов свинины, 3 луковицы, 2 столовые ложки масла»."
)

async def _transcribe_voice(audio_bytes: bytes) -> str:
    """Transcribe voice via OpenRouter (openai/whisper-large-v3) with culinary hint."""
    client = _get_or_client()
    resp = await client.audio.transcriptions.create(
        model="openai/whisper-large-v3",
        file=("voice.ogg", io.BytesIO(audio_bytes), "audio/ogg"),
        language="ru",
        temperature=0.1,
        prompt=_WHISPER_PROMPT,
    )
    return resp.text


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


async def _ensure_public_url(u: str) -> None:
    """SSRF guard: allow only http(s) to a publicly-routable host. Raises ValueError."""
    import ipaddress
    from urllib.parse import urlparse
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Недопустимый URL")
    try:
        infos = await asyncio.get_event_loop().getaddrinfo(p.hostname, None)
    except Exception:
        raise ValueError("Не удалось разрешить адрес сайта")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ValueError("Доступ к этому адресу запрещён")


async def _fetch_page_html(url: str) -> str:
    """Fetch HTML with browser-like headers. SSRF-guarded: public hosts only,
    each redirect hop re-validated (raises ValueError / HTTPStatusError on failure)."""
    cur = url
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for _ in range(5):
            await _ensure_public_url(cur)
            resp = await client.get(cur, headers=_BROWSER_HEADERS)
            loc = resp.headers.get("location")
            if resp.is_redirect and loc:
                cur = str(httpx.URL(cur).join(loc))
                continue
            resp.raise_for_status()
            return resp.text
    raise ValueError("Слишком много перенаправлений")


def _html_to_text(html: str) -> str:
    """Strip tags and return readable text (max 8 000 chars) for LLM."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)[:8000]


async def _try_recipe_scraper(url: str, html: str) -> dict | None:
    """Try recipe-scrapers with pre-fetched HTML. Returns None if site not supported."""
    try:
        from recipe_scrapers import scrape_html
        scraper = scrape_html(html, org_url=url)
        try:
            raw_yield = str(scraper.yields() or "")
            m = re.search(r'\d+', raw_yield)
            servings = int(m.group()) if m else None
        except Exception:
            servings = None
        raw_ingredient_strings = [s.strip() for s in (scraper.ingredients() or []) if s.strip()]
        steps = []
        try:
            for s in (scraper.instructions_list() or []):
                if s.strip():
                    steps.append({"text": s.strip()})
        except Exception:
            raw = scraper.instructions()
            if raw:
                steps = [{"text": raw.strip()}]
        title = scraper.title() or ""
        if not title or not raw_ingredient_strings:
            return None   # scraper found nothing useful, fall through to LLM

        # Normalize raw strings ("500г свинины") → {name, qty, unit, category}
        ingredients = await _llm_normalize_ingredients(raw_ingredient_strings)

        return {
            "name": title,
            "servings": servings,
            "cook_time_minutes": scraper.total_time() or None,
            "ingredients": ingredients,
            "steps": steps,
            "source_type": "url",
        }
    except Exception:
        return None


async def parse_and_save_recipe(
    user_id: int,
    *,
    url: str | None = None,
    text: str | None = None,
    image_bytes: bytes | None = None,
    image_file_id: str | None = None,
    audio_bytes: bytes | None = None,
) -> dict:
    """Full pipeline: detect type → LLM parse → save to DB. Returns saved recipe dict."""
    # AI consent check — must have consent before sending to OpenRouter
    if pool is not None:
        try:
            async with pool.acquire() as db:
                await LegalConsentService.require_ai_access(db, user_id)
        except ValueError:
            raise ValueError("ai_consent_required")

    # Determine operation type for billing
    if pool is not None and FEATURE_AI_BILLING:
        if image_bytes or image_file_id:
            op_type = "recipe_image_parse"
        elif audio_bytes:
            op_type = "recipe_voice_parse"
        elif url:
            op_type = "recipe_url_parse"
        else:
            op_type = "recipe_text_parse"
        async with pool.acquire() as db:
            ok, needed, available = await AIUsageBillingService.check_balance(db, user_id, op_type)
        if not ok:
            raise ValueError(f"insufficient_balance:{needed}:{available}")
        async with pool.acquire() as db:
            reservation_ok, operation_id = await AIUsageBillingService.reserve_points(db, user_id, op_type)
        if not reservation_ok:
            raise ValueError("ai_reservation_failed")
    else:
        op_type = None
        operation_id = None

    parsed: dict | None = None

    if url:
        # 1. Fetch HTML once with browser headers
        try:
            html = await _fetch_page_html(url)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (403, 429, 503):
                raise ValueError(
                    f"Сайт закрыл доступ для бота (HTTP {code}).\n"
                    "Скопируйте текст рецепта со страницы и пришлите его мне напрямую."
                ) from exc
            raise ValueError(f"Не удалось загрузить страницу (HTTP {code}).") from exc

        # 2. Try recipe-scrapers (structured, no LLM cost)
        parsed = await _try_recipe_scraper(url, html)
        if parsed is None:
            # 3. LLM fallback: feed plain text
            page_text = _html_to_text(html)
            parsed = await _llm_parse_text(page_text, source_type="url")
        if parsed:
            parsed["source_url"] = url
            parsed.setdefault("source_type", "url")

    elif image_bytes:
        parsed = await _llm_parse_image(image_bytes)
        if parsed and image_file_id:
            parsed["source_photo_file_id"] = image_file_id

    elif audio_bytes:
        transcript = await _transcribe_voice(audio_bytes)
        log.info("Voice transcript: %s", transcript[:200])
        if not transcript or len(transcript.strip()) < 10:
            raise ValueError("Не удалось распознать речь. Попробуйте говорить чётче или пришлите текст.")
        parsed = await _llm_parse_text(
            f"[Голосовое сообщение, расшифровка Whisper]\n\n{transcript}",
            source_type="voice",
        )
        if not parsed:
            raise ValueError(
                f"Не смог извлечь рецепт из голосового.\n\n"
                f"Распознанный текст:\n«{transcript[:300]}»\n\n"
                "Если это рецепт — пришлите текстом."
            )

    elif text:
        parsed = await _llm_parse_text(text, source_type="manual")

    if not parsed:
        # Release reservation on failure
        if operation_id and pool is not None:
            async with pool.acquire() as db:
                await AIUsageBillingService.release_reservation(db, operation_id, "parse_failed")
        raise ValueError("Не удалось распознать рецепт в этом контенте")

    result = await _save_parsed_recipe(user_id, parsed)

    # Commit reservation on success
    if operation_id and pool is not None:
        async with pool.acquire() as db:
            await AIUsageBillingService.commit_charge(
                db, operation_id, provider="openrouter",
                model=AI_OPERATION_CATALOG.get(op_type, {}).get("model", "unknown"))

    return result


async def _save_parsed_recipe(user_id: int, parsed: dict) -> dict:
    """Persist a parsed recipe dict to DB. Returns minimal response dict."""
    if pool is None:
        raise RuntimeError("DB not ready")

    async with pool.acquire() as db:
        try:
            rec = await db.fetchrow(
                """
                INSERT INTO recipes
                    (user_id, name, name_original, emoji, source_url, source_type,
                     original_language, servings, cook_time_minutes, category, source_photo_file_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                RETURNING *
                """,
                user_id,
                (parsed.get("name") or "Рецепт").strip(),
                parsed.get("name_original"),
                parsed.get("emoji") or "🍽",
                parsed.get("source_url"),
                parsed.get("source_type", "manual"),
                parsed.get("original_language"),
                int(parsed["servings"]) if parsed.get("servings") else None,
                int(parsed["cook_time_minutes"]) if parsed.get("cook_time_minutes") else None,
                parsed.get("category"),
                parsed.get("source_photo_file_id"),
            )
        except asyncpg.UniqueViolationError:
            # Same URL already in this user's library — return existing
            existing = await db.fetchrow(
                "SELECT * FROM recipes WHERE user_id=$1 AND source_url=$2",
                user_id, parsed.get("source_url"),
            )
            ing_count = await db.fetchval(
                "SELECT COUNT(*) FROM ingredients WHERE recipe_id=$1", existing["id"]
            )
            return {
                "id": existing["id"], "name": existing["name"],
                "emoji": existing["emoji"] or "🍽",
                "servings": existing["servings"],
                "cook_time_minutes": existing["cook_time_minutes"],
                "category": existing["category"],
                "ingredients_count": ing_count or 0,
                "already_exists": True,
            }

        recipe_id = rec["id"]
        ing_count = 0
        for i, ing in enumerate(parsed.get("ingredients", [])):
            ing_name = (ing.get("name") or "").strip()
            if not ing_name:
                continue
            raw_qty = ing.get("qty")
            qty_val = None
            if raw_qty not in (None, "", 0):
                try:
                    qty_val = float(str(raw_qty).replace(",", "."))
                except (TypeError, ValueError):
                    pass
            await db.execute(
                "INSERT INTO ingredients (recipe_id, name, qty, unit, category, sort_order) VALUES ($1,$2,$3,$4,$5,$6)",
                recipe_id, ing_name, qty_val,
                (ing.get("unit") or "").strip(),
                categorize_ingredient(ing_name),
                i,
            )
            ing_count += 1

        for i, step in enumerate(parsed.get("steps", [])):
            step_text = (step.get("text") or "").strip()
            if not step_text:
                continue
            await db.execute(
                "INSERT INTO recipe_steps (recipe_id, step_number, text) VALUES ($1,$2,$3)",
                recipe_id, i + 1, step_text,
            )

    return {
        "id": recipe_id,
        "name": rec["name"],
        "emoji": rec["emoji"] or "🍽",
        "servings": rec["servings"],
        "cook_time_minutes": rec["cook_time_minutes"],
        "category": rec["category"],
        "ingredients_count": ing_count,
        "already_exists": False,
    }


# ── Auth ─────────────────────────────────────────────────────────────────────

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


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="ПОЛЯНА API", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "https://coiqastore-ai.github.io",
        "https://web.telegram.org",
        "https://telegram.org",
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {
        "status": "ok", "service": "ПОЛЯНА API v3.0",
        "rev": "audit-fixes-1",
        "db_ready": _db_ready,
        "db_error": _db_error,
    }


# ── GET /api/files/photo/{file_id}  (proxy a Telegram photo) ─────────────────
# Streams the image bytes through the backend so the bot token stays server-side.
# Public (no auth) — <img> tags cannot send the init-data header. file_id is opaque.

@app.get("/api/files/photo/{file_id}")
async def get_recipe_photo(file_id: str):
    if not file_id or len(file_id) > 256:
        raise HTTPException(404, "Bad file id")
    try:
        tg_file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path, buf)
    except Exception:
        raise HTTPException(404, "Photo not available")
    data = buf.getvalue()
    if not data:
        raise HTTPException(404, "Empty photo")
    # Telegram photos are JPEG
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/admin/migration-check")
async def migration_check(user_id: int = Depends(get_current_user), db=Depends(get_db)):
    """Structural migration verification — admin only."""
    if user_id != ADMIN_CHAT_ID:
        raise HTTPException(403, "Forbidden")
    # БЛОК 1: orphan check
    recipes_without_user = await db.fetchval(
        "SELECT COUNT(*) FROM recipes WHERE user_id IS NULL"
    )
    recipes_with_zero = await db.fetchval(
        "SELECT COUNT(*) FROM recipes WHERE user_id = 0"
    )

    # БЛОК 1: priority check sample (first 10 rows)
    priority_rows = await db.fetch("""
        SELECT r.id,
               r.user_id,
               r.added_by_user_id,
               CASE
                 WHEN r.added_by_user_id IS NOT NULL
                      THEN r.user_id = r.added_by_user_id
                 ELSE NULL
               END AS priority_correct
        FROM recipes r
        LIMIT 10
    """)

    # БЛОК 1: duplicates in event_recipes
    dup_count = await db.fetchval("""
        SELECT COUNT(*) FROM (
            SELECT event_id, recipe_id FROM event_recipes
            GROUP BY event_id, recipe_id HAVING COUNT(*) > 1
        ) x
    """)

    # БЛОК 4: indexes on recipes
    indexes = await db.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'recipes' ORDER BY indexname"
    )

    # БЛОК 4: constraints on recipes
    constraints = await db.fetch("""
        SELECT conname, contype, pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE conrelid = 'recipes'::regclass
        ORDER BY conname
    """)

    # event_recipes columns (verify added_by_id exists)
    er_columns = await db.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name='event_recipes' ORDER BY ordinal_position"
    )

    # Analytics funnel snapshot (deploy verification + lightweight K-factor dashboard)
    try:
        ae = await db.fetch(
            "SELECT event_type, COUNT(*) c, COUNT(DISTINCT user_id) u FROM analytics_events GROUP BY event_type"
        )
        analytics = {r["event_type"]: {"count": r["c"], "users": r["u"]} for r in ae}
        joined = await db.fetchval("SELECT COUNT(*) FROM analytics_events WHERE event_type='guest_joined'")
        became = await db.fetchval("SELECT COUNT(*) FROM analytics_events WHERE event_type='guest_became_organizer'")
        avg_guests = await db.fetchval(
            "SELECT COALESCE(AVG(c),0) FROM (SELECT event_ref, COUNT(*) c FROM analytics_events "
            "WHERE event_type='guest_joined' GROUP BY event_ref) t"
        )
        g2o = (became / joined) if joined else 0.0
        analytics["_guest_to_organizer"] = round(g2o, 3)
        analytics["_k_factor"] = round(float(avg_guests or 0) * g2o, 3)
    except Exception as e:
        analytics = {"error": type(e).__name__}

    return {
        "блок1_recipes_without_user": recipes_without_user,
        "блок1_recipes_user_id_zero": recipes_with_zero,
        "блок1_priority_sample": [dict(r) for r in priority_rows],
        "блок1_event_recipes_duplicates": dup_count,
        "блок4_indexes_on_recipes": [{"name": r["indexname"], "def": r["indexdef"]} for r in indexes],
        "блок4_constraints_on_recipes": [{"name": r["conname"], "type": r["contype"], "def": r["def"]} for r in constraints],
        "event_recipes_columns": [r["column_name"] for r in er_columns],
        "analytics": analytics,
    }


# ── Progress helpers ──────────────────────────────────────────────────────────

def compute_progress(recipes_count: int, shopping_total: int, shopping_bought: int) -> int:
    p = 0
    if recipes_count >= 1: p += 20
    if recipes_count >= 2: p += 15
    if recipes_count >= 3: p += 15
    if shopping_total > 0:
        p += int(30 * shopping_bought / shopping_total)
    return min(p, 100)


def next_step_hint(recipes_count: int) -> dict:
    if recipes_count == 0:
        return {"text": "Добавьте первое блюдо в меню", "action": "add_recipe"}
    if recipes_count < 3:
        return {"text": f"Добавьте ещё {3 - recipes_count} блюда", "action": "add_recipe"}
    return {"text": "Разошлите приглашения гостям", "action": "invite"}


# ── GET /api/events ───────────────────────────────────────────────────────────

@app.get("/api/events")
async def list_events(user_id: int = Depends(get_current_user), db=Depends(get_db)):
    rows = await db.fetch(
        """
        SELECT e.id, e.name, e.event_date, e.location, e.template, e.share_token, e.telegram_user_id,
               (SELECT COUNT(*) FROM event_recipes er WHERE er.event_id = e.id) AS recipes_count,
               (SELECT COUNT(*) FROM shopping_items s WHERE s.event_id = e.id)  AS shopping_total,
               (SELECT COUNT(*) FROM shopping_items s WHERE s.event_id = e.id AND s.bought) AS shopping_bought,
               (SELECT COUNT(*) FROM collaborators c WHERE c.event_id = e.id)   AS collab_count
        FROM events e
        WHERE e.telegram_user_id = $1
           OR EXISTS (SELECT 1 FROM collaborators c WHERE c.event_id = e.id AND c.telegram_user_id = $1)
        ORDER BY e.event_date ASC NULLS LAST
        """,
        user_id,
    )
    events = []
    for r in rows:
        rc = r["recipes_count"] or 0
        st = r["shopping_total"] or 0
        sb = r["shopping_bought"] or 0
        events.append({
            "id": r["id"],
            "name": r["name"],
            "event_date": r["event_date"].isoformat() if r["event_date"] else None,
            "location": r["location"],
            "template": r["template"],
            "share_token": r["share_token"],
            "guests_count": (r["collab_count"] or 0) + 1,
            "recipes_count": rc,
            "shopping_items_count": st,
            "progress_percent": compute_progress(rc, st, sb),
            "is_owner": r["telegram_user_id"] == user_id,
            "owner_id": r["telegram_user_id"],
        })
    return {"events": events}


# ── POST /api/events ──────────────────────────────────────────────────────────

@app.post("/api/events", status_code=201)
async def create_event(body: dict, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")

    # K-factor: read state BEFORE creating this event.
    #  prior_owned == 0 AND has_joined > 0  → a guest just converted into an organizer.
    prior_owned = await db.fetchval(
        "SELECT COUNT(*) FROM events WHERE telegram_user_id=$1", user_id
    )
    has_joined = await db.fetchval(
        "SELECT COUNT(*) FROM collaborators WHERE telegram_user_id=$1 AND role<>'owner'", user_id
    )

    event_date = None
    raw = body.get("event_date")
    if raw:
        try:
            event_date = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, "Invalid event_date (ISO 8601 expected)")

    share_token = secrets.token_urlsafe(16)
    row = await db.fetchrow(
        """
        INSERT INTO events (name, event_date, location, description, template, share_token, telegram_user_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id, name, share_token, telegram_user_id
        """,
        name, event_date,
        body.get("location"), body.get("description"), body.get("template"),
        share_token, user_id,
    )
    await db.execute(
        """
        INSERT INTO collaborators (event_id, telegram_user_id, first_name, username, role)
        VALUES ($1,$2,$3,$4,'owner') ON CONFLICT DO NOTHING
        """,
        row["id"], user_id,
        body.get("owner_first_name", ""), body.get("owner_username", ""),
    )
    await track(user_id, "event_created", props={"event_id": row["id"]}, event_ref=row["id"])
    if (prior_owned or 0) == 0 and (has_joined or 0) > 0:
        await track(user_id, "guest_became_organizer", props={"event_id": row["id"]}, event_ref=row["id"])
    return {"id": row["id"], "name": row["name"], "share_token": row["share_token"], "owner_id": user_id}


# ── GET /api/events/shared/{event_id} (no-auth) ───────────────────────────────
# Must be registered BEFORE /api/events/{event_id} to avoid route shadowing

@app.get("/api/events/shared/{event_id}")
async def get_shared_event(event_id: int, db=Depends(get_db)):
    row = await db.fetchrow(
        "SELECT id, name, event_date, location, guests_count FROM events WHERE id=$1", event_id
    )
    if not row:
        raise HTTPException(404, "Not found")
    return {
        "id": row["id"], "name": row["name"],
        "event_date": row["event_date"].isoformat() if row["event_date"] else None,
        "location": row["location"], "guests_count": row["guests_count"], "read_only": True,
    }


# ── GET /api/events/{id} ──────────────────────────────────────────────────────

@app.get("/api/events/{event_id}")
async def get_event(event_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    row = await db.fetchrow("SELECT * FROM events WHERE id=$1", event_id)
    if not row:
        raise HTTPException(404, "Event not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if row["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")

    collabs = await db.fetch(
        "SELECT * FROM collaborators WHERE event_id=$1 ORDER BY joined_at ASC", event_id
    )

    # Recipes via event_recipes M2M join
    recipes = await db.fetch(
        """
        SELECT r.id, r.name, r.emoji, r.servings, r.cook_time_minutes,
               r.user_id AS recipe_owner_id,
               er.servings_multiplier, er.added_by_id, er.added_at AS linked_at,
               (SELECT COUNT(*) FROM ingredients i WHERE i.recipe_id = r.id) AS ingredients_count
        FROM event_recipes er
        JOIN recipes r ON r.id = er.recipe_id
        WHERE er.event_id = $1
        ORDER BY er.added_at ASC
        """,
        event_id,
    )

    shop_row = await db.fetchrow(
        "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE bought) AS bought FROM shopping_items WHERE event_id=$1",
        event_id,
    )
    rc, st, sb = len(recipes), (shop_row["total"] or 0), (shop_row["bought"] or 0)

    # Collaborator name lookup
    collab_names = {c["telegram_user_id"]: c["first_name"] or "Гость" for c in collabs}

    return {
        "id": row["id"],
        "name": row["name"],
        "event_date": row["event_date"].isoformat() if row["event_date"] else None,
        "location": row.get("location") or "",
        "description": row.get("description") or "",
        "template": row.get("template") or "",
        "share_token": row["share_token"],
        "owner_id": row["telegram_user_id"],
        "is_owner": row["telegram_user_id"] == user_id,
        "progress_percent": compute_progress(rc, st, sb),
        "next_step": next_step_hint(rc),
        "collaborators": [
            {
                "user_id": c["telegram_user_id"],
                "first_name": c["first_name"] or "Гость",
                "username": c["username"],
                "role": c["role"],
                "recipes_count": sum(1 for r in recipes if r["added_by_id"] == c["telegram_user_id"]),
            }
            for c in collabs
        ],
        "recipes": [
            {
                "id": r["id"],
                "name": r["name"],
                "emoji": r["emoji"] or "🍽",
                "servings": r["servings"],
                "cook_time_min": r["cook_time_minutes"],        # compat alias
                "cook_time_minutes": r["cook_time_minutes"],
                "servings_multiplier": float(r["servings_multiplier"] or 1.0),
                "ingredients_count": r["ingredients_count"] or 0,
                "added_by": {
                    "user_id": r["added_by_id"],
                    "first_name": collab_names.get(r["added_by_id"], "Гость"),
                },
                "added_at": r["linked_at"].isoformat() if r["linked_at"] else None,
            }
            for r in recipes
        ],
    }


# ── PATCH /api/events/{id} ────────────────────────────────────────────────────

@app.patch("/api/events/{event_id}")
async def update_event(event_id: int, body: dict, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    owner = await db.fetchval("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if owner != user_id:
        raise HTTPException(403, "Access denied")
    allowed = ("name", "event_date", "location", "description", "guests_count")
    fields = {k: v for k, v in body.items() if k in allowed and v is not None}
    if not fields:
        raise HTTPException(400, "No updatable fields")
    sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(fields))
    row = await db.fetchrow(
        f"UPDATE events SET {sets} WHERE id=$1 RETURNING *", event_id, *fields.values()
    )
    return dict(row)


# ── DELETE /api/events/{id} ───────────────────────────────────────────────────

@app.delete("/api/events/{event_id}", status_code=204)
async def delete_event(event_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    owner = await db.fetchval("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if owner is None:
        raise HTTPException(404, "Event not found")
    if owner != user_id:
        raise HTTPException(403, "Access denied")
    # Explicitly remove children first — don't rely on FK ON DELETE CASCADE,
    # since legacy tables in production may have been created without it.
    # (Recipes are library-owned and shared, so they are NOT deleted here.)
    await db.execute("DELETE FROM shopping_items WHERE event_id=$1", event_id)
    await db.execute("DELETE FROM event_recipes  WHERE event_id=$1", event_id)
    await db.execute("DELETE FROM collaborators   WHERE event_id=$1", event_id)
    # Legacy table from an older schema — clean up only if it still exists.
    try:
        await db.execute("DELETE FROM event_menu_items WHERE event_id=$1", event_id)
    except asyncpg.UndefinedTableError:
        pass
    await db.execute("DELETE FROM events WHERE id=$1", event_id)


# ── POST /api/events/{id}/recipes ─────────────────────────────────────────────
# Mode 1: {"recipe_id": 123, "servings_multiplier": 2.0}  → link existing library recipe
# Mode 2: {"name": "...", "emoji": "🥩", ...}             → create in library + link

@app.post("/api/events/{event_id}/recipes", status_code=201)
async def add_recipe_to_event(event_id: int, body: dict, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if ev["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")

    recipe_id = body.get("recipe_id")

    if recipe_id:
        # ── Mode 1: link existing recipe from user's library ──────────────────
        rec = await db.fetchrow(
            "SELECT id, name, emoji, servings FROM recipes WHERE id=$1 AND user_id=$2",
            int(recipe_id), user_id
        )
        if not rec:
            raise HTTPException(404, "Recipe not found in your library")

        mult = float(body.get("servings_multiplier") or 1.0)
        await db.execute(
            """
            INSERT INTO event_recipes (event_id, recipe_id, servings_multiplier, added_by_id)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (event_id, recipe_id) DO UPDATE
                SET servings_multiplier = EXCLUDED.servings_multiplier
            """,
            event_id, rec["id"], mult, user_id,
        )
        await _resync_shopping_if_exists(event_id, db)
        return {
            "id": rec["id"], "name": rec["name"],
            "emoji": rec["emoji"] or "🍽",
            "servings": rec["servings"],
            "servings_multiplier": mult,
        }

    else:
        # ── Mode 2: create new recipe in library, then link to event ──────────
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name required")

        rec = await db.fetchrow(
            """
            INSERT INTO recipes
                (user_id, name, emoji, servings, cook_time_minutes, source_url, source_type)
            VALUES ($1,$2,$3,$4,$5,$6,'manual')
            RETURNING *
            """,
            user_id, name,
            body.get("emoji", "🍽"),
            body.get("servings", 4),
            body.get("cook_time_min") or body.get("cook_time_minutes"),
            body.get("source_url"),
        )

        # Persist ingredients
        for i, ing in enumerate(body.get("ingredients", [])):
            ing_name = (ing.get("name") or "").strip()
            if not ing_name:
                continue
            raw_qty = ing.get("qty")
            qty_val = None
            if raw_qty not in (None, "", 0):
                try:
                    qty_val = float(raw_qty)
                except (TypeError, ValueError):
                    qty_val = None
            await db.execute(
                "INSERT INTO ingredients (recipe_id, name, qty, unit, category, sort_order) VALUES ($1,$2,$3,$4,$5,$6)",
                rec["id"], ing_name, qty_val,
                (ing.get("unit") or "").strip(),
                categorize_ingredient(ing_name),
                i,
            )

        # Persist steps
        for i, step in enumerate(body.get("steps", [])):
            step_text = (step.get("text") or "").strip()
            if not step_text:
                continue
            await db.execute(
                "INSERT INTO recipe_steps (recipe_id, step_number, text) VALUES ($1,$2,$3)",
                rec["id"], i + 1, step_text,
            )

        # Link to event via event_recipes
        await db.execute(
            """
            INSERT INTO event_recipes (event_id, recipe_id, servings_multiplier, added_by_id)
            VALUES ($1,$2,1.0,$3)
            ON CONFLICT (event_id, recipe_id) DO NOTHING
            """,
            event_id, rec["id"], user_id,
        )

        await _resync_shopping_if_exists(event_id, db)
        return {
            "id": rec["id"], "name": rec["name"],
            "emoji": rec["emoji"] or "🍽",
            "servings": rec["servings"],
            "servings_multiplier": 1.0,
            "added_at": rec["created_at"].isoformat() if rec["created_at"] else None,
        }


# ── PATCH /api/events/{id}/recipes/{id} (update multiplier) ──────────────────

@app.patch("/api/events/{event_id}/recipes/{recipe_id}")
async def update_event_recipe(
    event_id: int, recipe_id: int, body: dict,
    user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if ev["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")

    mult = float(body.get("servings_multiplier") or 1.0)
    await db.execute(
        "UPDATE event_recipes SET servings_multiplier=$1 WHERE event_id=$2 AND recipe_id=$3",
        mult, event_id, recipe_id,
    )
    return {"servings_multiplier": mult}


# ── DELETE /api/events/{id}/recipes/{id} (unlink only — library intact) ───────

@app.delete("/api/events/{event_id}/recipes/{recipe_id}", status_code=204)
async def unlink_recipe_from_event(
    event_id: int, recipe_id: int,
    user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    er = await db.fetchrow(
        "SELECT added_by_id FROM event_recipes WHERE event_id=$1 AND recipe_id=$2",
        event_id, recipe_id,
    )
    if not er:
        raise HTTPException(404, "Recipe not linked to this event")
    rec_owner = await db.fetchval("SELECT user_id FROM recipes WHERE id=$1", recipe_id)
    if ev["telegram_user_id"] != user_id and er["added_by_id"] != user_id and rec_owner != user_id:
        raise HTTPException(403, "Access denied")
    await db.execute(
        "DELETE FROM event_recipes WHERE event_id=$1 AND recipe_id=$2", event_id, recipe_id
    )
    await _resync_shopping_if_exists(event_id, db)


# ── GET /api/recipes  (personal library) ─────────────────────────────────────

@app.get("/api/recipes")
async def list_recipes(
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    user_id: int = Depends(get_current_user),
    db=Depends(get_db),
):
    where_parts = ["r.user_id = $1"]
    params: list = [user_id]

    if q:
        params.append(f"%{q.lower()}%")
        where_parts.append(f"LOWER(r.name) LIKE ${len(params)}")
    if category:
        params.append(category)
        where_parts.append(f"r.category = ${len(params)}")

    where_sql = " AND ".join(where_parts)
    rows = await db.fetch(
        f"""
        SELECT r.id, r.name, r.name_original, r.emoji, r.servings, r.cook_time_minutes,
               r.category, r.tags, r.times_cooked, r.rating, r.source_url, r.source_type,
               r.notes, r.created_at,
               (SELECT COUNT(*) FROM ingredients i WHERE i.recipe_id = r.id) AS ingredients_count
        FROM recipes r
        WHERE {where_sql}
        ORDER BY r.created_at DESC
        """,
        *params,
    )
    return {
        "recipes": [
            {
                "id": r["id"],
                "name": r["name"],
                "name_original": r["name_original"],
                "emoji": r["emoji"] or "🍽",
                "servings": r["servings"],
                "cook_time_minutes": r["cook_time_minutes"],
                "cook_time_min": r["cook_time_minutes"],   # compat
                "category": r["category"],
                "tags": list(r["tags"] or []),
                "times_cooked": r["times_cooked"] or 0,
                "rating": r["rating"],
                "source_url": r["source_url"],
                "source_type": r["source_type"] or "manual",
                "notes": r["notes"],
                "ingredients_count": r["ingredients_count"] or 0,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


# ── POST /api/recipes  (add to personal library directly) ────────────────────

@app.post("/api/recipes", status_code=201)
async def create_recipe(body: dict, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")

    rec = await db.fetchrow(
        """
        INSERT INTO recipes
            (user_id, name, name_original, emoji, source_url, source_type,
             original_language, servings, cook_time_minutes, category, notes)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        RETURNING *
        """,
        user_id, name,
        body.get("name_original"),
        body.get("emoji", "🍽"),
        body.get("source_url"),
        body.get("source_type", "manual"),
        body.get("original_language"),
        body.get("servings", 4),
        body.get("cook_time_min") or body.get("cook_time_minutes"),
        body.get("category"),
        body.get("notes"),
    )

    for i, ing in enumerate(body.get("ingredients", [])):
        ing_name = (ing.get("name") or "").strip()
        if not ing_name:
            continue
        raw_qty = ing.get("qty")
        qty_val = None
        if raw_qty not in (None, "", 0):
            try:
                qty_val = float(raw_qty)
            except (TypeError, ValueError):
                qty_val = None
        await db.execute(
            "INSERT INTO ingredients (recipe_id, name, qty, unit, category, sort_order) VALUES ($1,$2,$3,$4,$5,$6)",
            rec["id"], ing_name, qty_val,
            (ing.get("unit") or "").strip(),
            categorize_ingredient(ing_name),
            i,
        )

    for i, step in enumerate(body.get("steps", [])):
        step_text = (step.get("text") or "").strip()
        if not step_text:
            continue
        await db.execute(
            "INSERT INTO recipe_steps (recipe_id, step_number, text) VALUES ($1,$2,$3)",
            rec["id"], i + 1, step_text,
        )

    return {
        "id": rec["id"], "name": rec["name"], "emoji": rec["emoji"] or "🍽",
        "user_id": rec["user_id"], "servings": rec["servings"],
        "cook_time_minutes": rec["cook_time_minutes"],
        "created_at": rec["created_at"].isoformat() if rec["created_at"] else None,
    }


# ── PATCH /api/recipes/{id}  (edit recipe + ingredients/steps) ───────────────

@app.patch("/api/recipes/{recipe_id}")
async def update_recipe(
    recipe_id: int, body: dict,
    user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    rec = await db.fetchrow("SELECT user_id FROM recipes WHERE id=$1", recipe_id)
    if not rec:
        raise HTTPException(404, "Recipe not found")
    if rec["user_id"] != user_id:
        raise HTTPException(403, "Access denied")

    # Update scalar fields that are present in the body
    scalar_map = {
        "name": "name",
        "emoji": "emoji",
        "servings": "servings",
        "category": "category",
        "notes": "notes",
    }
    sets, params = [], []
    for body_key, col in scalar_map.items():
        if body_key in body and body[body_key] is not None:
            params.append(body[body_key])
            sets.append(f"{col} = ${len(params)}")
    # cook time accepts either alias
    if "cook_time_min" in body or "cook_time_minutes" in body:
        params.append(body.get("cook_time_min") or body.get("cook_time_minutes"))
        sets.append(f"cook_time_minutes = ${len(params)}")
    if sets:
        params.append(recipe_id)
        await db.execute(
            f"UPDATE recipes SET {', '.join(sets)} WHERE id = ${len(params)}", *params
        )

    # Replace ingredients if the key is present (even if empty list = clear all)
    if "ingredients" in body:
        await db.execute("DELETE FROM ingredients WHERE recipe_id=$1", recipe_id)
        for i, ing in enumerate(body.get("ingredients") or []):
            ing_name = (ing.get("name") or "").strip()
            if not ing_name:
                continue
            raw_qty = ing.get("qty")
            qty_val = None
            if raw_qty not in (None, "", 0):
                try:
                    qty_val = float(raw_qty)
                except (TypeError, ValueError):
                    qty_val = None
            await db.execute(
                "INSERT INTO ingredients (recipe_id, name, qty, unit, category, sort_order) VALUES ($1,$2,$3,$4,$5,$6)",
                recipe_id, ing_name, qty_val,
                (ing.get("unit") or "").strip(),
                categorize_ingredient(ing_name),
                i,
            )

    # Replace steps if present
    if "steps" in body:
        await db.execute("DELETE FROM recipe_steps WHERE recipe_id=$1", recipe_id)
        for i, step in enumerate(body.get("steps") or []):
            step_text = (step.get("text") or "").strip()
            if not step_text:
                continue
            await db.execute(
                "INSERT INTO recipe_steps (recipe_id, step_number, text) VALUES ($1,$2,$3)",
                recipe_id, i + 1, step_text,
            )

    # If ingredients changed, resync shopping for every event using this recipe
    if "ingredients" in body:
        evt_rows = await db.fetch(
            "SELECT event_id FROM event_recipes WHERE recipe_id=$1", recipe_id
        )
        for er in evt_rows:
            await _resync_shopping_if_exists(er["event_id"], db)

    return {"id": recipe_id, "ok": True}


# ── POST /api/recipes/{id}/normalize-ingredients ─────────────────────────────
# Re-runs the LLM normalizer over the recipe's current ingredient names —
# useful for recipes imported before normalization existed (raw "500г свинины"
# strings with no qty/unit). Owner-only.

@app.post("/api/recipes/{recipe_id}/normalize-ingredients")
async def normalize_recipe_ingredients(
    recipe_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    # AI consent check
    try:
        await LegalConsentService.require_ai_access(db, user_id)
    except ValueError:
        raise HTTPException(403, "ai_consent_required")

    rec = await db.fetchrow("SELECT user_id FROM recipes WHERE id=$1", recipe_id)
    if not rec:
        raise HTTPException(404, "Recipe not found")
    if rec["user_id"] != user_id:
        raise HTTPException(403, "Access denied")

    ings = await db.fetch(
        "SELECT name FROM ingredients WHERE recipe_id=$1 ORDER BY sort_order, id", recipe_id
    )
    raw = [i["name"] for i in ings if (i["name"] or "").strip()]
    if not raw:
        return {"updated": 0}

    normalized = await _llm_normalize_ingredients(raw)

    await db.execute("DELETE FROM ingredients WHERE recipe_id=$1", recipe_id)
    for idx, ing in enumerate(normalized):
        ing_name = (ing.get("name") or "").strip()
        if not ing_name:
            continue
        await db.execute(
            "INSERT INTO ingredients (recipe_id, name, qty, unit, category, sort_order) VALUES ($1,$2,$3,$4,$5,$6)",
            recipe_id, ing_name, ing.get("qty"),
            (ing.get("unit") or "").strip(),
            ing.get("category") or categorize_ingredient(ing_name),
            idx,
        )

    # Keep shopping lists in sync for events using this recipe
    evt_rows = await db.fetch("SELECT event_id FROM event_recipes WHERE recipe_id=$1", recipe_id)
    for er in evt_rows:
        await _resync_shopping_if_exists(er["event_id"], db)

    return {"updated": len(normalized)}


# ── GET /api/recipes/{id} ─────────────────────────────────────────────────────

@app.get("/api/recipes/{recipe_id}")
async def get_recipe(recipe_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    rec = await db.fetchrow("SELECT * FROM recipes WHERE id=$1", recipe_id)
    if not rec:
        raise HTTPException(404, "Recipe not found")

    # Access: recipe owner OR collaborator in any event that contains this recipe
    if rec["user_id"] != user_id:
        has_access = await db.fetchval(
            """
            SELECT 1 FROM event_recipes er
            JOIN collaborators c ON c.event_id = er.event_id
            WHERE er.recipe_id = $1 AND c.telegram_user_id = $2
            LIMIT 1
            """,
            recipe_id, user_id,
        )
        if not has_access:
            raise HTTPException(403, "Access denied")

    ingredients = await db.fetch(
        "SELECT * FROM ingredients WHERE recipe_id=$1 ORDER BY sort_order, id", recipe_id
    )
    steps = await db.fetch(
        "SELECT * FROM recipe_steps WHERE recipe_id=$1 ORDER BY step_number", recipe_id
    )

    rec_dict = dict(rec)
    cook_time = rec_dict.get("cook_time_minutes") or rec_dict.get("cook_time_min")

    return {
        "id": rec["id"],
        "user_id": rec["user_id"],
        "name": rec["name"],
        "name_original": rec_dict.get("name_original"),
        "emoji": rec["emoji"] or "🍽",
        "servings": rec["servings"],
        "cook_time_minutes": cook_time,
        "cook_time_min": cook_time,   # compat
        "source_url": rec_dict.get("source_url"),
        "source_type": rec_dict.get("source_type") or "manual",
        "source_photo_file_id": rec_dict.get("source_photo_file_id"),
        "category": rec_dict.get("category"),
        "tags": list(rec_dict.get("tags") or []),
        "times_cooked": rec_dict.get("times_cooked") or 0,
        "rating": rec_dict.get("rating"),
        "notes": rec_dict.get("notes"),
        "created_at": rec["created_at"].isoformat() if rec["created_at"] else None,
        "ingredients": [
            {
                "id": i["id"], "name": i["name"],
                "qty": i["qty"], "unit": i["unit"] or "",
                "category": i["category"] or "прочее",
            }
            for i in ingredients
        ],
        "steps": [
            {"step_number": s["step_number"], "text": s["text"]}
            for s in steps
        ],
    }


# ── POST /api/recipes/import-url  (Mini App → import by URL) ─────────────────

@app.post("/api/recipes/import-url", status_code=201)
async def import_recipe_url(
    body: dict,
    user_id: int = Depends(get_current_user),
    db=Depends(get_db),
):
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")
    if not url.startswith("http"):
        raise HTTPException(400, "Invalid URL")
    try:
        recipe = await parse_and_save_recipe(user_id, url=url)
        return recipe
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        log.error("import-url error: %s", e)
        raise HTTPException(500, f"Parsing failed: {str(e)[:200]}")


# ── POST /api/recipes/import-text  (Mini App → import free text) ──────────────

@app.post("/api/recipes/import-text", status_code=201)
async def import_recipe_text(
    body: dict,
    user_id: int = Depends(get_current_user),
    db=Depends(get_db),
):
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    try:
        recipe = await parse_and_save_recipe(user_id, text=text)
        return recipe
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        log.error("import-text error: %s", e)
        raise HTTPException(500, f"Parsing failed: {str(e)[:200]}")


# ── DELETE /api/recipes/{id}  (remove from library entirely) ─────────────────

@app.delete("/api/recipes/{recipe_id}", status_code=204)
async def delete_recipe_from_library(
    recipe_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    rec = await db.fetchrow("SELECT user_id FROM recipes WHERE id=$1", recipe_id)
    if not rec:
        raise HTTPException(404, "Recipe not found")
    if rec["user_id"] != user_id:
        raise HTTPException(403, "Access denied")
    # CASCADE removes ingredients, recipe_steps, event_recipes links
    await db.execute("DELETE FROM recipes WHERE id=$1", recipe_id)


# ── Recipe share (inline prepared message) ────────────────────────────────────

@app.post("/api/recipes/{recipe_id}/prepare-share")
async def prepare_recipe_share(
    recipe_id: int,
    user_id: int = Depends(get_current_user),
    db=Depends(get_db),
):
    """Create a prepared inline message for sharing a recipe."""
    rec = await db.fetchrow("SELECT * FROM recipes WHERE id=$1", recipe_id)
    if not rec:
        raise HTTPException(404, "Recipe not found")
    if rec["user_id"] != user_id:
        raise HTTPException(403, "Access denied")

    # Fetch ingredients and steps for snapshot
    ings = await db.fetch(
        "SELECT name, qty, unit FROM ingredients WHERE recipe_id=$1 ORDER BY id", recipe_id
    )
    steps = await db.fetch(
        "SELECT step_number, text FROM recipe_steps WHERE recipe_id=$1 ORDER BY step_number", recipe_id
    )

    snapshot = {
        "name": rec["name"],
        "emoji": rec["emoji"] or "🍽",
        "category": rec.get("category"),
        "servings": rec.get("servings"),
        "cook_time_minutes": rec.get("cook_time_minutes"),
        "ingredients": [{"name": i["name"], "qty": i.get("qty"), "unit": i.get("unit")} for i in ings],
        "steps": [{"step_number": s["step_number"], "text": s["text"]} for s in steps],
    }

    # Build share text
    lines = [f"{snapshot['emoji']} <b>{snapshot['name']}</b>"]
    meta = []
    if snapshot.get("category"):
        meta.append(snapshot["category"])
    if snapshot.get("servings"):
        meta.append(f"🍽 {snapshot['servings']} порц.")
    if snapshot.get("cook_time_minutes"):
        meta.append(f"⏱ {snapshot['cook_time_minutes']} мин.")
    if meta:
        lines.append(" · ".join(meta))
    if ings:
        lines.append(f"\n🥄 Ингредиенты ({len(ings)}):")
        for i in ings:
            q = i.get("qty")
            if q and q != 0:
                q_str = str(int(q)) if q == int(q) else str(round(q, 2)).rstrip("0").rstrip(".")
                qty = f"{q_str} {i.get('unit') or ''}".strip()
            else:
                qty = ""
            lines.append(f"  • {i['name']}" + (f" — {qty}" if qty else ""))
    if steps:
        lines.append(f"\n📋 Приготовление:")
        for s in steps:
            lines.append(f"  {s['step_number']}. {s['text']}")
    lines.append("\n🌿 Рецепт из ПОЛЯНЫ")
    message_text = "\n".join(lines)

    # Create share record with token
    token = secrets.token_urlsafe(16)
    share = await db.fetchrow(
        "INSERT INTO recipe_shares (token, source_recipe_id, owner_user_id, snapshot) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        token, recipe_id, user_id, json.dumps(snapshot)
    )

    callback_data = f"rs:{token}"
    if len(callback_data.encode("utf-8")) > 64:
        raise HTTPException(500, "Share token too long")

    bot_username = await _get_bot_username()
    mini_app_url = f"https://t.me/{bot_username}?startapp=shared_{token}"

    from aiogram.types import (
        InlineQueryResultArticle, InputTextMessageContent,
        InlineKeyboardMarkup, InlineKeyboardButton,
    )

    result = InlineQueryResultArticle(
        id=str(share["id"]),
        title=snapshot["name"],
        description="Рецепт из ПОЛЯНЫ",
        input_message_content=InputTextMessageContent(
            message_text=message_text, parse_mode="HTML"
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💾 Сохранить себе", callback_data=callback_data)],
            [InlineKeyboardButton(text="🌿 Открыть в ПОЛЯНЕ", url=mini_app_url)],
        ]),
    )

    try:
        prepared = await bot.save_prepared_inline_message(
            user_id=user_id,
            result=result,
            allow_user_chats=True,
            allow_group_chats=True,
            allow_bot_chats=False,
            allow_channel_chats=False,
        )
        return {
            "prepared_message_id": prepared.id,
            "share_id": str(share["id"]),
        }
    except Exception as e:
        log.warning("save_prepared_inline_message failed: %s", e)
        # Fallback: return the inline result for manual use
        return {
            "prepared_message_id": None,
            "share_id": str(share["id"]),
            "fallback": True,
        }


# ── Share link & join ─────────────────────────────────────────────────────────

@app.post("/api/recipes/share/{token}/prepare-message")
async def prepare_share_message(
    token: str,
    user_id: int = Depends(get_current_user),
    db=Depends(get_db),
):
    """Prepare an inline message for sharing via shareMessage()."""
    share = await db.fetchrow(
        "SELECT * FROM recipe_shares WHERE token=$1 AND revoked_at IS NULL", token
    )
    if not share:
        raise HTTPException(404, "Share not found or expired")
    if share["owner_user_id"] != user_id:
        raise HTTPException(403, "Not your share")

    snap = share["snapshot"]
    if isinstance(snap, str):
        snap = json.loads(snap)

    # Build message text
    lines = [f"{snap.get('emoji', '🍽')} <b>{snap['name']}</b>"]
    meta = []
    if snap.get("category"):
        meta.append(snap["category"])
    if snap.get("servings"):
        meta.append(f"🍽 {snap['servings']} порц.")
    if snap.get("cook_time_minutes"):
        meta.append(f"⏱ {snap['cook_time_minutes']} мин.")
    if meta:
        lines.append(" · ".join(meta))
    for i in snap.get("ingredients", [])[:10]:
        q = i.get("qty")
        if q and q != 0:
            q_str = str(int(q)) if q == int(q) else str(round(q, 2)).rstrip("0").rstrip(".")
            qty = f"{q_str} {i.get('unit') or ''}".strip()
        else:
            qty = ""
        lines.append(f"  • {i['name']}" + (f" — {qty}" if qty else ""))
    if len(snap.get("ingredients", [])) > 10:
        lines.append(f"  … и ещё {len(snap['ingredients']) - 10}")
    lines.append("\n🌿 Рецепт из ПОЛЯНЫ")
    message_text = "\n".join(lines)

    bot_username = await _get_bot_username()
    mini_app_url = f"https://t.me/{bot_username}?startapp=shared_{token}"

    from aiogram.types import (
        InlineQueryResultArticle, InputTextMessageContent,
        InlineKeyboardMarkup as IKM, InlineKeyboardButton as IKB,
    )

    result = InlineQueryResultArticle(
        id=str(share["id"]),
        title=snap["name"],
        description="Рецепт из ПОЛЯНЫ",
        input_message_content=InputTextMessageContent(
            message_text=message_text, parse_mode="HTML"
        ),
        reply_markup=IKM(inline_keyboard=[
            [IKB(text="💾 Сохранить себе", callback_data=f"rs:{token}")],
            [IKB(text="🌿 Открыть в ПОЛЯНЕ", url=mini_app_url)],
        ]),
    )

    try:
        prepared = await bot.save_prepared_inline_message(
            user_id=user_id,
            result=result,
            allow_user_chats=True,
            allow_group_chats=True,
            allow_bot_chats=False,
            allow_channel_chats=False,
        )
        return {"prepared_message_id": prepared.id}
    except Exception as e:
        log.warning("save_prepared_inline_message failed: %s", e)
        raise HTTPException(502, f"Telegram API error: {e}")


@app.get("/api/events/{event_id}/share-link")
async def get_share_link(event_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    row = await db.fetchrow("SELECT id, name, event_date, telegram_user_id FROM events WHERE id=$1", event_id)
    if not row:
        raise HTTPException(404, "Not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if row["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")
    return {
        "share_link": f"https://t.me/reciptesbot?start=event_{event_id}",
        "event_name": row["name"],
        "event_date": row["event_date"].isoformat() if row["event_date"] else None,
    }


@app.post("/api/events/{event_id}/join")
async def join_event(event_id: int, body: dict, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Not found")
    was_new = not await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    await db.execute(
        """
        INSERT INTO collaborators (event_id, telegram_user_id, first_name, username, role)
        VALUES ($1,$2,$3,$4,'collaborator')
        ON CONFLICT (event_id, telegram_user_id) DO UPDATE SET first_name=EXCLUDED.first_name
        """,
        event_id, user_id, body.get("first_name", ""), body.get("username", ""),
    )
    if was_new and ev["telegram_user_id"] != user_id:
        await track(user_id, "guest_joined",
                    props={"event_id": event_id, "owner_id": ev["telegram_user_id"]},
                    event_ref=event_id)
    return {"status": "joined", "role": "collaborator"}


# ── Shopping list helpers ─────────────────────────────────────────────────────

def _fmt_qty(qty: float | None) -> str:
    """Format a float quantity to a clean string (1.5 → '1.5', 2.0 → '2')."""
    if qty is None or qty == 0:
        return ""
    if qty == int(qty):
        return str(int(qty))
    return f"{qty:.2f}".rstrip("0").rstrip(".")


# Unit canonicalization for merging the same product across recipes.
# dimension -> base unit: mass=граммы, vol=мл, count=шт.
_UNIT_CANON = {
    "г": ("mass", 1), "гр": ("mass", 1), "грамм": ("mass", 1), "граммов": ("mass", 1), "g": ("mass", 1),
    "кг": ("mass", 1000), "kg": ("mass", 1000), "килограмм": ("mass", 1000),
    "мл": ("vol", 1), "ml": ("vol", 1),
    "л": ("vol", 1000), "l": ("vol", 1000), "литр": ("vol", 1000), "литров": ("vol", 1000),
    "ст.л": ("vol", 15), "ст.л.": ("vol", 15), "стл": ("vol", 15), "ст. л": ("vol", 15), "ст ложка": ("vol", 15),
    "ч.л": ("vol", 5), "ч.л.": ("vol", 5), "чл": ("vol", 5), "ч. л": ("vol", 5),
    "стакан": ("vol", 200), "стакана": ("vol", 200),
    "шт": ("count", 1), "шт.": ("count", 1), "штук": ("count", 1), "штуки": ("count", 1),
}
_TASTE_UNITS = {"", "по вкусу", "щепотка", "щепотки", "щепоть", "на вкус"}


def _norm_name(name: str) -> str:
    """Grouping key for the same product (lowercase, whitespace-collapsed)."""
    return " ".join((name or "").lower().split())


def _merge_measures(entries: list) -> str:
    """entries: list of (qty_float, unit_str) for ONE product. Sum per dimension
    (mass→г/кг, vol→мл/л, count→шт), list unknown units separately, fold
    unquantified ('по вкусу') in. Returns one human display string."""
    mass = vol = count = 0.0
    raw: dict = {}
    taste = False
    for qty, unit in entries:
        u = (unit or "").strip().lower()
        q = qty or 0.0
        c = _UNIT_CANON.get(u)
        if c:
            dim, f = c
            if dim == "mass":
                mass += q * f
            elif dim == "vol":
                vol += q * f
            else:
                count += q * f
        elif u in _TASTE_UNITS:
            taste = True
        elif q > 0:
            key = (unit or "").strip()
            raw[key] = raw.get(key, 0.0) + q
        else:
            taste = True
    parts = []
    if mass > 0:
        parts.append(f"{_fmt_qty(mass / 1000)} кг" if mass >= 1000 else f"{_fmt_qty(mass)} г")
    if vol > 0:
        parts.append(f"{_fmt_qty(vol / 1000)} л" if vol >= 1000 else f"{_fmt_qty(vol)} мл")
    if count > 0:
        parts.append(f"{_fmt_qty(count)} шт")
    for u, q in raw.items():
        parts.append(f"{_fmt_qty(q)} {u}".strip())
    if not parts and taste:
        return "по вкусу"
    return " + ".join(parts)


CATEGORY_ORDER = [
    "мясо", "рыба", "овощи", "фрукты", "молочное", "яйца",
    "крупы", "мука", "масло", "соусы", "специи", "орехи",
    "сахар", "консервы", "хлеб", "грибы", "напитки", "прочее",
]


_last_gen_error: dict[int, str] = {}  # TEMP diagnostics: last generation error per event


async def _generate_shopping_list(event_id: int, db) -> int:
    """Aggregate ingredients from all event recipes into shopping_items.
    Deletes previously generated items, inserts fresh aggregated ones.
    Returns number of items generated."""

    _last_gen_error.pop(event_id, None)

    rows = await db.fetch(
        """
        SELECT i.name, i.qty, i.unit, i.category, er.servings_multiplier
        FROM event_recipes er
        JOIN ingredients i ON i.recipe_id = er.recipe_id
        WHERE er.event_id = $1
        """,
        event_id,
    )

    # Group by normalized product NAME (not name+unit), collecting every (qty,unit)
    # entry so the same product across recipes/units merges into one line.
    agg: dict = {}
    for row in rows:
        raw_name = (row["name"] or "").strip()
        if not raw_name:
            continue  # skip ingredients with empty/NULL name — never crash the list
        key = _norm_name(raw_name)
        try:
            mult = float(row["servings_multiplier"] or 1.0)
        except (TypeError, ValueError):
            mult = 1.0
        try:
            qty = (float(row["qty"]) if row["qty"] else 0.0) * mult
        except (TypeError, ValueError):
            qty = 0.0
        g = agg.get(key)
        if g is None:
            g = {"name": raw_name, "category": row["category"] or "прочее", "entries": []}
            agg[key] = g
        g["entries"].append((qty, (row["unit"] or "").strip()))

    # Preserve "bought" state across regeneration (key by lower name + unit)
    prev = await db.fetch(
        "SELECT name, unit, bought FROM shopping_items WHERE event_id=$1 AND is_generated=TRUE",
        event_id,
    )
    bought_state = {
        _norm_name(p["name"]): p["bought"]
        for p in prev
        if (p["name"] or "").strip()
    }

    # Remove previously generated items (keep manual ones)
    await db.execute(
        "DELETE FROM shopping_items WHERE event_id=$1 AND is_generated=TRUE", event_id
    )

    # Insert aggregated items — per-row guarded so one bad row can't wipe the list
    inserted = 0
    for key, item in agg.items():
        display_qty = _merge_measures(item["entries"]) or None
        was_bought = bought_state.get(key, False)
        try:
            await db.execute(
                """
                INSERT INTO shopping_items (event_id, name, quantity, qty, unit, category, is_generated, bought)
                VALUES ($1,$2,$3,$4,$5,$6,TRUE,$7)
                """,
                event_id, item["name"], display_qty, None, "", item["category"], was_bought,
            )
            inserted += 1
        except asyncpg.UndefinedColumnError:
            # Older schema — insert what the base table guarantees
            await db.execute(
                "INSERT INTO shopping_items (event_id, name, quantity, bought) VALUES ($1,$2,$3,$4)",
                event_id, item["name"], display_qty, was_bought,
            )
            inserted += 1
        except Exception as e:
            log.exception("shopping insert failed for event %s item %r", event_id, item.get("name"))
            _last_gen_error[event_id] = f"{type(e).__name__}: {e}"
            continue
    log.info("shopping generated for event %s: %s/%s items", event_id, inserted, len(agg))

    return inserted


async def _resync_shopping_if_exists(event_id: int, db) -> None:
    """Regenerate the shopping list, but only if one was already generated for
    this event — so adding/removing a recipe keeps an existing list in sync
    without building one for events the user never opened shopping for."""
    has_generated = await db.fetchval(
        "SELECT 1 FROM shopping_items WHERE event_id=$1 AND is_generated=TRUE LIMIT 1", event_id
    )
    if has_generated:
        await _generate_shopping_list(event_id, db)


# ── GET /api/events/{id}/shopping ─────────────────────────────────────────────

@app.get("/api/events/{event_id}/shopping")
async def get_shopping_list(
    event_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if ev["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")

    # Auto-generate if no generated items exist yet
    has_generated = await db.fetchval(
        "SELECT 1 FROM shopping_items WHERE event_id=$1 AND is_generated=TRUE LIMIT 1", event_id
    )
    if not has_generated:
        try:
            await _generate_shopping_list(event_id, db)
        except Exception as e:
            # Never let generation failure blank the whole shopping screen —
            # log the real cause and fall through to whatever items exist.
            log.exception("shopping auto-generate failed for event %s", event_id)
            _last_gen_error[event_id] = f"{type(e).__name__}: {e}"

    items = await db.fetch(
        "SELECT * FROM shopping_items WHERE event_id=$1 ORDER BY category, name", event_id
    )
    total = len(items)
    bought_count = sum(1 for i in items if i["bought"])

    # Group by category
    grouped: dict[str, list] = {}
    for item in items:
        cat = item["category"] or "прочее"
        grouped.setdefault(cat, []).append({
            "id": item["id"],
            "name": item["name"],
            "qty": item["qty"],
            "unit": item["unit"] or "",
            "quantity": item["quantity"] or "",
            "category": cat,
            "bought": bool(item["bought"]),
            "is_generated": bool(item["is_generated"]),
        })

    # Sort categories by known order
    def cat_sort(cat):
        try:
            return CATEGORY_ORDER.index(cat)
        except ValueError:
            return 99

    categories = [
        {"name": cat, "items": grouped[cat]}
        for cat in sorted(grouped.keys(), key=cat_sort)
    ]

    # Diagnostics so the UI can explain an empty list (no recipes vs no ingredients)
    linked_recipes = await db.fetchval(
        "SELECT COUNT(*) FROM event_recipes WHERE event_id=$1", event_id
    ) or 0
    ingredient_rows = await db.fetchval(
        """
        SELECT COUNT(*) FROM event_recipes er
        JOIN ingredients i ON i.recipe_id = er.recipe_id
        WHERE er.event_id=$1 AND COALESCE(TRIM(i.name),'') <> ''
        """,
        event_id,
    ) or 0

    return {
        "items": categories, "total": total, "bought": bought_count,
        "linked_recipes": linked_recipes, "ingredient_rows": ingredient_rows,
        "debug_gen_error": _last_gen_error.get(event_id),
    }


# ── POST /api/events/{id}/shopping/sync ───────────────────────────────────────

@app.post("/api/events/{event_id}/shopping/sync")
async def sync_shopping_list(
    event_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if ev["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")

    count = await _generate_shopping_list(event_id, db)
    return {"generated": count}


# ── POST /api/events/{id}/shopping  (manual add) ─────────────────────────────

@app.post("/api/events/{event_id}/shopping", status_code=201)
async def add_manual_shopping_item(
    event_id: int, body: dict,
    user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if ev["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")

    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    qty_str = (body.get("quantity") or "").strip() or None

    try:
        row = await db.fetchrow(
            """
            INSERT INTO shopping_items (event_id, name, quantity, is_generated, bought, added_by)
            VALUES ($1, $2, $3, FALSE, FALSE, $4)
            RETURNING *
            """,
            event_id, name, qty_str, user_id,
        )
    except asyncpg.UndefinedColumnError:
        # Older schema may be missing extended columns — fall back to minimal insert
        log.warning("shopping_items missing extended columns; minimal insert for event %s", event_id)
        row = await db.fetchrow(
            "INSERT INTO shopping_items (event_id, name, quantity, bought) VALUES ($1,$2,$3,FALSE) RETURNING *",
            event_id, name, qty_str,
        )
    except Exception as e:
        log.exception("manual shopping add failed for event %s", event_id)
        raise HTTPException(500, f"add failed: {type(e).__name__}: {e}")

    return {"id": row["id"], "name": row["name"], "quantity": row["quantity"],
            "bought": row["bought"], "is_generated": False}


# ── DELETE /api/events/{id}/shopping/{item_id} ────────────────────────────────

@app.delete("/api/events/{event_id}/shopping/{item_id}", status_code=204)
async def delete_shopping_item(
    event_id: int, item_id: int,
    user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if ev["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")
    await db.execute(
        "DELETE FROM shopping_items WHERE id=$1 AND event_id=$2", item_id, event_id
    )


# ── PATCH /api/events/{id}/shopping/{item_id} ────────────────────────────────

@app.patch("/api/events/{event_id}/shopping/{item_id}")
async def toggle_shopping_item(
    event_id: int, item_id: int, body: dict,
    user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if ev["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")

    bought = bool(body.get("bought", False))
    await db.execute(
        "UPDATE shopping_items SET bought=$1 WHERE id=$2 AND event_id=$3",
        bought, item_id, event_id,
    )
    return {"id": item_id, "bought": bought}


# ── Balance / ledger ──────────────────────────────────────────────────────────

async def _get_balance(db, uid: int) -> int:
    return await db.fetchval(
        "SELECT balance FROM user_balance WHERE telegram_user_id=$1", uid
    ) or 0


async def _credit(db, uid: int, amount: int, kind: str, ref: str | None = None,
                  meta: dict | None = None) -> int:
    """Add funds. Idempotent when `ref` is given (unique on kind+ref)."""
    meta_json = json.dumps(meta) if meta else None
    try:
        async with db.transaction():
            row = await db.fetchrow(
                """
                INSERT INTO user_balance (telegram_user_id, balance) VALUES ($1,$2)
                ON CONFLICT (telegram_user_id)
                DO UPDATE SET balance = user_balance.balance + $2, updated_at = NOW()
                RETURNING balance
                """,
                uid, amount,
            )
            bal = row["balance"]
            await db.execute(
                "INSERT INTO payment_txns (telegram_user_id, kind, amount, balance_after, ref, meta) "
                "VALUES ($1,$2,$3,$4,$5,$6)",
                uid, kind, amount, bal, ref, meta_json,
            )
            return bal
    except asyncpg.UniqueViolationError:
        # Already processed (duplicate ref) — transaction rolled back, no double credit
        return await _get_balance(db, uid)


async def _debit(db, uid: int, amount: int, kind: str, meta: dict | None = None) -> tuple[int | None, int | None]:
    """Subtract funds atomically. Returns (new_balance, txn_id), or (None, None)."""
    meta_json = json.dumps(meta) if meta else None
    async with db.transaction():
        bal = await db.fetchval(
            "SELECT balance FROM user_balance WHERE telegram_user_id=$1 FOR UPDATE", uid
        ) or 0
        if bal < amount:
            return None, None
        new_bal = bal - amount
        await db.execute(
            "UPDATE user_balance SET balance=$2, updated_at=NOW() WHERE telegram_user_id=$1",
            uid, new_bal,
        )
        txn_id = await db.fetchval(
            "INSERT INTO payment_txns (telegram_user_id, kind, amount, balance_after, meta) "
            "VALUES ($1,$2,$3,$4,$5) RETURNING id",
            uid, kind, -amount, new_bal, meta_json,
        )
        return new_bal, txn_id


async def _accrue_referral_bonus(db, referee_id: int, spend: int, source_ref: str) -> None:
    """If the referee was referred, schedule a matured-in-24h bonus for the referrer.
    Now uses ReferralService for proper pending/bonus separation."""
    # Use the new service for proper referral reward processing
    await ReferralService.process_successful_payment(
        db, payment_id=source_ref, user_id=referee_id,
        cash_amount_minor=spend,
        metadata={"legacy_call": True}
    )


async def _referral_maturation_loop():
    """Activate matured referral rewards. Runs every 10 minutes.
    Uses the new WalletService for proper pending→bonus transition."""
    while True:
        try:
            if pool is not None:
                async with pool.acquire() as db:
                    # Find users with matured pending rewards
                    rows = await db.fetch(
                        "SELECT DISTINCT referrer_user_id FROM referral_rewards "
                        "WHERE status='pending' AND available_at <= NOW() LIMIT 100"
                    )
                    for r in rows:
                        uid = r["referrer_user_id"]
                        count = await WalletService.activate_pending_rewards(db, uid)
                        if count > 0:
                            # Get total activated points for notification
                            total = await db.fetchval(
                                "SELECT SUM(reward_points) FROM referral_rewards "
                                "WHERE referrer_user_id=$1 AND status='available' "
                                "AND activated_at >= NOW() - INTERVAL '1 minute'",
                                uid
                            ) or 0
                            try:
                                await bot.send_message(
                                    uid,
                                    f"✅ {total} бонусных баллов доступны\n\n"
                                    "Используйте их для распознавания рецептов "
                                    "и других ИИ-функций.")
                            except Exception:
                                pass
        except Exception:
            log.exception("referral maturation loop error")
        await asyncio.sleep(600)


@app.get("/api/balance")
async def get_balance_endpoint(user_id: int = Depends(get_current_user), db=Depends(get_db)):
    bal = await _get_balance(db, user_id)
    return {"balance": bal, "balance_rub": round(bal / 100, 2)}


_bot_username: str | None = None


async def _get_bot_username() -> str:
    global _bot_username
    if _bot_username is None:
        try:
            me = await bot.get_me()
            _bot_username = me.username or ""
        except Exception:
            _bot_username = ""
    return _bot_username


@app.get("/api/referrals/me")
async def referral_info(user_id: int = Depends(get_current_user), db=Depends(get_db)):
    return await ReferralService.get_dashboard(db, user_id)


@app.get("/api/referrals/history")
async def referral_history(
    user_id: int = Depends(get_current_user), db=Depends(get_db),
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
):
    return {"history": await ReferralService.get_history(db, user_id, limit, offset)}


@app.get("/api/wallet/me")
async def wallet_info(user_id: int = Depends(get_current_user), db=Depends(get_db)):
    return await WalletService.get_balance(db, user_id)


# Legacy endpoint for backward compat
@app.get("/api/referral")
async def referral_info_legacy(user_id: int = Depends(get_current_user), db=Depends(get_db)):
    return await ReferralService.get_dashboard(db, user_id)


# ── YooKassa top-up ───────────────────────────────────────────────────────────

_TOPUP_AMOUNTS = {100, 200, 500, 1000}   # rubles (minimum top-up 100 ₽)


@app.post("/api/balance/topup")
async def create_topup(body: dict, user_id: int = Depends(get_current_user)):
    """Create YooKassa payment for external site."""
    if not FEATURE_PAYMENTS_YOOKASSA_WEB:
        raise HTTPException(410, "ЮKassa оплата временно отключена")
    package_code = body.get("package_code", "")
    if not package_code:
        raise HTTPException(400, "package_code required")
    try:
        async with pool.acquire() as db:
            order = await PaymentService.create_order(db, user_id, package_code, "yookassa")
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Create YooKassa payment
    try:
        import base64
        auth = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.yookassa.ru/v3/payments",
                headers={
                    "Authorization": f"Basic {auth}",
                    "Idempotence-Key": order["order_id"],
                    "Content-Type": "application/json",
                },
                json={
                    "amount": {"value": str(order["amount"] / 100), "currency": "RUB"},
                    "confirmation": {"type": "redirect", "return_url": f"{FRONTEND_URL}/topup/status"},
                    "capture": True,
                    "description": f"ПОЛЯНА — {order['title']} ({order['total_points']} AI-баллов)",
                    "metadata": {"internal_order_id": order["order_id"], "package_code": package_code},
                })
        data = r.json()
        if "confirmation" in data:
            return {
                "ok": True,
                "confirmation_url": data["confirmation"]["confirmation_url"],
                "order_id": order["order_id"],
            }
        else:
            raise HTTPException(502, "Не удалось создать платёж")
    except HTTPException:
        raise
    except Exception as e:
        log.exception("YooKassa payment creation failed")
        raise HTTPException(502, f"Ошибка создания платежа: {type(e).__name__}")


@app.post("/api/balance/topup-stars")
async def create_topup_stars(body: dict, user_id: int = Depends(get_current_user)):
    """Create Telegram Stars invoice."""
    if not FEATURE_PAYMENTS_STARS:
        raise HTTPException(410, "Stars оплата временно отключена")
    package_code = body.get("package_code", "")
    if not package_code:
        raise HTTPException(400, "package_code required")
    try:
        async with pool.acquire() as db:
            order = await PaymentService.create_order(db, user_id, package_code, "telegram_stars")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "ok": True,
        "invoice_payload": order["invoice_payload"],
        "title": order["title"],
        "description": f"{order['total_points']} AI-баллов",
        "currency": "XTR",
        "amount": order["amount"],
        "order_id": order["order_id"],
    }


@app.post("/api/yookassa/webhook")
async def yookassa_webhook(body: dict, db=Depends(get_db)):
    """YooKassa server-to-server notification. We DO NOT trust the body — we
    re-fetch the authoritative payment from the API before crediting, so a
    forged webhook can't credit anyone."""
    obj = body.get("object") or {}
    pid = obj.get("id")
    if not pid or not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        return {"ok": True}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"https://api.yookassa.ru/v3/payments/{pid}",
                auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            )
        real = r.json()
    except Exception:
        log.exception("yookassa verify failed for %s", pid)
        return {"ok": True}

    if real.get("status") != "succeeded":
        return {"ok": True}
    try:
        uid = int((real.get("metadata") or {}).get("user_id") or 0)
        kopecks = int(round(float(real["amount"]["value"]) * 100))
    except Exception:
        return {"ok": True}
    if uid and kopecks > 0:
        new_bal = await _credit(db, uid, kopecks, "topup_yookassa", ref=pid,
                                meta={"amount_rub": real["amount"]["value"]})
        # Also credit to new wallets system
        await WalletService.credit_paid_points(
            db, uid, kopecks // POINTS_PER_RUBLE,
            reference_type="topup_yookassa", reference_id=pid,
            idempotency_key=f"yookassa:{pid}",
            metadata={"amount_rub": real["amount"]["value"]}
        )
        await track(uid, "payment_succeeded",
                    props={"kopecks": kopecks, "method": "yookassa", "ref": pid})
        # Process referral reward
        reward = await ReferralService.process_successful_payment(
            db, payment_id=pid, user_id=uid,
            cash_amount_minor=kopecks,
            metadata={"method": "yookassa", "amount_rub": real["amount"]["value"]}
        )
        try:
            msg = f"✅ Баланс пополнен на {int(kopecks/100)} ₽.\nТекущий баланс: {int(new_bal/100)} ₽"
            if reward:
                msg += f"\n\n🎁 Реферальный бонус: +{reward} баллов начислен вашему пригласившему"
            await bot.send_message(uid, msg)
        except Exception:
            pass
    return {"ok": True}


# ── Invitation image generation ───────────────────────────────────────────────

_RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_RU_WDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _fmt_event_dt(dt) -> tuple[str | None, str | None]:
    if not dt:
        return (None, None)
    try:
        date_str = f"{_RU_WDAYS[dt.weekday()]}, {dt.day} {_RU_MONTHS[dt.month - 1]}"
        time_str = f"{dt.hour:02d}:{dt.minute:02d}"
        return date_str, time_str
    except Exception:
        return (str(dt)[:16], None)


async def _compose_invite_scene(name, date_str, time_str, place, dishes) -> tuple[str | None, str | None]:
    """Let a cheap text model analyze the event (season/time/place/format + dishes)
    and produce a tailored image scene prompt + a matching palette theme key."""
    try:
        client = _get_or_client()
    except Exception:
        return None, None
    themes = "|".join(invite.THEMES.keys())
    dishes_str = ", ".join(dishes[:8]) if dishes else "(не указаны)"
    prompt = (
        "Ты — арт-директор. По данным о событии придумай ФОН для вертикального "
        "приглашения-постера. Проанализируй сезон и время суток (по дате/времени), "
        "место, формат события и блюда. Верни строго JSON:\n"
        '{"theme":"<одно из: ' + themes + '>",'
        '"scene":"<яркий промпт НА АНГЛИЙСКОМ: сцена с этими блюдами на столе и '
        'подходящим антуражем, фотореализм, мягкое боке, БЕЗ текста и букв>"}\n\n'
        f"Событие: {name}\n"
        f"Когда: {date_str or '?'} {time_str or ''}\n"
        f"Место: {place or '?'}\n"
        f"Блюда: {dishes_str}\n"
    )
    try:
        resp = await client.chat.completions.create(
            model="google/gemini-2.5-flash",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.6,
            max_tokens=400,
        )
        data = json.loads(resp.choices[0].message.content)
        theme = invite.theme_or_default((data.get("theme") or "").strip())
        scene = (data.get("scene") or "").strip() or None
        return theme, scene
    except Exception as e:
        log.warning("_compose_invite_scene failed: %s", e)
        return None, None


async def _alert_admin(text: str) -> None:
    """Send an operational alert to the admin chat (best-effort)."""
    if not ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(ADMIN_CHAT_ID, text)
    except Exception:
        log.exception("admin alert failed")


_low_balance_alerted = False


_last_403_ts = 0.0

async def _openrouter_remaining_usd() -> float | None:
    """Remaining OpenRouter credit in USD, or None if unavailable."""
    global _last_403_ts
    if not OPENROUTER_KEY:
        return None
    if _last_403_ts and (time.time() - _last_403_ts) < 3600:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            _credits_url = (OPENROUTER_PROXY_URL.rstrip("/") + "/api/v1/credits") if OPENROUTER_PROXY_URL else "https://openrouter.ai/api/v1/credits"
            _h = {"Authorization": f"Bearer {OPENROUTER_KEY}"}
            if OPENROUTER_PROXY_SECRET:
                _h["X-Proxy-Secret"] = OPENROUTER_PROXY_SECRET
            r = await client.get(_credits_url, headers=_h)
        if r.status_code == 403:
            log.warning("OpenRouter credits: 403 from server IP, will retry in 1h")
            _last_403_ts = time.time()
            return None
        d = (r.json() or {}).get("data") or {}
        _last_403_ts = 0.0
        return float(d.get("total_credits", 0)) - float(d.get("total_usage", 0))
    except Exception:
        log.exception("openrouter credits check failed")
        return None
async def _openrouter_balance_loop():
    """Alert admin once when OpenRouter credit drops below threshold; reset on recovery."""
    global _low_balance_alerted
    while True:
        try:
            rem = await _openrouter_remaining_usd()
            if rem is not None:
                if rem < OPENROUTER_LOW_BALANCE_USD and not _low_balance_alerted:
                    _low_balance_alerted = True
                    await _alert_admin(
                        f"⚠️ OpenRouter: остаток ${rem:.2f} (< ${OPENROUTER_LOW_BALANCE_USD:.0f}). "
                        f"Пополни, пока генерация не встала: https://openrouter.ai/settings/credits"
                    )
                elif rem >= OPENROUTER_LOW_BALANCE_USD and _low_balance_alerted:
                    _low_balance_alerted = False  # recovered → re-arm
        except Exception:
            log.exception("openrouter balance loop error")
        await asyncio.sleep(1800)   # every 30 min


async def _payment_reconciliation_loop():
    """Reconcile pending payment orders with providers. Runs every 30 min."""
    while True:
        try:
            if pool is not None and FEATURE_PAYMENT_RECONCILIATION:
                async with pool.acquire() as db:
                    # Check YooKassa pending orders older than 5 min
                    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
                    pending = await db.fetch(
                        "SELECT * FROM payment_orders WHERE provider='yookassa' "
                        "AND status IN ('created','pending') AND created_at < $1 "
                        "LIMIT 50", cutoff)
                    for order in pending:
                        try:
                            ext_id = order["external_payment_id"]
                            if not ext_id:
                                continue
                            import base64
                            auth = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()
                            async with httpx.AsyncClient(timeout=30) as client:
                                r = await client.get(
                                    f"https://api.yookassa.ru/v3/payments/{ext_id}",
                                    headers={"Authorization": f"Basic {auth}"})
                            if r.status_code == 200:
                                data = r.json()
                                if data.get("status") == "succeeded":
                                    paid = await PaymentService.mark_order_paid(db, order["id"], ext_id)
                                    if paid:
                                        order_dict = dict(order)
                                        order_dict["external_payment_id"] = ext_id
                                        await WalletService.credit_purchase(db, order_dict)
                                        await ReferralService.process_successful_payment(
                                            db, payment_id=ext_id, user_id=order["user_id"],
                                            cash_amount_minor=order["amount"],
                                            metadata={"method": "yookassa_reconciliation"})
                                        log.info("Reconciled YooKassa order %s", order["id"])
                                elif data.get("status") in ("canceled", "cancelled"):
                                    await db.execute(
                                        "UPDATE payment_orders SET status='cancelled', cancelled_at=NOW() "
                                        "WHERE id=$1 AND status NOT IN ('succeeded','cancelled')",
                                        order["id"])
                        except Exception:
                            log.exception("YooKassa reconciliation failed for order %s", order["id"])

                    # Check Stars pending orders older than 10 min
                    stars_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
                    stars_pending = await db.fetch(
                        "SELECT * FROM payment_orders WHERE provider='telegram_stars' "
                        "AND status IN ('created','pending') AND created_at < $1 "
                        "LIMIT 50", stars_cutoff)
                    for order in stars_pending:
                        # Stars orders can only be verified via Telegram API (not trivial)
                        # Mark as expired if older than 1 hour
                        if order["created_at"] < datetime.now(timezone.utc) - timedelta(hours=1):
                            await db.execute(
                                "UPDATE payment_orders SET status='expired', cancelled_at=NOW() "
                                "WHERE id=$1 AND status IN ('created','pending')",
                                order["id"])
        except Exception:
            log.exception("payment reconciliation loop error")
        await asyncio.sleep(1800)   # every 30 min


async def _openrouter_background(scene_prompt: str) -> bytes:
    """Generate a vertical 9:16 1K background (no text) via gpt-5.4-image-2."""
    if not OPENROUTER_KEY:
        raise HTTPException(500, "OPENROUTER_API_KEY not set")
    prompt = (
        "Vertical 9:16 invitation poster background. " + scene_prompt + ". "
        "Keep the top third darker and uncluttered to leave room for overlay text. "
        "No text, no letters, no words, no captions. Photographic, high detail, soft bokeh."
    )
    payload = {
        "model": "openai/gpt-5.4-image-2",
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
        "image_config": {"aspect_ratio": "9:16", "image_size": "1K"},
        # Cap output so OpenRouter doesn't pre-reserve the full 65536-token budget
        # (~$2). One 1K 9:16 image is ~4k tokens; 12k gives headroom, reserves ~$0.36.
        "max_tokens": 12000,
    }
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
            json=payload,
        )
    data = r.json()
    err = data.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        low = str(msg).lower()
        # Provider-side credit/quota problems are OUR issue, not the user's —
        # never show the raw "add more credits / openrouter.ai" text to end users.
        if r.status_code == 402 or "credit" in low or "afford" in low or "quota" in low:
            log.error("OpenRouter out of credits/quota: %s", msg)
            await _alert_admin(
                "🚨 OpenRouter: КОНЧИЛИСЬ КРЕДИТЫ — генерация не работает, "
                "оплатившие пользователи заблокированы! Пополни: "
                "https://openrouter.ai/settings/credits"
            )
            raise HTTPException(503, f"Генерация временно недоступна. Напишите в поддержку {SUPPORT_HANDLE}")
        log.error("OpenRouter image error: %s", msg)
        raise HTTPException(502, f"Не удалось сгенерировать фон, попробуйте ещё раз. Если повторяется — {SUPPORT_HANDLE}")
    try:
        url = data["choices"][0]["message"]["images"][0]["image_url"]["url"]
        return base64.b64decode(url.split(",", 1)[1])
    except Exception:
        raise HTTPException(502, "Модель не вернула изображение")


@app.post("/api/events/{event_id}/invite")
async def make_invite_image(
    event_id: int, body: dict,
    user_id: int = Depends(get_current_user), db=Depends(get_db),
):
    ev = await db.fetchrow("SELECT * FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if ev["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")

    requested_theme = (body.get("theme") or "").strip()
    mode = (body.get("mode") or "free").strip()
    date_str, time_str = _fmt_event_dt(ev["event_date"])

    # Dishes linked to the event (shown in the menu block + fed to the AI scene)
    dish_rows = await db.fetch(
        """
        SELECT r.name FROM event_recipes er
        JOIN recipes r ON r.id = er.recipe_id
        WHERE er.event_id = $1
        ORDER BY er.added_at
        """,
        event_id,
    )
    dishes = [d["name"] for d in dish_rows if d["name"]]

    evt = {
        "name": ev["name"],
        "date_str": date_str,
        "time_str": time_str,
        "place": ev["location"],
        "host_name": (body.get("host_name") or "").strip() or None,
        "dishes": dishes,
    }

    # Paid AI invitations removed — free typographic template only (no charge, no LLM).
    use_theme = invite.theme_or_default(requested_theme) if requested_theme else invite.DEFAULT_THEME
    png = invite.render_typographic(evt, use_theme)

    return {
        "image": "data:image/png;base64," + base64.b64encode(png).decode(),
        "mode": "free",
        "theme": use_theme,
    }


@app.get("/api/invite/themes")
async def list_invite_themes(user_id: int = Depends(get_current_user)):
    return [{"key": k, "title": k.capitalize()} for k in invite.THEMES.keys()]


@app.post("/api/events/{event_id}/invite/send")
async def send_invite_to_chat(
    event_id: int, body: dict,
    user_id: int = Depends(get_current_user), db=Depends(get_db),
):
    """Send a generated invitation image to the user's Telegram chat so they can
    forward it to guests (the Mini App can't share an image directly)."""
    ev = await db.fetchrow("SELECT name FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")

    data_url = (body.get("image") or "").strip()
    if "," in data_url:
        data_url = data_url.split(",", 1)[1]
    try:
        png = base64.b64decode(data_url)
    except Exception:
        raise HTTPException(400, "bad image")
    if not png:
        raise HTTPException(400, "empty image")

    try:
        await bot.send_photo(
            chat_id=user_id,
            photo=BufferedInputFile(png, filename="invite.png"),
            caption=f"🎉 Приглашение на «{ev['name']}» — перешлите гостям!",
        )
    except Exception as e:
        log.exception("send invite failed for user %s", user_id)
        raise HTTPException(502, f"Не удалось отправить: {type(e).__name__}")
    return {"ok": True}


# ── Legal Documents API ───────────────────────────────────────────────────────

@app.get("/api/legal/documents")
async def list_legal_documents(user_id: int = Depends(get_current_user)):
    """List all active legal documents."""
    async with pool.acquire() as db:
        rows = await db.fetch(
            "SELECT document_type, version, title, published_at, requires_acceptance "
            "FROM legal_documents WHERE is_active=TRUE ORDER BY document_type")
    return [{"type": r["document_type"], "version": r["version"],
             "title": r["title"], "published_at": r["published_at"].isoformat(),
             "requires_acceptance": r["requires_acceptance"]} for r in rows]


@app.get("/api/legal/documents/{document_type}")
async def get_legal_document(document_type: str, user_id: int = Depends(get_current_user)):
    """Get a specific legal document."""
    import legal_docs
    if document_type not in legal_docs.DOCUMENT_TYPES:
        raise HTTPException(404, "Document type not found")
    async with pool.acquire() as db:
        doc = await LegalConsentService.get_active_document(db, document_type)
    if not doc:
        raise HTTPException(404, "No active document")
    return {
        "type": doc["document_type"], "version": doc["version"],
        "title": doc["title"], "content": doc["content"],
        "content_hash": doc["content_hash"],
        "published_at": doc["published_at"].isoformat(),
        "requires_acceptance": doc["requires_acceptance"],
    }


@app.get("/api/legal/status")
async def get_legal_status(user_id: int = Depends(get_current_user)):
    """Get user's legal acceptance status."""
    async with pool.acquire() as db:
        status = await LegalConsentService.get_user_acceptance_status(db, user_id)
    return status


@app.post("/api/legal/accept")
async def accept_legal_document(body: dict, user_id: int = Depends(get_current_user)):
    """Accept a legal document."""
    doc_type = body.get("document_type", "")
    version = body.get("document_version", "1.0")
    if not doc_type:
        raise HTTPException(400, "document_type required")
    import legal_docs
    if doc_type not in legal_docs.DOCUMENT_TYPES:
        raise HTTPException(400, "Invalid document type")
    try:
        async with pool.acquire() as db:
            result = await LegalConsentService.accept_document(
                db, user_id, doc_type, version,
                {"source": "mini_app", "session_id": body.get("session_id")})
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/legal/revoke")
async def revoke_legal_document(body: dict, user_id: int = Depends(get_current_user)):
    """Revoke legal consent."""
    doc_type = body.get("document_type", "")
    if not doc_type:
        raise HTTPException(400, "document_type required")
    async with pool.acquire() as db:
        result = await LegalConsentService.revoke_document(db, user_id, doc_type)
    return result


# ── Onboarding API ────────────────────────────────────────────────────────────

@app.get("/api/onboarding/status")
async def get_onboarding_status(user_id: int = Depends(get_current_user)):
    """Get onboarding status."""
    async with pool.acquire() as db:
        user = await UserService.get_user(db, user_id)
    if not user:
        return {"status": "not_started", "step": 0}
    return {
        "status": user["onboarding_status"],
        "step": user["onboarding_step"],
        "version": user["onboarding_version"],
        "completed_at": user["onboarding_completed_at"].isoformat() if user["onboarding_completed_at"] else None,
    }


@app.post("/api/onboarding/step")
async def advance_onboarding_step(body: dict, user_id: int = Depends(get_current_user)):
    """Advance onboarding to next step."""
    step = body.get("step", 0)
    async with pool.acquire() as db:
        await UserService.update_onboarding_step(db, user_id, step, "in_progress")
    return {"ok": True, "step": step}


@app.post("/api/onboarding/skip")
async def skip_onboarding(user_id: int = Depends(get_current_user)):
    """Skip onboarding."""
    async with pool.acquire() as db:
        await UserService.update_onboarding_step(db, user_id, 0, "skipped")
        await UserService.complete_onboarding(db, user_id)
    return {"ok": True}


# ── Account API ───────────────────────────────────────────────────────────────

@app.post("/api/account/delete-request")
async def request_account_deletion(user_id: int = Depends(get_current_user)):
    """Request account deletion (requires confirmation)."""
    await asyncio.create_task(track(user_id, "account_deletion_requested"))
    return {"ok": True, "message": "Use /delete_me in bot for full deletion flow"}


@app.post("/api/account/export")
async def export_account_data(user_id: int = Depends(get_current_user)):
    """Export user data (GDPR/152-ФЗ compliance)."""
    async with pool.acquire() as db:
        user = await UserService.get_user(db, user_id)
        recipes = await db.fetch("SELECT name, emoji, category, servings, cook_time_minutes "
                                 "FROM recipes WHERE user_id=$1", user_id)
        events = await db.fetch("SELECT name, event_date, location FROM events WHERE telegram_user_id=$1",
                                user_id)
        status = await LegalConsentService.get_user_acceptance_status(db, user_id)
    return {
        "user": {
            "first_name": user["first_name"] if user else None,
            "username": user["username"] if user else None,
            "created_at": user["created_at"].isoformat() if user else None,
            "acquisition_source": user["acquisition_source"] if user else None,
        },
        "recipes_count": len(recipes),
        "events_count": len(events),
        "legal_status": status,
    }


# ── Bot ───────────────────────────────────────────────────────────────────────

# FSM states for voice recipe editing flow
class VoiceStates(StatesGroup):
    editing = State()   # User is typing a corrected transcript


# FSM states for onboarding flow
class OnboardingStates(StatesGroup):
    welcome = State()       # Screen 1: welcome
    how_to_save = State()   # Screen 2: how to save recipes
    library = State()       # Screen 3: library features
    planning = State()      # Screen 4: events & shopping
    free_vs_ai = State()    # Screen 5: free vs AI
    legal_pending = State() # Legal acceptance screen
    completed = State()     # Final screen


# FSM states for account deletion flow
class DeleteStates(StatesGroup):
    confirm1 = State()  # First confirmation
    confirm2 = State()  # Second confirmation ("Удалить навсегда")


# FSM states for AI consent flow
class AiConsentStates(StatesGroup):
    pending = State()  # Waiting for AI consent decision


# ── Consent Middleware ────────────────────────────────────────────────────────

# Handlers that do NOT require any consent
_PUBLIC_CALLBACKS = {
    "show_terms", "show_ref", "show_documents", "show_back",
    "ws_how_to_add", "ws_example", "ws_send_hint", "ws_ai_functions",
    "ws_get_points", "ws_help", "ws_back",
    "ob_start_tutorial", "ob_start_skip_to_legal",
    "balance_stars", "balance_back", "balance_history",
}
_PUBLIC_COMMANDS = {"start", "terms", "privacy", "documents", "help", "delete_me", "balance"}

# Callbacks that require basic consent
_BASIC_CALLBACKS = set()  # filled dynamically for most menu actions

# Handlers that require AI consent
_AI_HANDLERS = set()  # checked at runtime


class LegalConsentMiddleware:
    """aiogram outer middleware that checks legal consent before handler execution."""

    async def __call__(self, handler, event, data):
        user_id = None
        if hasattr(event, 'from_user') and event.from_user:
            user_id = event.from_user.id
        if not user_id or pool is None:
            return await handler(event, data)

        # Determine handler name
        handler_name = ""
        if hasattr(event, 'text') and event.text:
            if event.text.startswith("/"):
                handler_name = event.text.split()[0].lstrip("/").split("@")[0]
        if hasattr(event, 'data') and event.data:
            handler_name = event.data.split(":")[0] if ":" in event.data else event.data

        # Public handlers — always allowed
        if handler_name in _PUBLIC_COMMANDS or handler_name in _PUBLIC_CALLBACKS:
            return await handler(event, data)

        # Onboarding handlers — always allowed
        if handler_name.startswith("ob_"):
            return await handler(event, data)

        # Delete account handlers — always allowed
        if handler_name.startswith("del_"):
            return await handler(event, data)

        # Legal document view — always allowed
        if handler_name.startswith("legal_doc"):
            return await handler(event, data)

        # Check if user exists and onboarding is completed
        async with pool.acquire() as db:
            user = await UserService.get_user(db, user_id)
            if not user:
                return await handler(event, data)
            if user["onboarding_status"] not in ("completed", "skipped"):
                # User hasn't finished onboarding — route to onboarding
                # But only for non-onboarding handlers
                if not handler_name.startswith("ob_"):
                    await _start_onboarding_for_user(event, user_id)
                    return

            # Check basic consent for sensitive actions
            try:
                await LegalConsentService.require_basic_access(db, user_id)
            except ValueError:
                # Show consent prompt
                await _show_consent_prompt(event, user_id, "basic")
                return

        return await handler(event, data)


async def _start_onboarding_for_user(event, user_id: int):
    """Send the welcome screen to a user who hasn't completed onboarding."""
    async with pool.acquire() as db:
        user = await UserService.get_user(db, user_id)
    if not user:
        return
    source = user.get("acquisition_source") or "organic"
    referrer_name = None
    if source == "referral" and user.get("referrer_user_id"):
        async with pool.acquire() as db:
            ref_user = await db.fetchrow(
                "SELECT first_name FROM users WHERE telegram_user_id=$1",
                user["referrer_user_id"])
        if ref_user:
            referrer_name = ref_user["first_name"]

    # Build welcome text based on source
    first_name = user.get("first_name") or ""
    if source == "referral" and referrer_name:
        text = WelcomeService.build_referral_welcome(first_name, referrer_name, WELCOME_POINTS)
    else:
        text = WelcomeService.build_new_user_welcome(first_name, source, referrer_name, WELCOME_POINTS)

    kb = _welcome_keyboard()
    if hasattr(event, 'message') and event.message:
        try:
            await event.message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    if hasattr(event, 'message') and event.message:
        await event.message.answer(text, reply_markup=kb)
    elif hasattr(event, 'text'):
        await event.answer(text, reply_markup=kb)


async def _show_consent_prompt(event, user_id: int, consent_type: str):
    """Show consent acceptance prompt."""
    if consent_type == "basic":
        text = (
            "Чтобы пользоваться ПОЛЯНОЙ, ознакомьтесь с документами.\n\n"
            "Необходимо принять:\n"
            "• Пользовательское соглашение\n"
            "• Согласие на обработку персональных данных"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Открыть документы", callback_data="show_documents")],
            [InlineKeyboardButton(text="✅ Принять соглашение", callback_data="ob_accept:terms")],
            [InlineKeyboardButton(text="✅ Дать согласие на данные", callback_data="ob_accept:personal_data_consent")],
        ])
    else:
        return

    if hasattr(event, 'message') and event.message:
        await event.message.answer(text, reply_markup=kb)
    elif hasattr(event, 'text'):
        await event.answer(text, reply_markup=kb)


# Onboarding screen texts
_ONBOARDING_SCREENS = [
    # Screen 0: Step 1/4 — Send a recipe
    {
        "text": (
            "📥 <b>Шаг 1 из 4. Отправьте рецепт</b>\n\n"
            "Ничего специально заполнять не нужно.\n\n"
            "Отправьте боту:\n"
            "📷 фото или скриншот\n"
            "🔗 ссылку\n"
            "📝 текст\n"
            "🎙 голосовое сообщение\n\n"
            "ПОЛЯНА сама определит формат и начнёт обработку."
        ),
        "buttons": [["Дальше"]],
        "skip": True,
    },
    # Screen 1: Step 2/4 — Recipe saved to library
    {
        "text": (
            "📚 <b>Шаг 2 из 4. Рецепт сохранится в библиотеке</b>\n\n"
            "Бот выделит:\n\n"
            "— название\n"
            "— ингредиенты\n"
            "— количество порций\n"
            "— время приготовления\n"
            "— пошаговую инструкцию\n\n"
            "После сохранения рецепт можно открыть,\n"
            "исправить или отправить другу."
        ),
        "buttons": [["Дальше"]],
        "skip": True,
    },
    # Screen 2: Step 3/4 — Use accumulated recipes
    {
        "text": (
            "🍽 <b>Шаг 3 из 4. Используйте накопленные рецепты</b>\n\n"
            "Когда в библиотеке появятся любимые блюда, вы сможете:\n\n"
            "— быстро находить их\n"
            "— пересчитывать ингредиенты\n"
            "— составлять меню для гостей\n"
            "— получать единый список покупок\n"
            "— распределять покупки и расходы"
        ),
        "buttons": [["Дальше"]],
        "skip": True,
    },
    # Screen 3: Step 4/4 — AI paid only when needed
    {
        "text": (
            "✨ <b>Шаг 4 из 4. ИИ оплачивается только когда нужен</b>\n\n"
            "Основные функции ПОЛЯНЫ бесплатны.\n\n"
            "AI-баллы используются для распознавания,\n"
            "генерации рецептов, изображений и умных рекомендаций.\n\n"
            "Обязательной подписки нет:\n"
            "можно купить пакет баллов или получить их,\n"
            "приглашая друзей.\n\n"
            "Теперь отправьте первый рецепт прямо в этот чат."
        ),
        "buttons": [["📎 Начать с рецепта"], ["🌿 Открыть библиотеку"]],
        "skip": False,
    },
    # Screen 4: Legal pending
    {
        "text": (
            "📋 <b>Документы</b>\n\n"
            "Чтобы пользоваться ПОЛЯНОЙ, ознакомьтесь с документами.\n\n"
            "{status_text}"
        ),
        "buttons": [],  # dynamic
        "skip": False,
    },
    # Screen 5: Completed
    {
        "text": (
            "Всё готово 🌿\n\n"
            "Отправьте сюда первый рецепт или откройте библиотеку."
        ),
        "buttons": [
            ["📷 Отправить рецепт"],
            ["📚 Открыть библиотеку"],
        ],
        "skip": False,
    },
]

# Total onboarding steps: 0-3 (tutorial), 4 (legal), 5 (completed)
_ONB_STEPS_TUTORIAL = 4  # indices 0..3
_ONB_STEP_LEGAL = 4
_ONB_STEP_COMPLETED = 5


async def _send_onboarding_step(event, user_id: int, step: int,
                                 source: str = "organic", referrer_name: str = None):
    """Send or edit onboarding screen."""
    if step >= len(_ONBOARDING_SCREENS):
        step = 0

    screen = _ONBOARDING_SCREENS[step]
    text = screen["text"]

    # Legal pending screen — show acceptance status
    if step == _ONB_STEP_LEGAL:
        async with pool.acquire() as db:
            status = await LegalConsentService.get_user_acceptance_status(db, user_id)
        terms_ok = "✅" if status.get("terms", {}).get("accepted") else "⬜"
        pdn_ok = "✅" if status.get("personal_data_consent", {}).get("accepted") else "⬜"
        status_text = f"{terms_ok} Пользовательское соглашение\n{pdn_ok} Обработка персональных данных"
        text = text.replace("{status_text}", status_text)

    # Build keyboard
    buttons = []
    if step == _ONB_STEP_LEGAL:
        # Legal screen — dynamic buttons
        async with pool.acquire() as db:
            status = await LegalConsentService.get_user_acceptance_status(db, user_id)
        if not status.get("terms", {}).get("accepted"):
            buttons.append([InlineKeyboardButton(text="✅ Принять соглашение",
                                                  callback_data="ob_accept:terms")])
        if not status.get("personal_data_consent", {}).get("accepted"):
            buttons.append([InlineKeyboardButton(text="✅ Дать согласие на данные",
                                                  callback_data="ob_accept:personal_data_consent")])
        buttons.append([InlineKeyboardButton(text="📋 Открыть документы",
                                              callback_data="show_documents")])
        all_ok = (status.get("terms", {}).get("accepted") and
                  status.get("personal_data_consent", {}).get("accepted"))
        if all_ok:
            buttons.append([InlineKeyboardButton(text="Продолжить",
                                                  callback_data=f"ob_next:{step}")])
    elif step == _ONB_STEP_COMPLETED:
        buttons.append([InlineKeyboardButton(text="📷 Отправить рецепт",
                                              callback_data="ob_action:send_recipe")])
        buttons.append([InlineKeyboardButton(text="📚 Открыть библиотеку",
                                              callback_data="ob_action:open_library")])
    else:
        # Tutorial screens 0-3
        nav_row = []
        if step > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ob_back:{step}"))
        # Step 3 has two action buttons instead of one "Дальше"
        if step == _ONB_STEPS_TUTORIAL - 1:
            nav_row.append(InlineKeyboardButton(text="📎 Начать с рецепта",
                                                 callback_data="ob_action:start_with_recipe"))
            buttons.append(nav_row)
            buttons.append([InlineKeyboardButton(text="🌿 Открыть библиотеку",
                                                  callback_data="ob_action:open_library")])
        else:
            nav_row.append(InlineKeyboardButton(text=screen["buttons"][0][0],
                                                 callback_data=f"ob_next:{step}"))
            buttons.append(nav_row)
        if screen.get("skip"):
            buttons.append([InlineKeyboardButton(text="Пропустить знакомство",
                                                  callback_data="ob_skip")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    # Send or edit
    if hasattr(event, 'message') and event.message and hasattr(event.message, 'edit_text'):
        try:
            await event.message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    if hasattr(event, 'message') and event.message:
        await event.message.answer(text, reply_markup=kb)
    elif hasattr(event, 'text'):
        await event.answer(text, reply_markup=kb)
        await event.answer(text, reply_markup=kb)


# ── Onboarding Callback Handlers ─────────────────────────────────────────────

@dp.callback_query(F.data.startswith("ob_next:"))
async def cb_ob_next(callback: CallbackQuery):
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    step = int(callback.data.split(":")[1]) + 1
    uid = callback.from_user.id
    async with pool.acquire() as db:
        await UserService.update_onboarding_step(db, uid, step)
    source = "organic"
    referrer_name = None
    async with pool.acquire() as db:
        user = await UserService.get_user(db, uid)
        if user:
            source = user.get("acquisition_source") or "organic"
    # If completing onboarding (step >= len screens - 1 means going to final)
    if step >= len(_ONBOARDING_SCREENS) - 1:
        # Don't complete yet — user needs to take action on final screen
        pass
    await _send_onboarding_step(callback, uid, step, source, referrer_name)
    await callback.answer()


@dp.callback_query(F.data.startswith("ob_back:"))
async def cb_ob_back(callback: CallbackQuery):
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    step = max(0, int(callback.data.split(":")[1]) - 1)
    uid = callback.from_user.id
    async with pool.acquire() as db:
        await UserService.update_onboarding_step(db, uid, step)
        user = await UserService.get_user(db, uid)
    source = user.get("acquisition_source") or "organic" if user else "organic"
    await _send_onboarding_step(callback, uid, step, source)
    await callback.answer()


@dp.callback_query(F.data == "ob_skip")
async def cb_ob_skip(callback: CallbackQuery):
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    uid = callback.from_user.id
    # Skip to legal pending
    async with pool.acquire() as db:
        await UserService.update_onboarding_step(db, uid, _ONB_STEP_LEGAL, "legal_pending")
    await _send_onboarding_step(callback, uid, _ONB_STEP_LEGAL)
    await callback.answer()


@dp.callback_query(F.data.startswith("ob_accept:"))
async def cb_ob_accept(callback: CallbackQuery):
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    doc_type = callback.data.split(":")[1]
    uid = callback.from_user.id
    try:
        async with pool.acquire() as db:
            await LegalConsentService.accept_document(db, uid, doc_type, "1.0", {
                "source": "telegram_bot",
                "message_id": callback.message.message_id if callback.message else None,
                "chat_id": callback.message.chat.id if callback.message else None,
            })
            # Update onboarding step to legal_pending to refresh screen
            await UserService.update_onboarding_step(db, uid, _ONB_STEP_LEGAL)
        await _send_onboarding_step(callback, uid, _ONB_STEP_LEGAL)
    except Exception as e:
        log.exception("consent accept failed: %s", e)
    await callback.answer()


@dp.callback_query(F.data.startswith("ob_action:"))
async def cb_ob_action(callback: CallbackQuery):
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    action = callback.data.split(":")[1]
    uid = callback.from_user.id
    # Complete onboarding
    async with pool.acquire() as db:
        await UserService.complete_onboarding(db, uid)
    await callback.answer("✅")
    if action == "send_recipe" or action == "start_with_recipe":
        await callback.message.answer(
            "Отправьте мне рецепт — фото, ссылку, текст или голосовое сообщение.\n\n"
            "Пример: просто напишите название блюда и список ингредиентов.")
    elif action == "open_library":
        FR = FRONTEND_URL or ""
        if FR:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📚 Открыть библиотеку",
                                      web_app=WebAppInfo(url=FR))
            ]])
            await callback.message.answer("Откройте библиотеку в мини-приложении 👇", reply_markup=kb)


@dp.callback_query(F.data == "onboarding_cancel")
async def cb_onboarding_cancel(callback: CallbackQuery):
    if callback.message:
        try:
            await callback.message.edit_text("❌ Отменено.", reply_markup=None)
        except Exception:
            pass
    await callback.answer()


# ── Welcome sub-screen callbacks ──────────────────────────────────────────────

@dp.callback_query(F.data == "ob_start_tutorial")
async def cb_ob_start_tutorial(callback: CallbackQuery):
    """User clicked 'Показать, как пользоваться' — start 4-screen onboarding."""
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    uid = callback.from_user.id
    async with pool.acquire() as db:
        await UserService.update_onboarding_step(db, uid, 0)
    await _send_onboarding_step(callback, uid, 0)
    await callback.answer()
    await track(uid, "onboarding_tutorial_chosen")


@dp.callback_query(F.data == "ob_start_skip_to_legal")
async def cb_ob_start_skip_to_legal(callback: CallbackQuery):
    """User clicked 'Начать сразу' — skip tutorial, go to legal."""
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    uid = callback.from_user.id
    async with pool.acquire() as db:
        await UserService.update_onboarding_step(db, uid, _ONB_STEP_LEGAL, "legal_pending")
    await _send_onboarding_step(callback, uid, _ONB_STEP_LEGAL)
    await callback.answer()
    await track(uid, "onboarding_skip_chosen")


@dp.callback_query(F.data == "ws_how_to_add")
async def cb_ws_how_to_add(callback: CallbackQuery):
    if not callback.from_user:
        await callback.answer()
        return
    text = WelcomeService.build_how_to_add_recipe()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍲 Посмотреть пример", callback_data="ws_example")],
        [InlineKeyboardButton(text="🌿 Открыть библиотеку",
                              web_app=WebAppInfo(url=FRONTEND_URL))],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="ws_back")],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()
    await track(callback.from_user.id, "welcome_how_to_add_viewed")


@dp.callback_query(F.data == "ws_example")
async def cb_ws_example(callback: CallbackQuery):
    if not callback.from_user:
        await callback.answer()
        return
    text = WelcomeService.build_example()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Отправить свой рецепт",
                              callback_data="ws_send_hint")],
        [InlineKeyboardButton(text="🌿 Открыть ПОЛЯНУ",
                              web_app=WebAppInfo(url=FRONTEND_URL))],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="ws_back")],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()
    await track(callback.from_user.id, "welcome_example_viewed")


@dp.callback_query(F.data == "ws_send_hint")
async def cb_ws_send_hint(callback: CallbackQuery):
    await callback.answer(
        "Отправьте фото, ссылку, текст или голосовое сообщение следующим сообщением 👇",
        show_alert=True)


@dp.callback_query(F.data == "ws_ai_functions")
async def cb_ws_ai_functions(callback: CallbackQuery):
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    uid = callback.from_user.id
    async with pool.acquire() as db:
        wallet = await WalletService.get_balance(db, uid)
    total = wallet.get("total_available_points", 0)
    text = WelcomeService.build_ai_functions(total)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Получить баллы бесплатно",
                              callback_data="ws_get_points")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="ws_back")],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()
    await track(uid, "welcome_ai_functions_viewed")


@dp.callback_query(F.data == "ws_get_points")
async def cb_ws_get_points(callback: CallbackQuery):
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    uid = callback.from_user.id
    if not FEATURE_REFERRALS:
        await callback.answer("Реферальная программа временно недоступна", show_alert=True)
        return
    async with pool.acquire() as db:
        dashboard = await ReferralService.get_dashboard(db, uid)
    stats = dashboard.get("stats", {})
    wallet = dashboard.get("balance", {})
    text = WelcomeService.build_get_points(
        stats.get("invited", 0),
        wallet.get("bonus_points", 0),
        wallet.get("pending_bonus_points", 0))
    link = dashboard.get("referral_url", "")
    buttons = []
    if link:
        buttons.append([InlineKeyboardButton(text="📨 Пригласить друга",
                                              url=f"https://t.me/share/url?url={urllib.parse.quote(link, safe='')}&text={urllib.parse.quote('🌿 Попробуй ПОЛЯНУ — планировщик застолий с друзьями!', safe='')}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="ws_back")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()
    await track(uid, "welcome_get_points_viewed")


@dp.callback_query(F.data == "ws_help")
async def cb_ws_help(callback: CallbackQuery):
    if not callback.message:
        await callback.answer()
        return
    text = (
        "📚 <b>Как пользоваться ПОЛЯНОЙ</b>\n\n"
        "<b>Добавление рецептов:</b>\n"
        "• 📸 Фото или скриншот рецепта\n"
        "• 🔗 Ссылка на сайт с рецептом\n"
        "• 📝 Текст рецепта\n"
        "• 🎙 Голосовое сообщение\n"
        "• /add — добавление вручную\n\n"
        "<b>Библиотека:</b>\n"
        "Откройте ПОЛЯНу кнопкой внизу → вкладка «Рецепты»\n\n"
        "<b>Команды:</b>\n"
        "/start — главное меню\n"
        "/add — добавить рецепт\n"
        "/ref — партнёрская программа\n"
        "/documents — юридические документы\n"
        "/privacy — данные и конфиденциальность\n"
        "/help — эта справка"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="ws_back")],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "ws_back")
async def cb_ws_back(callback: CallbackQuery):
    """Return to welcome/dashboard screen."""
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    uid = callback.from_user.id
    async with pool.acquire() as db:
        usr = await UserService.get_user(db, user_id=uid)
    if not usr:
        await callback.answer()
        return

    if usr["onboarding_status"] not in ("completed", "skipped"):
        # New user — show welcome
        source = usr.get("acquisition_source") or "organic"
        referrer_name = None
        if source == "referral" and usr.get("referrer_user_id"):
            async with pool.acquire() as db:
                ref_user = await db.fetchrow(
                    "SELECT first_name FROM users WHERE telegram_user_id=$1",
                    usr["referrer_user_id"])
                if ref_user:
                    referrer_name = ref_user["first_name"]
        if source == "referral" and referrer_name:
            text = WelcomeService.build_referral_welcome(
                callback.from_user.first_name, referrer_name, WELCOME_POINTS)
        else:
            text = WelcomeService.build_new_user_welcome(
                callback.from_user.first_name, source, referrer_name, WELCOME_POINTS)
        kb = _welcome_keyboard()
    else:
        # Returning user — compact dashboard
        async with pool.acquire() as db:
            recipes_count = await db.fetchval(
                "SELECT COUNT(*) FROM recipes WHERE user_id=$1", uid) or 0
            events_count = 0
            if FEATURE_EVENTS:
                events_count = await db.fetchval(
                    "SELECT COUNT(*) FROM events WHERE telegram_user_id=$1", uid) or 0
            wallet = await WalletService.get_balance(db, uid)
        total = wallet.get("total_available_points", 0)
        text = WelcomeService.build_returning_user_dashboard(
            callback.from_user.first_name, recipes_count, events_count, total, FEATURE_EVENTS)
        kb = _returning_user_keyboard()

    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


# ── Bot helpers ───────────────────────────────────────────────────────────────

# Text recipe buffering — debounce consecutive text messages
_text_buffers: dict[int, dict] = {}
_TEXT_DEBOUNCE_SEC = 3.5


async def _reply_parse_error(status_msg, err: Exception, hint: str = "рецепт"):
    msg = str(err)
    if "ai_consent_required" in msg:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Разрешить и продолжить", callback_data="ob_accept:ai_processing_consent")],
            [InlineKeyboardButton(text="📋 Подробнее", callback_data="legal_doc:ai_processing_consent")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="onboarding_cancel")],
        ])
        await status_msg.edit_text(
            "🤖 <b>Для этой функции используется внешний ИИ.</b>\n\n"
            "В ИИ будет передано содержимое рецепта, изображения "
            "или голосового сообщения без вашего Telegram ID.\n\n"
            "Подробнее: /documents",
            reply_markup=kb)
    elif "insufficient_balance" in msg:
        try:
            _, needed, available = msg.split(":")
            needed, available = int(needed), int(available)
        except Exception:
            needed, available = 0, 0
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Пополнить через Telegram Stars", callback_data="balance_stars")],
            [InlineKeyboardButton(text="🎁 Получить баллы бесплатно", callback_data="ws_get_points")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="onboarding_cancel")],
        ])
        await status_msg.edit_text(
            f"💰 <b>Недостаточно AI-баллов</b>\n\n"
            f"Для этой функции нужно: <b>{needed}</b>\n"
            f"На балансе: <b>{available}</b>",
            reply_markup=kb)
    elif isinstance(err, ValueError):
        await status_msg.edit_text(f"🤷 {msg}")
    elif "not_a_recipe" in msg or "Не удалось распознать" in msg:
        await status_msg.edit_text(f"🤷 Не смог найти {hint} в этом контенте.\nПришли ссылку или команду /add")
    elif "429" in msg or "rate-limit" in msg.lower() or "temporarily" in msg.lower():
        await status_msg.edit_text("⏳ Сервис распознавания перегружен. Попробуй через минуту.")
    else:
        log.error("parse error (%s): %s", hint, err)
        await status_msg.edit_text("❌ Не получилось разобрать. Попробуй ещё раз или пришли текст/ссылку.")


async def _flush_text_buffer(user_id: int):
    try:
        await asyncio.sleep(_TEXT_DEBOUNCE_SEC)
    except asyncio.CancelledError:
        return
    buf = _text_buffers.pop(user_id, None)
    if not buf:
        return
    combined = "\n".join(buf["parts"]).strip()
    status = buf["status_msg"]
    try:
        recipe = await parse_and_save_recipe(user_id, text=combined)
        await _reply_recipe_saved(status, recipe, status_msg=status)
    except ValueError:
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        await _reply_parse_error(status, e, "рецепт")


async def _send_referral(msg: Message, uid: int):
    async with pool.acquire() as db:
        dashboard = await ReferralService.get_dashboard(db, uid)
    username = await _get_bot_username()
    link = dashboard["referral_url"]
    stats = dashboard["stats"]
    wallet = dashboard["balance"]
    text = (
        "🎁 <b>Пользуйтесь ИИ в ПОЛЯНЕ бесплатно</b>\n\n"
        "Приглашайте друзей и получайте <b>10%</b> от каждого\n"
        "их пополнения бонусными баллами.\n\n"
        "Баллы можно тратить на:\n"
        "📷 распознавание рецептов по фото\n"
        "🎙 обработку голосовых сообщений\n"
        "✨ создание меню\n"
        "🍽 AI-рекомендации\n"
        "🧾 распознавание чеков\n\n"
        f"🔗 Твоя ссылка:\n<code>{link}</code>\n\n"
        f"👥 Приглашено друзей: <b>{stats['invited']}</b>\n"
        f"✅ Активных: <b>{stats['activated']}</b>\n"
        f"💳 Пополняли баланс: <b>{stats['paying']}</b>\n"
        f"💰 Получено всего: <b>{stats['total_reward_points']} баллов</b>"
    )
    kb = None
    if username:
        share_text = "🌿 Я сохраняю рецепты в ПОЛЯНЕ"
        share_url = f"https://t.me/share/url?url={urllib.parse.quote(link, safe='')}&text={urllib.parse.quote(share_text, safe='')}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📨 Пригласить друга", url=share_url)],
        ])
    await msg.answer(text, reply_markup=kb)


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


@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    if not message.from_user or pool is None:
        return
    await _send_referral(message, message.from_user.id)


@dp.message(Command("terms"))
async def cmd_terms(message: Message):
    await message.answer(
        "📄 <b>Правила бонусной программы «Приглашение друзей»</b>\n\n"
        "1. Бонус начисляется за подходящие денежные пополнения баланса.\n"
        "2. Размер по умолчанию — 10% от суммы пополнения.\n"
        "3. Бонус начисляется бонусными баллами (AI-баллами).\n"
        "4. Баллы нельзя вывести, перевести другому пользователю или обменять на деньги.\n"
        "5. Баллы используются только внутри ПОЛЯНЫ для ИИ-функций.\n"
        "6. Баллы не начисляются с бонусной части оплаты.\n"
        "7. Начисление может быть отменено при возврате оплаты.\n"
        "8. Саморефералы запрещены.\n"
        "9. Один пользователь может иметь только одного пригласившего.\n"
        "10. При злоупотреблениях бонус может быть отменён.\n"
        "11. Правила и процент могут изменяться только для будущих операций.\n\n"
        "Начисление доступно через 7 дней после пополнения.\n\n"
        "<i>Подробнее: /ref</i>"
    )


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    if message.from_user:
        await message.answer(f"Твой chat_id: <code>{message.from_user.id}</code>")


@dp.message(Command("opbalance"))
async def cmd_opbalance(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_CHAT_ID:
        return
    remaining = await _openrouter_remaining_usd()
    if remaining is not None:
        await message.answer(f"💵 OpenRouter баланс: ${remaining:.2f}")
    else:
        await message.answer("❌ Не удалось получить баланс")


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



# ── Share recipe callback ────────────────────────────────────────────────────

async def _format_recipe_for_share(recipe_id: int) -> str:
    """Fetch recipe from DB and format as shareable text."""
    if pool is None:
        return ""
    async with pool.acquire() as db:
        rec = await db.fetchrow("SELECT * FROM recipes WHERE id=$1", recipe_id)
        if not rec:
            return ""
        ings = await db.fetch(
            "SELECT name, qty, unit FROM ingredients WHERE recipe_id=$1 ORDER BY id", recipe_id
        )
        steps = await db.fetch(
            "SELECT step_number, text FROM recipe_steps WHERE recipe_id=$1 ORDER BY step_number", recipe_id
        )
    lines = [f"{rec['emoji'] or '🍽'} {rec['name']}"]
    meta = []
    if rec.get('category'):
        meta.append(rec['category'])
    if rec.get('servings'):
        meta.append(f"🍽 {rec['servings']} порц.")
    if rec.get('cook_time_minutes'):
        meta.append(f"⏱ {rec['cook_time_minutes']} мин.")
    if meta:
        lines.append(" · ".join(meta))
    if ings:
        lines.append(f"\n🥄 Ингредиенты ({len(ings)}):")
        for ing in ings:
            q = ing['qty']
            if q and q != 0:
                q_str = str(int(q)) if q == int(q) else str(round(q, 2)).rstrip('0').rstrip('.')
                qty = f"{q_str} {ing['unit'] or ''}".strip()
            else:
                qty = ''
            lines.append(f"  • {ing['name']}" + (f" — {qty}" if qty else ""))
    if steps:
        lines.append(f"\n📋 Приготовление:")
        for s in steps:
            lines.append(f"  {s['step_number']}. {s['text']}")
    lines.append(f"\n🌿 Рецепт из ПОЛЯНЫ")
    return "\n".join(lines)


@dp.callback_query(F.data.startswith("share_recipe_"))
async def handle_share_recipe(callback: CallbackQuery):
    """Share recipe from chat — opens contact picker via inline."""
    if not callback.from_user:
        await callback.answer()
        return
    try:
        recipe_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка", show_alert=True)
        return

    # Create share token
    if pool is None:
        await callback.answer("Сервис запускается", show_alert=True)
        return

    async with pool.acquire() as db:
        # Build full snapshot with ingredients and steps
        rec = await db.fetchrow("SELECT * FROM recipes WHERE id=$1", recipe_id)
        if not rec:
            await callback.answer("Рецепт не найден", show_alert=True)
            return
        ings = await db.fetch("SELECT name, qty, unit FROM ingredients WHERE recipe_id=$1 ORDER BY id", recipe_id)
        steps = await db.fetch("SELECT step_number, text FROM recipe_steps WHERE recipe_id=$1 ORDER BY step_number", recipe_id)
        snapshot = {
            "name": rec["name"], "emoji": rec["emoji"] or "🍽",
            "category": rec.get("category"), "servings": rec.get("servings"),
            "cook_time_minutes": rec.get("cook_time_minutes"),
            "ingredients": [{"name": i["name"], "qty": i.get("qty"), "unit": i.get("unit")} for i in ings],
            "steps": [{"step_number": s["step_number"], "text": s["text"]} for s in steps],
        }
        token = secrets.token_urlsafe(16)
        share = await db.fetchrow(
            "INSERT INTO recipe_shares (token, source_recipe_id, owner_user_id, snapshot) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            token, recipe_id, callback.from_user.id, json.dumps(snapshot)
        )

    bot_username = await _get_bot_username()
    share_screen_url = f"https://t.me/{bot_username}?startapp=share_{token}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📤 Поделиться рецептом",
            web_app=WebAppInfo(url=share_screen_url),
        )
    ]])

    await callback.message.answer(
        "Откроется ПОЛЯНА — нажмите «Выбрать чат и отправить»:", reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("rs:"))
async def handle_shared_recipe_save(callback: CallbackQuery):
    """Save a shared recipe to the recipient's library."""
    if not callback.from_user or pool is None:
        await callback.answer()
        return

    token = callback.data.removeprefix("rs:")

    async with pool.acquire() as db:
        share = await db.fetchrow(
            "SELECT * FROM recipe_shares WHERE token=$1 AND revoked_at IS NULL", token
        )
        if not share:
            await callback.answer("Рецепт больше недоступен", show_alert=True)
            return

        # Check if already saved
        already = await db.fetchval(
            "SELECT 1 FROM recipe_share_saves WHERE share_id=$1 AND recipient_user_id=$2",
            share["id"], callback.from_user.id
        )
        if already:
            await callback.answer("Уже сохранено в вашей библиотеке!", show_alert=True)
            return

        snap = share["snapshot"]
        if isinstance(snap, str):
            snap = json.loads(snap)

        # Create recipe from snapshot
        new_rec = await db.fetchrow(
            "INSERT INTO recipes (user_id, name, emoji, source_type, servings, cook_time_minutes, category) "
            "VALUES ($1, $2, $3, 'shared', $4, $5, $6) RETURNING id",
            callback.from_user.id, snap["name"], snap.get("emoji", "🍽"),
            snap.get("servings", 4), snap.get("cook_time_minutes"), snap.get("category")
        )

        # Copy ingredients
        for i in snap.get("ingredients", []):
            await db.execute(
                "INSERT INTO ingredients (recipe_id, name, qty, unit) VALUES ($1,$2,$3,$4)",
                new_rec["id"], i["name"], i.get("qty"), i.get("unit")
            )

        # Copy steps
        for s in snap.get("steps", []):
            await db.execute(
                "INSERT INTO recipe_steps (recipe_id, step_number, text) VALUES ($1,$2,$3)",
                new_rec["id"], s["step_number"], s["text"]
            )

        # Record the save
        await db.execute(
            "INSERT INTO recipe_share_saves (share_id, recipient_user_id, created_recipe_id) VALUES ($1,$2,$3)",
            share["id"], callback.from_user.id, new_rec["id"]
        )

        # Referral: bind owner as referrer for truly new users
        owner_id = share["owner_user_id"]
        recipient_id = callback.from_user.id
        if owner_id != recipient_id:
            is_new = not await ReferralService.is_active_user(db, recipient_id)
            if is_new:
                bound = await ReferralService.bind_referrer_from_recipe_share(
                    db, recipient_id, owner_id, str(share["id"])
                )
                if bound:
                    log.info("referral_bound: referrer=%s referred=%s via=recipe_share share=%s",
                             owner_id, recipient_id, share["id"])

    await callback.answer("Рецепт сохранён в ПОЛЯНУ 🌿", show_alert=True)


# ── Inline mode: share recipes from any chat ─────────────────────────────────

@dp.inline_query()
async def handle_inline_query(inline_query: InlineQuery):
    """@reciptesbot — show recipes to share. Handles both bare queries and share:token."""
    if pool is None:
        await inline_query.answer([], cache_time=30)
        return

    user_id = inline_query.from_user.id if inline_query.from_user else 0
    query = (inline_query.query or "").strip()

    # share:token — triggered by switch_inline_query_chosen_chat
    if query.startswith("share:"):
        token = query.removeprefix("share:")
        async with pool.acquire() as db:
            share = await db.fetchrow(
                "SELECT rs.*, r.name, r.emoji, r.category, r.servings, r.cook_time_minutes "
                "FROM recipe_shares rs JOIN recipes r ON r.id = rs.source_recipe_id "
                "WHERE rs.token=$1 AND rs.revoked_at IS NULL", token
            )
        if not share:
            await inline_query.answer([], cache_time=30)
            return

        snap = share["snapshot"]
        if isinstance(snap, str):
            snap = json.loads(snap)
        lines = [f"{snap.get('emoji', '🍽')} <b>{snap['name']}</b>"]
        meta = []
        if snap.get("category"):
            meta.append(snap["category"])
        if snap.get("servings"):
            meta.append(f"🍽 {snap['servings']} порц.")
        if snap.get("cook_time_minutes"):
            meta.append(f"⏱ {snap['cook_time_minutes']} мин.")
        if meta:
            lines.append(" · ".join(meta))
        for i in snap.get("ingredients", []):
            q = i.get("qty")
            if q and q != 0:
                q_str = str(int(q)) if q == int(q) else str(round(q, 2)).rstrip("0").rstrip(".")
                qty = f"{q_str} {i.get('unit') or ''}".strip()
            else:
                qty = ""
            lines.append(f"  • {i['name']}" + (f" — {qty}" if qty else ""))
        for s in snap.get("steps", []):
            lines.append(f"  {s['step_number']}. {s['text']}")
        lines.append("\n🌿 Рецепт из ПОЛЯНЫ")
        text = "\n".join(lines)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💾 Сохранить себе", callback_data=f"rs:{token}")],
        ])

        bot_username = await _get_bot_username()
        mini_app_url = f"https://t.me/{bot_username}?startapp=shared_{token}"

        await inline_query.answer([
            InlineQueryResultArticle(
                id=str(share["id"]),
                title=f"{snap['name']} — нажмите, чтобы отправить",
                description="Рецепт из ПОЛЯНЫ · нажмите на карточку",
                input_message_content=InputTextMessageContent(message_text=text, parse_mode="HTML"),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💾 Сохранить себе", callback_data=f"rs:{token}")],
                    [InlineKeyboardButton(text="🌿 Открыть в ПОЛЯНЕ", url=mini_app_url)],
                ]),
            )
        ], cache_time=0, is_personal=True)
        return

    # Bare query or empty — show user's latest recipes
    async with pool.acquire() as db:
        if query:
            rows = await db.fetch(
                "SELECT id, name, emoji, category, servings, cook_time_minutes "
                "FROM recipes WHERE user_id=$1 AND name ILIKE $2 ORDER BY name LIMIT 50",
                user_id, f"%{query}%"
            )
        else:
            rows = await db.fetch(
                "SELECT id, name, emoji, category, servings, cook_time_minutes "
                "FROM recipes WHERE user_id=$1 ORDER BY created_at DESC LIMIT 50",
                user_id
            )

    results = []
    for row in rows:
        recipe_id = row["id"]
        emoji = row["emoji"] or "🍽"
        name = row["name"]
        cat = f"[{row['category']}] " if row.get("category") else ""
        serv = f"🍽 {row['servings']} порц. " if row.get("servings") else ""
        ct = f"⏱ {row['cook_time_minutes']} мин" if row.get("cook_time_minutes") else ""
        desc = f"{cat}{serv}{ct}".strip() or "Рецепт из Поляны"

        text = await _format_recipe_for_share(recipe_id)
        if not text:
            continue

        # Create share token for this recipe
        async with pool.acquire() as db:
            token = secrets.token_urlsafe(16)
            share_rec = await db.fetchrow(
                "INSERT INTO recipe_shares (token, source_recipe_id, owner_user_id, snapshot) "
                "VALUES ($1, $2, $3, (SELECT row_to_json(r)::jsonb FROM "
                "(SELECT name, emoji, category, servings, cook_time_minutes FROM recipes WHERE id=$2) r)) "
                "RETURNING id",
                token, recipe_id, user_id
            )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💾 Сохранить", callback_data=f"rs:{token}")],
        ])

        results.append(
            InlineQueryResultArticle(
                id=str(recipe_id),
                title=f"{emoji} {name}",
                description=desc,
                input_message_content=InputTextMessageContent(message_text=text),
                reply_markup=kb,
            )
        )

    await inline_query.answer(results, cache_time=300, is_personal=True)


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
@dp.message(F.photo & F.chat.type.in_({"group", "supergroup"}))
async def handle_photo_for_split(message: Message):
    """Handle photo - check if it's for split receipt."""
    if not SPLIT_AVAILABLE:
        return  # Let other handlers process

    async with pool.acquire() as db:
        # Check if there's an active split in this chat
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

        # Process receipt
        msg, is_free = await handle_receipt_photo(
            db, message.from_user.id, photo_bytes.read(), event['id'], message.bot
        )

    await message.answer(msg, reply_markup=split_event_keyboard(event['id']))

@dp.message(F.photo)
async def handle_photo_message(message: Message):
    log.info("Photo received: user=%s chat=%s media_group=%s",
             message.from_user.id if message.from_user else "?",
             message.chat.id, message.media_group_id)
    if not message.from_user or pool is None:
        log.warning("Photo skipped: user=%s pool=%s", message.from_user.id if message.from_user else "?", pool is not None)
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
    if not message.from_user or pool is None:
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


# ── Text / URL handler ────────────────────────────────────────────────────────

@dp.message(F.text & ~F.text.startswith("/"), StateFilter(None))
async def handle_text_message(message: Message, state: FSMContext):
    if not message.from_user or pool is None:
        return
    text = message.text or ""
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
    if len(text) < 30:
        return   # too short, silently ignore

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


@dp.message(VoiceStates.editing, F.text)
async def voice_edited_text(message: Message, state: FSMContext):
    if not message.from_user or pool is None:
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
    if pool is None:
        await bot.answer_pre_checkout_query(query.id, ok=False,
            error_message="Сервис запускается, попробуйте через минуту")
        return
    payload = query.invoice_payload or ""
    if not payload.startswith("po:"):
        await bot.answer_pre_checkout_query(query.id, ok=False,
            error_message="Некорректный платёж. Создайте новый счёт.")
        return
    async with pool.acquire() as db:
        order = await PaymentService.find_order_by_payload(db, payload)
    if not order:
        await bot.answer_pre_checkout_query(query.id, ok=False,
            error_message="Заказ не найден. Создайте новый счёт.")
        return
    if order["user_id"] != query.from_user.id:
        await bot.answer_pre_checkout_query(query.id, ok=False,
            error_message="Этот счёт предназначен для другого пользователя.")
        return
    if order["provider"] != "telegram_stars":
        await bot.answer_pre_checkout_query(query.id, ok=False,
            error_message="Неверный способ оплаты.")
        return
    if order["status"] not in ("created", "pending"):
        await bot.answer_pre_checkout_query(query.id, ok=False,
            error_message="Заказ уже обработан. Создайте новый счёт.")
        return
    if order["currency"] != "XTR":
        await bot.answer_pre_checkout_query(query.id, ok=False,
            error_message="Неверная валюта.")
        return
    if order["amount"] != query.total_amount:
        await bot.answer_pre_checkout_query(query.id, ok=False,
            error_message="Сумма не совпадает. Создайте новый счёт.")
        return
    if order["expires_at"] and order["expires_at"] < datetime.now(timezone.utc):
        await bot.answer_pre_checkout_query(query.id, ok=False,
            error_message="Счёт истёк. Создайте новый счёт.")
        return
    try:
        await bot.answer_pre_checkout_query(query.id, ok=True)
    except Exception:
        log.exception("pre_checkout answer failed")


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    sp = message.successful_payment
    payload = sp.invoice_payload or ""
    if not payload.startswith("po:") or pool is None:
        return
    async with pool.acquire() as db:
        order = await PaymentService.find_order_by_payload(db, payload)
    if not order:
        log.warning("successful_payment: order not found for payload %s", payload[:20])
        return
    if order["user_id"] != message.from_user.id:
        log.warning("successful_payment: user mismatch")
        return
    if order["status"] == "succeeded":
        return  # Idempotent — already processed
    charge_id = sp.telegram_payment_charge_id
    # Mark order paid
    async with pool.acquire() as db:
        paid = await PaymentService.mark_order_paid(db, order["id"], charge_id)
        if not paid:
            return  # Already processed
        # Credit balance via unified method
        order_dict = dict(order)
        order_dict["package_code"] = ""
        try:
            pkg = await db.fetchrow("SELECT code FROM payment_packages WHERE id=$1", order["package_id"])
            if pkg:
                order_dict["package_code"] = pkg["code"]
        except Exception:
            pass
        new_bal = await WalletService.credit_purchase(db, order_dict)
        # Process referral reward
        reward = await ReferralService.process_successful_payment(
            db, payment_id=charge_id, user_id=order["user_id"],
            cash_amount_minor=order["amount"],
            metadata={"method": "stars", "order_id": str(order["id"])}
        )
    await track(order["user_id"], "stars_payment_succeeded",
                props={"order_id": str(order["id"]), "points": order["total_points"],
                       "stars": sp.total_amount})
    try:
        available = new_bal.get("available", 0)
        msg = (
            f"✅ <b>Баланс пополнен</b>\n\n"
            f"Начислено: <b>{order['total_points']} AI-баллов</b>\n"
            f"Текущий баланс: <b>{available} AI-баллов</b>"
        )
        if reward:
            msg += f"\n\n🎁 Реферальный бонус: +{reward} баллов начислен вашему пригласившему"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✨ Посмотреть AI-функции", callback_data="ws_ai_functions")],
            [InlineKeyboardButton(text="🌿 Открыть ПОЛЯНУ", web_app=WebAppInfo(url=FRONTEND_URL))],
        ])
        await message.answer(msg, reply_markup=kb)
    except Exception:
        pass


# ── /balance command ──────────────────────────────────────────────────────────

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    if not message.from_user or pool is None:
        return
    uid = message.from_user.id
    async with pool.acquire() as db:
        bal = await WalletService.get_available_balance(db, uid)
    text = (
        f"✨ <b>AI-баланс</b>\n\n"
        f"Доступно: <b>{bal['available']} баллов</b>\n"
        f"Куплено: {bal['paid']}\n"
        f"Получено бонусами: {bal['bonus']}\n"
        f"Зарезервировано: {bal['reserved']}\n\n"
        f"Баллы используются только для функций с ИИ:\n"
        f"распознавания фото и голоса, генерации рецептов,\n"
        f"изображений и умных рекомендаций.\n\n"
        f"Основная библиотека и обычные инструменты бесплатны."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Пополнить через Telegram Stars", callback_data="balance_stars")],
        [InlineKeyboardButton(text="🎁 Получить баллы бесплатно", callback_data="ws_get_points")],
        [InlineKeyboardButton(text="📊 История операций", callback_data="balance_history")],
    ])
    await message.answer(text, reply_markup=kb)
    await track(uid, "balance_screen_opened")


@dp.callback_query(F.data == "balance_stars")
async def cb_balance_stars(callback: CallbackQuery):
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    async with pool.acquire() as db:
        packages = await PaymentService.get_available_packages(db, "telegram_stars")
    if not packages:
        await callback.answer("Пакеты временно недоступны", show_alert=True)
        return
    lines = ["⭐ <b>Пополнить AI-баланс</b>\n"]
    buttons = []
    for pkg in packages:
        total = pkg["base_points"] + (pkg["promo_points"] or 0)
        promo_note = f"\n    Включает {pkg['promo_points']} бонусных" if pkg.get("promo_points") else ""
        lines.append(f"<b>{pkg['title']}</b>\n    {total} AI-баллов — {pkg['stars_amount']} ⭐{promo_note}")
        buttons.append([InlineKeyboardButton(
            text=f"{total} баллов — {pkg['stars_amount']} ⭐",
            callback_data=f"buy_stars:{pkg['code']}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="balance_back")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    except Exception:
        await callback.message.answer("\n".join(lines), reply_markup=kb)
    await callback.answer()
    await track(callback.from_user.id, "payment_packages_opened", props={"provider": "stars"})


@dp.callback_query(F.data.startswith("buy_stars:"))
async def cb_buy_stars(callback: CallbackQuery):
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    package_code = callback.data.split(":", 1)[1]
    uid = callback.from_user.id
    try:
        async with pool.acquire() as db:
            order = await PaymentService.create_order(db, uid, package_code, "telegram_stars")
    except ValueError as e:
        await callback.answer(str(e), show_alert=True)
        return
    try:
        await bot.send_invoice(
            chat_id=uid,
            title=f"ПОЛЯНА — {order['title']}",
            description=f"{order['total_points']} AI-баллов",
            payload=order["invoice_payload"],
            currency="XTR",
            prices=[LabeledPrice(label=order["title"], amount=order["amount"])],
        )
        await callback.answer()
        await track(uid, "stars_invoice_opened", props={"package": package_code})
    except Exception as e:
        log.exception("send_invoice failed")
        await callback.answer("Не удалось создать счёт. Попробуйте позже.", show_alert=True)


@dp.callback_query(F.data == "balance_back")
async def cb_balance_back(callback: CallbackQuery):
    """Return to balance screen."""
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    uid = callback.from_user.id
    async with pool.acquire() as db:
        bal = await WalletService.get_available_balance(db, uid)
    text = (
        f"✨ <b>AI-баланс</b>\n\n"
        f"Доступно: <b>{bal['available']} баллов</b>\n"
        f"Куплено: {bal['paid']}\n"
        f"Получено бонусами: {bal['bonus']}\n"
        f"Зарезервировано: {bal['reserved']}\n\n"
        f"Баллы используются только для функций с ИИ.\n"
        f"Основная библиотека бесплатна."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Пополнить через Telegram Stars", callback_data="balance_stars")],
        [InlineKeyboardButton(text="🎁 Получить баллы бесплатно", callback_data="ws_get_points")],
        [InlineKeyboardButton(text="📊 История операций", callback_data="balance_history")],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "balance_history")
async def cb_balance_history(callback: CallbackQuery):
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    uid = callback.from_user.id
    async with pool.acquire() as db:
        rows = await db.fetch(
            "SELECT * FROM wallet_ledger WHERE user_id=$1 ORDER BY created_at DESC LIMIT 20", uid)
    if not rows:
        text = "📊 <b>История операций</b>\n\nПока нет операций."
    else:
        lines = ["📊 <b>История операций</b>\n"]
        type_labels = {
            "purchase_credit": "Покупка", "promo_credit": "Бонус к покупке",
            "referral_credit": "Реферальное начисление", "referral_pending": "Реферальное начисление (ожидание)",
            "referral_activated": "Реферальное начисление", "ai_reservation": "Резерв ИИ",
            "ai_usage": "Использование ИИ", "ai_usage_refund": "Возврат за ИИ",
            "ai_reservation_release": "Освобождение резерва",
            "manual_adjustment": "Корректировка", "referral_cancelled": "Отмена реферального",
            "referral_reversed": "Реверс реферального",
        }
        for r in rows:
            sign = "+" if r["amount"] > 0 else ""
            label = type_labels.get(r["transaction_type"], r["transaction_type"])
            dt = r["created_at"].strftime("%d.%m %H:%M") if r["created_at"] else ""
            lines.append(f"{sign}{r['amount']} баллов — {label} · {dt}")
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="balance_back")],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


# ── /start command ────────────────────────────────────────────────────────────


# ── Split Command ──────────────────────────────────────────────────────────
@dp.message(Command("split"))
async def cmd_split(message: Message, db=Depends(get_db)):
    """Main split command."""
    if not SPLIT_AVAILABLE:
        await message.answer("Модуль «Делёж» пока не подключён.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        # Create new split event
        title = args[1].strip()
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
async def cmd_split_add(message: Message, db=Depends(get_db)):
    """Add participant to split event."""
    if not SPLIT_AVAILABLE:
        await message.answer("Модуль «Делёж» пока не подключён.")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /split_add @username или /split_add user_id")
        return

    # Get active split event for this chat
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
async def cmd_split_join(message: Message, db=Depends(get_db)):
    """Join active split event."""
    if not SPLIT_AVAILABLE:
        await message.answer("Модуль «Делёж» пока не подключён.")
        return

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
async def cmd_split_done(message: Message, db=Depends(get_db)):
    """Calculate and send debts."""
    if not SPLIT_AVAILABLE:
        await message.answer("Модуль «Делёж» пока не подключён.")
        return

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
async def cb_split_list(callback: CallbackQuery, db=Depends(get_db)):
    """List user's split events."""
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
async def cb_split_members(callback: CallbackQuery, db=Depends(get_db)):
    """Show event members."""
    event_id = int(callback.data.split("_")[2])
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
async def cb_split_done(callback: CallbackQuery, db=Depends(get_db)):
    """Calculate and notify."""
    event_id = int(callback.data.split("_")[2])
    summary = await calculate_and_notify(db, event_id, callback.message.bot)
    await callback.message.answer(summary)

    # Close event
    await db.execute(
        "UPDATE split_events SET status = 'closed' WHERE id = $1",
        event_id
    )
    await callback.answer()

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not message.from_user or pool is None:
        return
    user = message.from_user
    text = message.text or ""
    arg = text.split(maxsplit=1)[1] if " " in text else None

    # Determine acquisition source
    acquisition_source = "organic"
    source_token = None
    if arg:
        if arg.startswith("ref_"):
            acquisition_source = "referral"
            source_token = arg
        elif arg.startswith("event_"):
            acquisition_source = "event_invite"
            source_token = arg
        elif arg.startswith("save_recipe_"):
            acquisition_source = "recipe_share"
            source_token = arg

    # Ensure user record exists
    async with pool.acquire() as db:
        existing_user = await UserService.get_user(db, user.id)
        if not existing_user:
            referrer_user_id = None
            if acquisition_source == "referral" and source_token:
                code = source_token.replace("ref_", "")
                referrer_user_id = await db.fetchval(
                    "SELECT user_id FROM referral_codes WHERE code=$1", code)
                if referrer_user_id == user.id:
                    referrer_user_id = None
            await UserService.get_or_create_user(
                db, user, acquisition_source, source_token, referrer_user_id)
        else:
            await db.execute(
                "UPDATE users SET first_name=$2, last_name=$3, username=$4, "
                "language_code=$5, updated_at=NOW() WHERE telegram_user_id=$1",
                user.id, user.first_name, user.last_name,
                user.username, getattr(user, 'language_code', None))

    # Deep link: save_recipe_{id}
    if arg and arg.startswith("save_recipe_"):
        try:
            source_id = int(arg.split("_", 2)[2])
        except (ValueError, IndexError):
            await message.answer("❌ Некорректная ссылка.")
            return

        async with pool.acquire() as db:
            usr = await UserService.get_user(db, user.id)
            if usr and usr["onboarding_status"] not in ("completed", "skipped"):
                src = await db.fetchrow("SELECT * FROM recipes WHERE id=$1", source_id)
                if src:
                    import hashlib as _hl
                    token_hash = _hl.sha256(f"save_recipe_{source_id}".encode()).hexdigest()
                    await db.execute(
                        "INSERT INTO pending_onboarding_actions "
                        "(user_id, action_type, token_hash, payload, expires_at) "
                        "VALUES ($1,'save_recipe',$2,$3,NOW() + INTERVAL '24 hours')",
                        user.id, token_hash, json.dumps({"recipe_id": source_id, "name": src["name"]}))
            else:
                src = await db.fetchrow("SELECT * FROM recipes WHERE id=$1", source_id)
            if not src:
                await message.answer("❌ Рецепт не найден.")
                return

            existing = await db.fetchrow(
                "SELECT id FROM recipes WHERE user_id=$1 AND name=$2",
                user.id, src["name"]
            )
            if existing:
                await message.answer(f"📚 Рецепт «{src['name']}» уже есть в вашей библиотеке.")
                return

            new_rec = await db.fetchrow(
                "INSERT INTO recipes (user_id, name, name_original, emoji, source_url, "
                "source_type, original_language, servings, cook_time_minutes, category, tags, notes) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id",
                user.id, src["name"], src.get("name_original"), src.get("emoji"),
                src.get("source_url"), "shared", src.get("original_language"),
                src.get("servings", 4), src.get("cook_time_minutes"), src.get("category"),
                src.get("tags", []), src.get("notes")
            )

            ings = await db.fetch(
                "SELECT name, qty, unit, category, sort_order FROM ingredients WHERE recipe_id=$1 ORDER BY id",
                source_id
            )
            for ing in ings:
                await db.execute(
                    "INSERT INTO ingredients (recipe_id, name, qty, unit, category, sort_order) "
                    "VALUES ($1,$2,$3,$4,$5,$6)",
                    new_rec["id"], ing["name"], ing.get("qty"), ing.get("unit"),
                    ing.get("category"), ing.get("sort_order", 0)
                )

            steps = await db.fetch(
                "SELECT step_number, text FROM recipe_steps WHERE recipe_id=$1 ORDER BY step_number",
                source_id
            )
            for step in steps:
                await db.execute(
                    "INSERT INTO recipe_steps (recipe_id, step_number, text) VALUES ($1,$2,$3)",
                    new_rec["id"], step["step_number"], step["text"]
                )

        ings_count = len(ings) if ings else 0
        await message.answer(
            f"✅ <b>Рецепт сохранён!</b>\n\n"
            f"{src.get('emoji', '🍽')} <b>{src['name']}</b>\n"
            f"🥕 {ings_count} ингредиентов · 📋 {len(steps) if steps else 0} шагов",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="📖 Открыть",
                    web_app=WebAppInfo(url=f"{FRONTEND_URL}?screen=recipe&id={new_rec['id']}")
                ),
            ]])
        )
        return

    # Analytics: top-of-funnel + attribution source (ref_<id> / event_<id> / organic)
    await track(user.id, "user_start", src_payload=(arg or "organic"))

    # Referral capture: ?start=ref_<code> (URL-safe code, not user_id)
    if arg and arg.startswith("ref_") and pool is not None:
        code = arg.replace("ref_", "")
        try:
            async with pool.acquire() as db:
                # Find referrer by code
                referrer_user_id = await db.fetchval(
                    "SELECT user_id FROM referral_codes WHERE code=$1", code
                )
                if referrer_user_id and referrer_user_id != user.id:
                    # Check if user is truly new (no recipes, no events, no payments)
                    is_new = not await ReferralService.is_active_user(db, user.id)
                    if is_new:
                        bound = await ReferralService.bind_referrer(
                            db, user.id, referrer_user_id,
                            source_type="referral_link", source_id=code
                        )
                        if bound:
                            # Also create legacy referral for backward compat
                            await db.execute(
                                "INSERT INTO referrals (referee_id, referrer_id) VALUES ($1,$2) "
                                "ON CONFLICT (referee_id) DO NOTHING",
                                user.id, referrer_user_id,
                            )
                            log.info("referral_bound: referrer=%s referred=%s via=ref_link code=%s",
                                     referrer_user_id, user.id, code)
                    elif not is_new:
                        log.info("referral_skip_active_user: user=%s", user.id)
        except Exception:
            log.exception("referral capture failed")
        # fall through to the normal welcome below

    if arg and arg.startswith("event_"):
        try:
            event_id = int(arg.replace("event_", ""))
        except ValueError:
            await message.answer("Неверная ссылка.", reply_markup=ReplyKeyboardRemove())
            return

        async with pool.acquire() as db:
            event = await db.fetchrow("SELECT * FROM events WHERE id=$1", event_id)
        if not event:
            await message.answer("Событие не найдено или удалено.", reply_markup=ReplyKeyboardRemove())
            return

        async with pool.acquire() as db:
            usr = await UserService.get_user(db, user.id)
            if usr and usr["onboarding_status"] not in ("completed", "skipped"):
                import hashlib as _hl
                token_hash = _hl.sha256(f"event_{event_id}".encode()).hexdigest()
                await db.execute(
                    "INSERT INTO pending_onboarding_actions "
                    "(user_id, action_type, token_hash, payload, expires_at) "
                    "VALUES ($1,'join_event',$2,$3,NOW() + INTERVAL '24 hours')",
                    user.id, token_hash, json.dumps({"event_id": event_id, "name": event["name"]}))
            else:
                was_new = not await db.fetchval(
                    "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2",
                    event_id, user.id)
                await db.execute(
                    "INSERT INTO collaborators (event_id, telegram_user_id, first_name, username, role) "
                    "VALUES ($1,$2,$3,$4,'collaborator') "
                    "ON CONFLICT (event_id, telegram_user_id) DO UPDATE SET first_name=EXCLUDED.first_name",
                    event_id, user.id, user.first_name, user.username or "")
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
            f"<b>{event['name']}</b>\n📅 {ev_date}\n\n"
            f"Нажмите кнопку, чтобы открыть ПОЛЯНУ:",
            reply_markup=kb)
        return

    # Check if user needs onboarding
    async with pool.acquire() as db:
        usr = await UserService.get_user(db, user.id)
    if usr and usr["onboarding_status"] not in ("completed", "skipped"):
        # New user — send rich welcome screen
        referrer_name = None
        if acquisition_source == "referral" and source_token:
            async with pool.acquire() as db:
                ref_uid = await db.fetchval(
                    "SELECT user_id FROM referral_codes WHERE code=$1",
                    source_token.replace("ref_", ""))
                if ref_uid:
                    ref_user = await db.fetchrow(
                        "SELECT first_name FROM users WHERE telegram_user_id=$1", ref_uid)
                    if ref_user:
                        referrer_name = ref_user["first_name"]

        if acquisition_source == "referral" and referrer_name:
            text = WelcomeService.build_referral_welcome(
                user.first_name, referrer_name, WELCOME_POINTS)
        elif acquisition_source == "recipe_share":
            # Try to get recipe title from pending action
            recipe_title = "рецепт"
            async with pool.acquire() as db:
                pending = await db.fetchrow(
                    "SELECT payload FROM pending_onboarding_actions "
                    "WHERE user_id=$1 AND action_type='save_recipe' AND completed_at IS NULL "
                    "ORDER BY created_at DESC LIMIT 1", user.id)
                if pending and pending.get("payload"):
                    try:
                        payload = json.loads(pending["payload"]) if isinstance(pending["payload"], str) else pending["payload"]
                        recipe_title = payload.get("name", "рецепт")
                    except Exception:
                        pass
            text = WelcomeService.build_recipe_share_welcome(
                user.first_name, "Друг", recipe_title)
        else:
            text = WelcomeService.build_new_user_welcome(
                user.first_name, acquisition_source, referrer_name, WELCOME_POINTS)

        kb = _welcome_keyboard() if FRONTEND_URL else None
        await message.answer(text, reply_markup=kb)
        # Mark welcome shown
        async with pool.acquire() as db:
            await UserService.update_onboarding_step(db, user.id, 0, "in_progress")
        await track(user.id, "welcome_screen_viewed",
                     props={"source": acquisition_source})
        return

    # Returning user — compact dashboard
    async with pool.acquire() as db:
        recipes_count = await db.fetchval(
            "SELECT COUNT(*) FROM recipes WHERE user_id=$1", user.id) or 0
        events_count = 0
        if FEATURE_EVENTS:
            events_count = await db.fetchval(
                "SELECT COUNT(*) FROM events WHERE telegram_user_id=$1", user.id) or 0
        wallet = await WalletService.get_balance(db, user.id)
    total_points = wallet.get("total_available_points", 0)

    text = WelcomeService.build_returning_user_dashboard(
        user.first_name, recipes_count, events_count, total_points, FEATURE_EVENTS)
    kb = _returning_user_keyboard() if FRONTEND_URL else None
    await message.answer(text, reply_markup=kb)


def _welcome_keyboard():
    """Keyboard for new user welcome screen."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌿 Открыть ПОЛЯНУ",
                              web_app=WebAppInfo(url=FRONTEND_URL))],
        [
            InlineKeyboardButton(text="📎 Как добавить рецепт",
                                  callback_data="ws_how_to_add"),
            InlineKeyboardButton(text="🍲 Посмотреть пример",
                                  callback_data="ws_example"),
        ],
        [
            InlineKeyboardButton(text="✨ AI-функции и баланс",
                                  callback_data="ws_ai_functions"),
            InlineKeyboardButton(text="🎁 Получить баллы",
                                  callback_data="ws_get_points"),
        ],
        [
            InlineKeyboardButton(text="📄 Документы",
                                  callback_data="show_documents"),
            InlineKeyboardButton(text="❓ Как всё работает",
                                  callback_data="ws_help"),
        ],
        [InlineKeyboardButton(text="📖 Показать, как пользоваться",
                              callback_data="ob_start_tutorial")],
        [InlineKeyboardButton(text="▶️ Начать сразу",
                              callback_data="ob_start_skip_to_legal")],
    ])


def _returning_user_keyboard():
    """Keyboard for returning user dashboard."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌿 Открыть ПОЛЯНУ",
                              web_app=WebAppInfo(url=FRONTEND_URL))],
        [
            InlineKeyboardButton(text="📎 Добавить рецепт",
                                  callback_data="ws_how_to_add"),
            InlineKeyboardButton(text="✨ AI-баланс",
                                  callback_data="ws_ai_functions"),
        ],
        [
            InlineKeyboardButton(text="🎁 Пригласить друзей",
                                  callback_data="ws_get_points"),
            InlineKeyboardButton(text="📄 Документы",
                                  callback_data="show_documents"),
        ],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="ws_help")],
    ])


@dp.callback_query(F.data == "show_ref")
async def cb_show_ref(callback: CallbackQuery):
    if pool is not None and callback.from_user and callback.message:
        await _send_referral(callback.message, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "show_terms")
async def cb_show_terms(callback: CallbackQuery):
    await cmd_terms(callback.message)
    await callback.answer()


# ── /documents command and callbacks ──────────────────────────────────────────

@dp.message(Command("documents"))
async def cmd_documents(message: Message):
    if not message.from_user or pool is None:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Пользовательское соглашение", callback_data="legal_doc:terms")],
        [InlineKeyboardButton(text="🔒 Политика конфиденциальности", callback_data="legal_doc:privacy_policy")],
        [InlineKeyboardButton(text="✅ Согласие на обработку данных", callback_data="legal_doc:personal_data_consent")],
        [InlineKeyboardButton(text="🤖 Использование ИИ", callback_data="legal_doc:ai_processing_consent")],
        [InlineKeyboardButton(text="🎁 Правила бонусной программы", callback_data="legal_doc:referral_terms")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="show_back")],
    ])
    await message.answer(
        "📄 <b>Документы ПОЛЯНЫ</b>\n\n"
        "Здесь можно ознакомиться с условиями использования сервиса, "
        "обработкой данных и правилами бонусной программы.",
        reply_markup=kb)


@dp.callback_query(F.data == "show_documents")
async def cb_show_documents(callback: CallbackQuery):
    if not callback.message:
        await callback.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Пользовательское соглашение", callback_data="legal_doc:terms")],
        [InlineKeyboardButton(text="🔒 Политика конфиденциальности", callback_data="legal_doc:privacy_policy")],
        [InlineKeyboardButton(text="✅ Согласие на обработку данных", callback_data="legal_doc:personal_data_consent")],
        [InlineKeyboardButton(text="🤖 Использование ИИ", callback_data="legal_doc:ai_processing_consent")],
        [InlineKeyboardButton(text="🎁 Правила бонусной программы", callback_data="legal_doc:referral_terms")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="show_back")],
    ])
    await callback.message.edit_text(
        "📄 <b>Документы ПОЛЯНЫ</b>\n\n"
        "Здесь можно ознакомиться с условиями использования сервиса, "
        "обработкой данных и правилами бонусной программы.",
        reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("legal_doc:"))
async def cb_legal_doc(callback: CallbackQuery):
    if not callback.from_user or not callback.message or pool is None:
        await callback.answer()
        return
    doc_type = callback.data.split(":", 1)[1]
    import legal_docs
    doc_title = legal_docs.DOCUMENT_TYPES.get(doc_type, doc_type)
    async with pool.acquire() as db:
        doc = await LegalConsentService.get_active_document(db, doc_type)
    if not doc:
        await callback.answer("Документ не найден", show_alert=True)
        return
    # Show document content (truncate if too long for Telegram 4096 limit)
    content = doc["content"]
    if len(content) > 3800:
        content = content[:3800] + "\n\n… (документ обрезан)"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К документам", callback_data="show_documents")],
    ])
    try:
        await callback.message.edit_text(content, reply_markup=kb)
    except Exception:
        # If edit fails (too long), send as new message
        await callback.message.answer(content, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "show_back")
async def cb_show_back(callback: CallbackQuery):
    if not callback.from_user:
        await callback.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌿 Открыть ПОЛЯНУ", web_app=WebAppInfo(url=FRONTEND_URL))],
        [
            InlineKeyboardButton(text="💰 Партнёрам", callback_data="show_ref"),
            InlineKeyboardButton(text="📄 Документы", callback_data="show_documents"),
        ],
    ]) if FRONTEND_URL else None
    if callback.message:
        try:
            await callback.message.edit_text(
                f"🌿 <b>Привет, {callback.from_user.first_name}!</b>\n\n"
                f"ПОЛЯНА — планировщик застолий с друзьями.\n\n"
                f"Откройте ПОЛЯНу кнопкой ниже 👇",
                reply_markup=kb)
        except Exception:
            pass
    await callback.answer()


# ── /privacy command ─────────────────────────────────────────────────────────

@dp.message(Command("privacy"))
async def cmd_privacy(message: Message):
    if not message.from_user or pool is None:
        return
    uid = message.from_user.id
    async with pool.acquire() as db:
        status = await LegalConsentService.get_user_acceptance_status(db, uid)
    terms_status = "✅ Принято" if status.get("terms", {}).get("accepted") else "⬜ Не принято"
    pdn_status = "✅ Принято" if status.get("personal_data_consent", {}).get("accepted") else "⬜ Не принято"
    ai_status = "✅ Принято" if status.get("ai_processing_consent", {}).get("accepted") else "⬜ Не принято"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Пользовательское соглашение", callback_data="legal_doc:terms")],
        [InlineKeyboardButton(text="🔒 Политика конфиденциальности", callback_data="legal_doc:privacy_policy")],
        [InlineKeyboardButton(text="🤖 Использование ИИ", callback_data="legal_doc:ai_processing_consent")],
        [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data="del_start")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="show_back")],
    ])
    await message.answer(
        f"🔒 <b>Данные и конфиденциальность</b>\n\n"
        f"Статус согласий:\n"
        f"  Пользовательское соглашение: {terms_status}\n"
        f"  Обработка персональных данных: {pdn_status}\n"
        f"  Использование ИИ: {ai_status}\n\n"
        f"Подробнее: /documents",
        reply_markup=kb)


# ── /help command ────────────────────────────────────────────────────────────

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📚 <b>Как пользоваться ПОЛЯНОЙ</b>\n\n"
        "<b>Добавление рецептов:</b>\n"
        "• 📸 Фото или скриншот рецепта\n"
        "• 🔗 Ссылка на сайт с рецептом\n"
        "• 📝 Текст рецепта\n"
        "• 🎙 Голосовое сообщение\n"
        "• /add — добавление вручную\n\n"
        "<b>Библиотека:</b>\n"
        "Откройте ПОЛЯНу кнопкой внизу → вкладка «Рецепты»\n\n"
        "<b>Совместные события:</b>\n"
        "Создайте событие, пригласите друзей, составьте меню вместе\n\n"
        "<b>Команды:</b>\n"
        "/start — главное меню\n"
        "/add — добавить рецепт\n"
        "/ref — партнёрская программа\n"
        "/documents — юридические документы\n"
        "/privacy — данные и конфиденциальность\n"
        "/help — эта справка"
    )


# ── /delete_me command ───────────────────────────────────────────────────────

@dp.message(Command("delete_me"))
async def cmd_delete_me(message: Message):
    if not message.from_user or pool is None:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Нет, оставить аккаунт", callback_data="del_cancel")],
        [InlineKeyboardButton(text="⚠️ Продолжить удаление", callback_data="del_confirm1")],
    ])
    await message.answer(
        "🗑 <b>Удаление аккаунта</b>\n\n"
        "Вы действительно хотите удалить аккаунт?\n\n"
        "Будут удалены:\n"
        "— ваша библиотека рецептов\n"
        "— события\n"
        "— списки покупок\n"
        "— история использования\n"
        "— доступный баланс в пределах правил сервиса\n\n"
        "Финансовые записи могут сохраняться в обезличенном виде.",
        reply_markup=kb)


@dp.callback_query(F.data == "del_cancel")
async def cb_del_cancel(callback: CallbackQuery):
    if callback.message:
        try:
            await callback.message.edit_text("✅ Аккаунт сохранён.", reply_markup=None)
        except Exception:
            pass
    await callback.answer()


@dp.callback_query(F.data == "del_start")
async def cb_del_start(callback: CallbackQuery):
    """Entry point from privacy screen."""
    if not callback.from_user:
        await callback.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Нет, оставить аккаунт", callback_data="del_cancel")],
        [InlineKeyboardButton(text="⚠️ Продолжить удаление", callback_data="del_confirm1")],
    ])
    if callback.message:
        try:
            await callback.message.edit_text(
                "🗑 <b>Удаление аккаунта</b>\n\n"
                "Вы действительно хотите удалить аккаунт?\n\n"
                "Будут удалены:\n"
                "— ваша библиотека рецептов\n"
                "— события\n"
                "— списки покупок\n"
                "— история использования\n"
                "— доступный баланс в пределах правил сервиса\n\n"
                "Финансовые записи могут сохраняться в обезличенном виде.",
                reply_markup=kb)
        except Exception:
            pass
    await callback.answer()


@dp.callback_query(F.data == "del_confirm1")
async def cb_del_confirm1(callback: CallbackQuery):
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Нет, оставить аккаунт", callback_data="del_cancel")],
        [InlineKeyboardButton(text="🗑 Удалить навсегда", callback_data="del_execute")],
    ])
    if callback.message:
        try:
            await callback.message.edit_text(
                "⚠️ <b>Подтвердите удаление</b>\n\n"
                "Для удаления нажмите «Удалить навсегда».\n"
                "Это действие необратимо.",
                reply_markup=kb)
        except Exception:
            pass
    await callback.answer()


@dp.callback_query(F.data == "del_execute")
async def cb_del_execute(callback: CallbackQuery):
    if not callback.from_user or pool is None:
        await callback.answer()
        return
    uid = callback.from_user.id
    try:
        async with pool.acquire() as db:
            await UserService.delete_user(db, uid)
        await asyncio.create_task(track(uid, "account_deleted"))
        if callback.message:
            try:
                await callback.message.edit_text(
                    "✅ <b>Аккаунт удалён</b>\n\n"
                    "Ваши данные были удалены. Если захотите вернуться — "
                    "просто отправьте /start.", reply_markup=None)
            except Exception:
                pass
    except Exception as e:
        log.exception("account deletion failed: %s", e)
        if callback.message:
            try:
                await callback.message.edit_text("❌ Произошла ошибка при удалении. Попробуйте позже.")
            except Exception:
                pass
    await callback.answer()


async def run_bot():
    try:
        # Register consent middleware
        dp.message.middleware(LegalConsentMiddleware())
        dp.callback_query.middleware(LegalConsentMiddleware())

        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="ПОЛЯНА", web_app=WebAppInfo(url=FRONTEND_URL))
        )
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="add", description="Добавить рецепт в библиотеку"),
                BotCommand(command="split", description="Делёж расходов"),
                BotCommand(command="ref", description="Партнёрская программа"),
                BotCommand(command="terms", description="Правила бонусной программы"),
                BotCommand(command="documents", description="Юридические документы"),
                BotCommand(command="privacy", description="Данные и конфиденциальность"),
                BotCommand(command="help", description="Как пользоваться ПОЛЯНОЙ"),
                BotCommand(command="delete_me", description="Удалить аккаунт"),
                BotCommand(command="balance", description="AI-баланс и пополнение"),
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )
        log.info("Bot polling...")
        await dp.start_polling(bot, skip_updates=True)
    except Exception as e:
        log.error("Bot error: %s", e)


async def _bg_init():
    """Run DB migrations and start bot in background so FastAPI starts immediately."""
    global _db_error
    try:
        await init_db()
    except asyncio.TimeoutError:
        _db_error = "DB connection timed out after 30s"
        log.error(_db_error)
    except Exception as e:
        _db_error = f"{type(e).__name__}: {e}"
        log.error("init_db error: %s", e)
    # Start bot regardless
    asyncio.create_task(run_bot())
    # Referral maturation loop: activate pending rewards
    if REFERRAL_ENABLED:
        asyncio.create_task(_referral_maturation_loop())
    # OpenRouter low-balance monitor stays: recipe text/photo/voice parsing still uses it.
    asyncio.create_task(_openrouter_balance_loop())
    asyncio.create_task(_payment_reconciliation_loop())


@app.on_event("startup")
async def startup():
    log.info("FastAPI starting on port %d", PORT)
    asyncio.create_task(_bg_init())  # non-blocking: /health responds immediately


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
