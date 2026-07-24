import os, hashlib, hmac, json, asyncio, secrets, time, logging, io, re, base64
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

# Referral program
REFERRAL_PERCENT = 10        # % of a referee's spend credited to the referrer
REFERRAL_HOLD_HOURS = 24     # delay before a bonus matures (chargeback protection)

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

        # ── Migration L: referrals ────────────────────────────────────────────
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
        raise ValueError("Не удалось распознать рецепт в этом контенте")

    return await _save_parsed_recipe(user_id, parsed)


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


# ── Share link & join ─────────────────────────────────────────────────────────

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
    """If the referee was referred, schedule a matured-in-24h bonus for the referrer."""
    referrer = await db.fetchval(
        "SELECT referrer_id FROM referrals WHERE referee_id=$1", referee_id
    )
    if not referrer or referrer == referee_id:
        return
    bonus = spend * REFERRAL_PERCENT // 100
    if bonus <= 0:
        return
    await db.execute(
        """
        INSERT INTO referral_bonuses (referrer_id, referee_id, source_ref, amount, available_at)
        VALUES ($1,$2,$3,$4, NOW() + ($5 || ' hours')::interval)
        ON CONFLICT (source_ref) DO NOTHING
        """,
        referrer, referee_id, source_ref, bonus, str(REFERRAL_HOLD_HOURS),
    )


async def _referral_maturation_loop():
    """Credit matured referral bonuses to referrers. Runs every 10 minutes."""
    while True:
        try:
            if pool is not None:
                async with pool.acquire() as db:
                    rows = await db.fetch(
                        "SELECT id, referrer_id, amount FROM referral_bonuses "
                        "WHERE NOT paid AND available_at <= NOW() LIMIT 200"
                    )
                    for r in rows:
                        await _credit(db, r["referrer_id"], r["amount"], "referral_bonus",
                                      ref=f"refbonus:{r['id']}")
                        await db.execute(
                            "UPDATE referral_bonuses SET paid=TRUE WHERE id=$1", r["id"])
                        try:
                            await bot.send_message(
                                r["referrer_id"],
                                f"💰 Реферальный бонус +{int(r['amount']/100)} ₽ зачислен на баланс!")
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


@app.get("/api/referral")
async def referral_info(user_id: int = Depends(get_current_user), db=Depends(get_db)):
    username = await _get_bot_username()
    link = f"https://t.me/{username}?start=ref_{user_id}" if username else ""
    invited = await db.fetchval(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id=$1", user_id) or 0
    earned = await db.fetchval(
        "SELECT COALESCE(SUM(amount),0) FROM referral_bonuses WHERE referrer_id=$1 AND paid", user_id) or 0
    pending = await db.fetchval(
        "SELECT COALESCE(SUM(amount),0) FROM referral_bonuses WHERE referrer_id=$1 AND NOT paid", user_id) or 0
    return {
        "link": link,
        "percent": REFERRAL_PERCENT,
        "invited": invited,
        "earned_rub": round(earned / 100, 2),
        "pending_rub": round(pending / 100, 2),
    }


# ── YooKassa top-up ───────────────────────────────────────────────────────────

_TOPUP_AMOUNTS = {100, 200, 500, 1000}   # rubles (minimum top-up 100 ₽)


# Top-up DISABLED: no paid feature currently consumes balance, so accepting money
# would be money-in / nothing-out. Endpoints kept (410) so old clients fail cleanly.
@app.post("/api/balance/topup")
async def create_topup(body: dict, user_id: int = Depends(get_current_user)):
    raise HTTPException(410, "Пополнение временно отключено")


@app.post("/api/balance/topup-stars")
async def create_topup_stars(body: dict, user_id: int = Depends(get_current_user)):
    raise HTTPException(410, "Пополнение временно отключено")


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
        await track(uid, "payment_succeeded",
                    props={"kopecks": kopecks, "method": "yookassa", "ref": pid})
        try:
            await bot.send_message(
                uid, f"✅ Баланс пополнен на {int(kopecks/100)} ₽.\nТекущий баланс: {int(new_bal/100)} ₽"
            )
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


# ── Bot ───────────────────────────────────────────────────────────────────────

# FSM states for voice recipe editing flow
class VoiceStates(StatesGroup):
    editing = State()   # User is typing a corrected transcript

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


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
    lines = [f"{rec['emoji'] or '🍽'} <b>{rec['name']}</b>"]
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
        lines.append(f"\n🥄 <b>Ингредиенты ({len(ings)}):</b>")
        for ing in ings:
            qty = f"{fmtIngQty(ing['qty'])} {ing['unit'] or ''}".strip() if ing['qty'] else ""
            lines.append(f"  • {ing['name']}" + (f" — {qty}" if qty else ""))
    if steps:
        lines.append(f"\n📋 <b>Приготовление:</b>")
        for s in steps:
            lines.append(f"  {s['step_number']}. {s['text']}")
    lines.append(f"\n🌿 Рецепт из ПОЛЯНЫ")
    return "\n".join(lines)


@dp.callback_query(F.data.startswith("share_recipe_"))
async def handle_share_recipe(callback: CallbackQuery):
    if not callback.from_user:
        await callback.answer()
        return
    try:
        recipe_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка", show_alert=True)
        return
    text = await _format_recipe_for_share(recipe_id)
    if not text:
        await callback.answer("Рецепт не найден", show_alert=True)
        return
    # Send the full recipe text to the user so they can forward it
    await callback.message.answer(text)
    await callback.answer()

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
    async with pool.acquire() as db:
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
    if not message.from_user or pool is None:
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
    except ValueError:
        try:
            await status.delete()   # silently drop non-recipe text
        except Exception:
            pass
    except Exception as e:
        await _reply_parse_error(status, e, "рецепт")


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
    if not message.from_user or pool is None:
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
    if not payload.startswith("topup:") or pool is None:
        return
    try:
        _, uid_s, rub_s = payload.split(":")
        uid = int(uid_s)
        kopecks = int(rub_s) * 100
    except Exception:
        log.warning("bad stars payload: %s", payload)
        return
    charge_id = sp.telegram_payment_charge_id   # idempotency key
    async with pool.acquire() as db:
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
    if not message.from_user:
        return
    user = message.from_user
    text = message.text or ""
    arg = text.split(maxsplit=1)[1] if " " in text else None

    # Analytics: top-of-funnel + attribution source (ref_<id> / event_<id> / organic)
    await track(user.id, "user_start", src_payload=(arg or "organic"))

    # Referral capture: ?start=ref_<referrer_id> (only for a brand-new referee)
    if arg and arg.startswith("ref_") and pool is not None:
        try:
            referrer_id = int(arg.replace("ref_", ""))
        except ValueError:
            referrer_id = 0
        if referrer_id and referrer_id != user.id:
            try:
                async with pool.acquire() as db:
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

        if pool is None:
            await message.answer("Сервис запускается, попробуйте через минуту.", reply_markup=ReplyKeyboardRemove())
            return

        async with pool.acquire() as db:
            event = await db.fetchrow("SELECT * FROM events WHERE id=$1", event_id)

        if not event:
            await message.answer("Событие не найдено или удалено.", reply_markup=ReplyKeyboardRemove())
            return

        async with pool.acquire() as db:
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
            f"ПОЛЯНА — планировщик застолий с друзьями.\n\n"
            f"<b>Как добавить рецепт в библиотеку:</b>\n"
            f"• 🔗 Пришли ссылку на рецепт\n"
            f"• 📸 Фото рецепта из книги или экрана\n"
            f"• 🎙 Голосовое сообщение\n"
            f"• 📝 Текст рецепта\n"
            f"• /add — явный режим добавления\n\n"
            f"💰 /ref — партнёрская программа · 📄 /terms — правила\n\n"
            f"Откройте ПОЛЯНУ кнопкой ниже 👇",
            reply_markup=kb,
        )


@dp.callback_query(F.data == "show_ref")
async def cb_show_ref(callback: CallbackQuery):
    if pool is not None and callback.from_user and callback.message:
        await _send_referral(callback.message, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data == "show_terms")
async def cb_show_terms(callback: CallbackQuery):
    await cmd_terms(callback.message)
    await callback.answer()


async def run_bot():
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="ПОЛЯНА", web_app=WebAppInfo(url=FRONTEND_URL))
        )
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="add", description="Добавить рецепт в библиотеку"),
                BotCommand(command="split", description="Делёж расходов"),
                BotCommand(command="ref", description="Партнёрская программа"),
                BotCommand(command="terms", description="Правила и документы"),
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
    # Referral maturation loop disabled — referral bonuses aren't spendable now.
    # OpenRouter low-balance monitor stays: recipe text/photo/voice parsing still uses it.
    asyncio.create_task(_openrouter_balance_loop())


@app.on_event("startup")
async def startup():
    log.info("FastAPI starting on port %d", PORT)
    asyncio.create_task(_bg_init())  # non-blocking: /health responds immediately


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
