import os, hashlib, hmac, json, asyncio, secrets, time, logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl
import asyncpg
from fastapi import FastAPI, HTTPException, Header, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import (
    BotCommand, BotCommandScopeAllPrivateChats,
    KeyboardButton, MenuButtonWebApp, Message,
    ReplyKeyboardMarkup, WebAppInfo,
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

pool = None


async def get_db():
    async with pool.acquire() as c:
        yield c


async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as c:
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

            CREATE TABLE IF NOT EXISTS event_menu_items (
                id               SERIAL PRIMARY KEY,
                event_id         INT REFERENCES events(id) ON DELETE CASCADE,
                name             TEXT NOT NULL,
                emoji            TEXT DEFAULT '🍽',
                servings         INT DEFAULT 4,
                added_by_user_id BIGINT,
                added_by_name    TEXT,
                added_at         TIMESTAMPTZ DEFAULT NOW()
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

        # Migrate old schema if needed (title->name, date->event_date)
        await c.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='events' AND column_name='title'
                ) AND NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='events' AND column_name='name'
                ) THEN
                    ALTER TABLE events RENAME COLUMN title TO name;
                END IF;

                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='events' AND column_name='date'
                ) AND NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='events' AND column_name='event_date'
                ) THEN
                    ALTER TABLE events RENAME COLUMN date TO event_date;
                END IF;

                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='share_token')
                    THEN ALTER TABLE events ADD COLUMN share_token TEXT UNIQUE; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='location')
                    THEN ALTER TABLE events ADD COLUMN location TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='description')
                    THEN ALTER TABLE events ADD COLUMN description TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='template')
                    THEN ALTER TABLE events ADD COLUMN template TEXT; END IF;
            END $$;
        """)

        # Backfill share_token for existing events
        rows = await c.fetch("SELECT id FROM events WHERE share_token IS NULL")
        for row in rows:
            await c.execute(
                "UPDATE events SET share_token=$1 WHERE id=$2",
                secrets.token_urlsafe(16), row["id"]
            )

    log.info("DB ready")


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
    db=Depends(get_db),
) -> int:
    if not x_telegram_init_data:
        raise HTTPException(401, "Missing initData")
    user = validate_init_data(x_telegram_init_data)
    if not user or "id" not in user:
        raise HTTPException(401, "Invalid initData")
    return int(user["id"])


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="ПОЛЯНА API", version="2.0")

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
    return {"status": "ok", "service": "ПОЛЯНА API v2"}


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
        SELECT e.id, e.name, e.event_date, e.location, e.share_token, e.telegram_user_id,
               (SELECT COUNT(*) FROM event_menu_items m WHERE m.event_id = e.id) AS recipes_count,
               (SELECT COUNT(*) FROM shopping_items s WHERE s.event_id = e.id) AS shopping_total,
               (SELECT COUNT(*) FROM shopping_items s WHERE s.event_id = e.id AND s.bought) AS shopping_bought,
               (SELECT COUNT(*) FROM collaborators c WHERE c.event_id = e.id) AS collab_count
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
async def create_event(
    body: dict,
    user_id: int = Depends(get_current_user),
    db=Depends(get_db),
):
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
    # owner as collaborator
    await db.execute(
        """
        INSERT INTO collaborators (event_id, telegram_user_id, first_name, username, role)
        VALUES ($1,$2,$3,$4,'owner') ON CONFLICT DO NOTHING
        """,
        row["id"], user_id,
        body.get("owner_first_name", ""), body.get("owner_username", ""),
    )
    return {"id": row["id"], "name": row["name"], "share_token": row["share_token"], "owner_id": user_id}


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
    recipes = await db.fetch(
        "SELECT * FROM event_menu_items WHERE event_id=$1 ORDER BY added_at ASC", event_id
    )
    shop_row = await db.fetchrow(
        "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE bought) AS bought FROM shopping_items WHERE event_id=$1",
        event_id,
    )
    rc, st, sb = len(recipes), (shop_row["total"] or 0), (shop_row["bought"] or 0)

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
                "recipes_count": sum(1 for r in recipes if r["added_by_user_id"] == c["telegram_user_id"]),
            }
            for c in collabs
        ],
        "recipes": [
            {
                "id": r["id"], "name": r["name"], "emoji": r["emoji"] or "🍽",
                "servings": r["servings"],
                "added_by": {"user_id": r["added_by_user_id"], "first_name": r["added_by_name"] or "Гость"},
                "added_at": r["added_at"].isoformat() if r["added_at"] else None,
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


# ── Recipes (menu items) ──────────────────────────────────────────────────────

@app.post("/api/events/{event_id}/recipes", status_code=201)
async def add_recipe(event_id: int, body: dict, user_id: int = Depends(get_current_user), db=Depends(get_db)):
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
    rec = await db.fetchrow(
        "INSERT INTO event_menu_items (event_id,name,emoji,servings,added_by_user_id,added_by_name) "
        "VALUES ($1,$2,$3,$4,$5,$6) RETURNING *",
        event_id, name, body.get("emoji", "🍽"), body.get("servings", 4),
        user_id, body.get("added_by_name", ""),
    )
    return {"id": rec["id"], "name": rec["name"], "emoji": rec["emoji"],
            "servings": rec["servings"], "added_at": rec["added_at"].isoformat()}


@app.delete("/api/events/{event_id}/recipes/{recipe_id}", status_code=204)
async def delete_recipe(event_id: int, recipe_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    rec = await db.fetchrow("SELECT * FROM event_menu_items WHERE id=$1 AND event_id=$2", recipe_id, event_id)
    if not rec:
        raise HTTPException(404, "Not found")
    owner = await db.fetchval("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if rec["added_by_user_id"] != user_id and owner != user_id:
        raise HTTPException(403, "Access denied")
    await db.execute("DELETE FROM event_menu_items WHERE id=$1", recipe_id)


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


# ── Shared (no-auth) ──────────────────────────────────────────────────────────

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


# ── Bot ───────────────────────────────────────────────────────────────────────

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🌿 Открыть ПОЛЯНУ", web_app=WebAppInfo(url=FRONTEND_URL))]],
        resize_keyboard=True,
    )


@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    if not message.from_user:
        return
    user = message.from_user
    arg = command.args

    if arg and arg.startswith("event_"):
        try:
            event_id = int(arg.replace("event_", ""))
        except ValueError:
            await message.answer("Неверная ссылка.", reply_markup=main_kb())
            return

        async with pool.acquire() as db:
            event = await db.fetchrow("SELECT * FROM events WHERE id=$1", event_id)

        if not event:
            await message.answer("Событие не найдено или удалено.", reply_markup=main_kb())
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
        await message.answer("Главное меню:", reply_markup=main_kb())
    else:
        await message.answer(
            f"🌿 <b>Привет, {user.first_name}!</b>\n\n"
            f"ПОЛЯНА — планировщик застолий с друзьями.\n\n"
            f"Создавайте события, составляйте меню и зовите гостей — всё в Telegram 👇",
            reply_markup=main_kb(),
        )


@dp.message(F.text == "🌿 Открыть ПОЛЯНУ")
async def btn_open(message: Message):
    await message.answer("Открываю...", reply_markup=main_kb())


async def run_bot():
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="ПОЛЯНА", web_app=WebAppInfo(url=FRONTEND_URL))
        )
        await bot.set_my_commands(
            [BotCommand(command="start", description="Главное меню")],
            scope=BotCommandScopeAllPrivateChats(),
        )
        log.info("Bot polling...")
        await dp.start_polling(bot, skip_updates=True)
    except Exception as e:
        log.error("Bot error: %s", e)


@app.on_event("startup")
async def startup():
    await init_db()
    asyncio.create_task(run_bot())
    log.info("Started on port %d", PORT)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
