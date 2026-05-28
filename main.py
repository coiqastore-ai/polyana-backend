import os, hashlib, hmac, json, asyncio, secrets, time, logging, io, re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl
import asyncpg
from fastapi import FastAPI, HTTPException, Header, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    BotCommand, BotCommandScopeAllPrivateChats,
    MenuButtonWebApp, Message,
    ReplyKeyboardRemove, WebAppInfo,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.client.default import DefaultBotProperties

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("polyana")

ENV = os.environ.get
BOT_TOKEN = ENV("BOT_TOKEN", "")
DATABASE_URL = ENV("DATABASE_URL", "")
FRONTEND_URL = ENV("FRONTEND_URL", "")
INTERNAL_API_KEY = ENV("INTERNAL_API_KEY", "")
PORT = int(ENV("PORT", "8000"))
OPENROUTER_KEY = ENV("OPENROUTER_API_KEY", "")
GROQ_API_KEY = ENV("GROQ_API_KEY", "")

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
        asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=60),
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
_groq_client = None


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


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY не задан в env")
        from openai import AsyncOpenAI
        _groq_client = AsyncOpenAI(
            api_key=GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
        )
    return _groq_client


async def _llm_parse_text(text: str, source_type: str = "text") -> dict | None:
    """Parse recipe from text using Gemini 2.5 Flash via OpenRouter."""
    client = _get_or_client()
    resp = await client.chat.completions.create(
        model="google/gemini-2.5-flash-preview-05-20",
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


async def _transcribe_voice(audio_bytes: bytes) -> str:
    """Transcribe voice via Groq Whisper large-v3."""
    client = _get_groq_client()
    resp = await client.audio.transcriptions.create(
        model="whisper-large-v3",
        file=("voice.ogg", io.BytesIO(audio_bytes), "audio/ogg"),
        language="ru",
    )
    return resp.text


def _try_recipe_scraper(url: str) -> dict | None:
    """Try recipe-scrapers for 300+ known sites. Returns None for unsupported sites."""
    try:
        from recipe_scrapers import scrape_me
        scraper = scrape_me(url)
        try:
            raw_yield = str(scraper.yields() or "4")
            servings = int(re.search(r'\d+', raw_yield).group()) if re.search(r'\d+', raw_yield) else 4
        except Exception:
            servings = 4
        ingredients = []
        for ing_str in (scraper.ingredients() or []):
            ingredients.append({"name": ing_str.strip(), "qty": None, "unit": ""})
        steps = []
        try:
            for s in (scraper.instructions_list() or []):
                if s.strip():
                    steps.append({"text": s.strip()})
        except Exception:
            raw = scraper.instructions()
            if raw:
                steps = [{"text": raw.strip()}]
        return {
            "name": scraper.title() or "Рецепт",
            "servings": servings,
            "cook_time_minutes": scraper.total_time() or None,
            "ingredients": ingredients,
            "steps": steps,
            "source_type": "url",
        }
    except Exception:
        return None


async def _fetch_page_text(url: str) -> str:
    """Fetch URL and extract readable text for LLM."""
    from bs4 import BeautifulSoup
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; RecipeBot/1.0)"})
        resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    return text[:8000]


async def parse_and_save_recipe(
    user_id: int,
    *,
    url: str | None = None,
    text: str | None = None,
    image_bytes: bytes | None = None,
    audio_bytes: bytes | None = None,
) -> dict:
    """Full pipeline: detect type → LLM parse → save to DB. Returns saved recipe dict."""
    parsed: dict | None = None

    if url:
        # 1. Try recipe-scrapers (fast, no LLM)
        parsed = _try_recipe_scraper(url)
        if parsed is None:
            # 2. Fetch HTML + LLM fallback
            page_text = await _fetch_page_text(url)
            parsed = await _llm_parse_text(page_text, source_type="url")
        if parsed:
            parsed["source_url"] = url
            parsed.setdefault("source_type", "url")

    elif image_bytes:
        parsed = await _llm_parse_image(image_bytes)

    elif audio_bytes:
        transcript = await _transcribe_voice(audio_bytes)
        log.info("Voice transcript: %s", transcript[:200])
        parsed = await _llm_parse_text(transcript, source_type="voice")

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
                     original_language, servings, cook_time_minutes, category)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
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
    if owner != user_id:
        raise HTTPException(403, "Access denied")
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


# ── Bot ───────────────────────────────────────────────────────────────────────

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
    if "not_a_recipe" in msg or "Не удалось распознать" in msg:
        await status_msg.edit_text(f"🤷 Не смог найти {hint} в этом контенте.\nПришли ссылку или команду /add")
    else:
        log.error("parse error: %s", err)
        await status_msg.edit_text(f"❌ Ошибка при разборе.\n<code>{msg[:200]}</code>")


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


# ── Text / URL handler ────────────────────────────────────────────────────────

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text_message(message: Message):
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

    # Plain text — only try if it's long enough to be a recipe
    if len(text) < 100:
        return   # too short, silently ignore

    status = await message.reply("⏳ Разбираю рецепт...")
    try:
        recipe = await parse_and_save_recipe(message.from_user.id, text=text)
        await _reply_recipe_saved(message, recipe, status)
    except ValueError:
        await status.delete()   # silently drop non-recipe text
    except Exception as e:
        await _reply_parse_error(status, e, "рецепт")


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
        recipe = await parse_and_save_recipe(message.from_user.id, image_bytes=buf.getvalue())
        await _reply_recipe_saved(message, recipe, status)
    except Exception as e:
        await _reply_parse_error(status, e, "рецепт на фото")


# ── Voice handler ─────────────────────────────────────────────────────────────

@dp.message(F.voice)
async def handle_voice_message(message: Message):
    if not message.from_user or pool is None:
        return
    status = await message.reply("⏳ Слушаю и распознаю...")
    try:
        file = await bot.get_file(message.voice.file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        recipe = await parse_and_save_recipe(message.from_user.id, audio_bytes=buf.getvalue())
        await _reply_recipe_saved(message, recipe, status)
    except Exception as e:
        await _reply_parse_error(status, e, "рецепт из голосового")


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
