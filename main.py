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
)
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("polyana")

ENV = os.environ.get
BOT_TOKEN = ENV("BOT_TOKEN", "")
DATABASE_URL = ENV("DATABASE_URL", "")
FRONTEND_URL = ENV("FRONTEND_URL", "")
INTERNAL_API_KEY = ENV("INTERNAL_API_KEY", "")
PORT = int(ENV("PORT", "8000"))
OPENROUTER_KEY = ENV("OPENROUTER_API_KEY", "")

pool = None
_db_ready = False
_db_error: str | None = None


async def get_db():
    if pool is None:
        raise HTTPException(503, "Сервис запускается, попробуйте через секунду")
    async with pool.acquire() as c:
        yield c


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
  "servings": 4,
  "cook_time_minutes": 90,
  "category": "ужин",
  "original_language": "ru",
  "ingredients": [{"name": "Свинина шейка", "qty": 1.5, "unit": "кг"}],
  "steps": [{"text": "Нарезать мясо кусками по 4-5 см"}]
}

category: завтрак|обед|ужин|десерт|суп|салат|закуска|напиток|выпечка|другое
unit: г/кг/мл/л/шт/ст.л/ч.л/щепотка/по вкусу
qty: только число (1.5, 200, 3)
Переведи название на русский если оригинал не русский."""

_or_client = None


def _get_or_client():
    global _or_client
    if _or_client is None:
        if not OPENROUTER_KEY:
            raise RuntimeError("OPENROUTER_API_KEY не задан в env")
        from openai import AsyncOpenAI
        _or_client = AsyncOpenAI(
            api_key=OPENROUTER_KEY,
            base_url="https://openrouter.ai/api/v1",
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


async def _llm_parse_image(image_bytes: bytes) -> dict | None:
    """Parse recipe from image using Qwen 2.5 VL via OpenRouter."""
    import base64
    client = _get_or_client()
    b64 = base64.b64encode(image_bytes).decode()
    resp = await client.chat.completions.create(
        model="qwen/qwen2.5-vl-72b-instruct",
        messages=[
            {"role": "system", "content": RECIPE_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": "Это изображение рецепта. Извлеки и верни JSON."},
            ]},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=2500,
    )
    raw = resp.choices[0].message.content
    data = json.loads(raw)
    if data.get("not_a_recipe"):
        return None
    data["source_type"] = "photo"
    return data


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


async def _fetch_page_html(url: str) -> str:
    """Fetch raw HTML with browser-like headers. Raises httpx.HTTPStatusError on failure."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url, headers=_BROWSER_HEADERS)
        resp.raise_for_status()
    return resp.text


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
            raw_yield = str(scraper.yields() or "4")
            servings = int(re.search(r'\d+', raw_yield).group()) if re.search(r'\d+', raw_yield) else 4
        except Exception:
            servings = 4
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
                int(parsed["servings"]) if parsed.get("servings") else 4,
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
async def migration_check(db=Depends(get_db)):
    """Structural migration verification — returns only counts/metadata, no user data."""
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

    return {
        "блок1_recipes_without_user": recipes_without_user,
        "блок1_recipes_user_id_zero": recipes_with_zero,
        "блок1_priority_sample": [dict(r) for r in priority_rows],
        "блок1_event_recipes_duplicates": dup_count,
        "блок4_indexes_on_recipes": [{"name": r["indexname"], "def": r["indexdef"]} for r in indexes],
        "блок4_constraints_on_recipes": [{"name": r["conname"], "type": r["contype"], "def": r["def"]} for r in constraints],
        "event_recipes_columns": [r["column_name"] for r in er_columns],
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
    if not await db.fetchrow("SELECT id FROM events WHERE id=$1", event_id):
        raise HTTPException(404, "Not found")
    await db.execute(
        """
        INSERT INTO collaborators (event_id, telegram_user_id, first_name, username, role)
        VALUES ($1,$2,$3,$4,'collaborator')
        ON CONFLICT (event_id, telegram_user_id) DO UPDATE SET first_name=EXCLUDED.first_name
        """,
        event_id, user_id, body.get("first_name", ""), body.get("username", ""),
    )
    return {"status": "joined", "role": "collaborator"}


# ── Shopping list helpers ─────────────────────────────────────────────────────

def _fmt_qty(qty: float | None) -> str:
    """Format a float quantity to a clean string (1.5 → '1.5', 2.0 → '2')."""
    if qty is None or qty == 0:
        return ""
    if qty == int(qty):
        return str(int(qty))
    return f"{qty:.2f}".rstrip("0").rstrip(".")


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

    # Aggregate by (lower_name, unit) — sum qty × multiplier
    agg: dict[tuple, dict] = {}
    for row in rows:
        raw_name = (row["name"] or "").strip()
        if not raw_name:
            continue  # skip ingredients with empty/NULL name — never crash the list
        key = (raw_name.lower(), (row["unit"] or "").strip().lower())
        try:
            mult = float(row["servings_multiplier"] or 1.0)
        except (TypeError, ValueError):
            mult = 1.0
        try:
            qty = (float(row["qty"]) if row["qty"] else 0.0) * mult
        except (TypeError, ValueError):
            qty = 0.0
        if key in agg:
            agg[key]["qty"] = (agg[key]["qty"] or 0.0) + qty
        else:
            agg[key] = {
                "name": raw_name,
                "qty": qty,
                "unit": (row["unit"] or "").strip(),
                "category": row["category"] or "прочее",
            }

    # Preserve "bought" state across regeneration (key by lower name + unit)
    prev = await db.fetch(
        "SELECT name, unit, bought FROM shopping_items WHERE event_id=$1 AND is_generated=TRUE",
        event_id,
    )
    bought_state = {
        ((p["name"] or "").strip().lower(), (p["unit"] or "").strip().lower()): p["bought"]
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
        qty_val = item["qty"] if item["qty"] > 0 else None
        qty_str = _fmt_qty(qty_val)
        display_qty = f"{qty_str} {item['unit']}".strip() if qty_str else (item["unit"] or None)
        was_bought = bought_state.get(key, False)
        try:
            await db.execute(
                """
                INSERT INTO shopping_items (event_id, name, quantity, qty, unit, category, is_generated, bought)
                VALUES ($1,$2,$3,$4,$5,$6,TRUE,$7)
                """,
                event_id, item["name"], display_qty, qty_val, item["unit"], item["category"], was_bought,
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
        raise HTTPException(502, f"Не удалось сгенерировать фон: {msg}")
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

    theme = invite.theme_or_default((body.get("theme") or "").strip())
    mode = (body.get("mode") or "free").strip()
    date_str, time_str = _fmt_event_dt(ev["event_date"])
    evt = {
        "name": ev["name"],
        "date_str": date_str,
        "time_str": time_str,
        "place": ev["location"],
        "host_name": (body.get("host_name") or "").strip() or None,
    }

    if mode == "ai":
        # NOTE: billing/balance gating is added in the payments step. For now AI
        # mode runs directly (and fails gracefully if OpenRouter has no credits).
        bg = await _openrouter_background(invite.THEMES[theme]["prompt"])
        png = invite.render_on_background(bg, evt, theme)
    else:
        png = invite.render_typographic(evt, theme)

    return {
        "image": "data:image/png;base64," + base64.b64encode(png).decode(),
        "mode": mode,
        "theme": theme,
    }


@app.get("/api/invite/themes")
async def list_invite_themes(user_id: int = Depends(get_current_user)):
    return [{"key": k, "title": k.capitalize()} for k in invite.THEMES.keys()]


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
    body = (
        f"{header}\n\n"
        f"{recipe['emoji']} <b>{recipe['name']}</b>\n"
        f"{cat_str}🍽 {recipe['servings']} порц. · {ct_str}"
        f"🥕 {recipe['ingredients_count']} ингр."
    )
    recipe_url = f"{FRONTEND_URL}?screen=recipe&id={recipe['id']}"
    add_url = f"{FRONTEND_URL}?screen=add_to_event&recipe_id={recipe['id']}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📖 Открыть", web_app=WebAppInfo(url=recipe_url)),
        InlineKeyboardButton(text="📅 В событие", web_app=WebAppInfo(url=add_url)),
    ]])
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
    else:
        log.error("parse error (%s): %s", hint, err)
        await status_msg.edit_text(f"❌ Ошибка при разборе.\n<code>{msg[:300]}</code>")


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

@dp.message(F.photo)
async def handle_photo_message(message: Message):
    if not message.from_user or pool is None:
        return
    status = await message.reply("⏳ Читаю рецепт с фото...")
    try:
        photo = message.photo[-1]   # largest size
        file = await bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        recipe = await parse_and_save_recipe(
            message.from_user.id, image_bytes=buf.getvalue(), image_file_id=photo.file_id
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


# ── /start command ────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not message.from_user:
        return
    user = message.from_user
    text = message.text or ""
    arg = text.split(maxsplit=1)[1] if " " in text else None

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
            await db.execute(
                """
                INSERT INTO collaborators (event_id, telegram_user_id, first_name, username, role)
                VALUES ($1,$2,$3,$4,'collaborator')
                ON CONFLICT (event_id, telegram_user_id) DO UPDATE SET first_name=EXCLUDED.first_name
                """,
                event_id, user.id, user.first_name, user.username or "",
            )

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
        await message.answer(
            f"🌿 <b>Привет, {user.first_name}!</b>\n\n"
            f"ПОЛЯНА — планировщик застолий с друзьями.\n\n"
            f"<b>Как добавить рецепт в библиотеку:</b>\n"
            f"• 🔗 Пришли ссылку на рецепт\n"
            f"• 📸 Фото рецепта из книги или экрана\n"
            f"• 🎙 Голосовое сообщение\n"
            f"• 📝 Текст рецепта\n"
            f"• /add — явный режим добавления\n\n"
            f"Откройте ПОЛЯНУ кнопкой внизу экрана 👇",
            reply_markup=ReplyKeyboardRemove(),
        )


async def run_bot():
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="ПОЛЯНА", web_app=WebAppInfo(url=FRONTEND_URL))
        )
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="add", description="Добавить рецепт в библиотеку"),
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


@app.on_event("startup")
async def startup():
    log.info("FastAPI starting on port %d", PORT)
    asyncio.create_task(_bg_init())  # non-blocking: /health responds immediately


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
