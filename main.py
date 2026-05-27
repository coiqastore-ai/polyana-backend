import os, hashlib, hmac, json, asyncio, secrets, time, logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl
import asyncpg
from fastapi import FastAPI, HTTPException, Header, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats
from aiogram.types import KeyboardButton, MenuButtonWebApp, Message, ReplyKeyboardMarkup, WebAppInfo
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
                id SERIAL PRIMARY KEY, title TEXT NOT NULL,
                date TIMESTAMPTZ, guests_count INT DEFAULT 1,
                notes TEXT, telegram_user_id BIGINT, created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS recipes (
                id SERIAL PRIMARY KEY, title TEXT NOT NULL, telegram_user_id BIGINT
            );
            CREATE TABLE IF NOT EXISTS event_recipes (
                event_id INT REFERENCES events(id) ON DELETE CASCADE,
                recipe_id INT REFERENCES recipes(id) ON DELETE CASCADE,
                servings_multiplier FLOAT DEFAULT 1, PRIMARY KEY (event_id, recipe_id)
            );
            CREATE TABLE IF NOT EXISTS ingredients (
                id SERIAL PRIMARY KEY, recipe_id INT REFERENCES recipes(id) ON DELETE CASCADE,
                name TEXT NOT NULL, quantity FLOAT, unit TEXT
            );
            CREATE TABLE IF NOT EXISTS collaborators (
                id SERIAL PRIMARY KEY, event_id INT REFERENCES events(id) ON DELETE CASCADE,
                telegram_user_id BIGINT NOT NULL, name TEXT
            );
            CREATE TABLE IF NOT EXISTS shopping_items (
                id SERIAL PRIMARY KEY, event_id INT REFERENCES events(id) ON DELETE CASCADE,
                ingredient_name TEXT NOT NULL, total_grams FLOAT,
                total_display TEXT, bought BOOLEAN DEFAULT FALSE
            );
            CREATE TABLE IF NOT EXISTS login_tokens (
                id SERIAL PRIMARY KEY, token TEXT UNIQUE NOT NULL,
                telegram_user_id BIGINT NOT NULL, used BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW(), expires_at TIMESTAMPTZ NOT NULL
            );
        """)
    log.info("DB ready")


def validate_init_data(init_data):
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
        ad = int(parsed.get("auth_date"))
    except (ValueError, TypeError):
        return None
    if ad <= 0 or (time.time() - ad) > 86400:
        return None
    dc = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    sk = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(sk, dc.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, h):
        return None
    try:
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None


async def get_current_user(
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
    db=Depends(get_db),
):
    if not x_telegram_init_data:
        raise HTTPException(401, "Missing initData")
    user = validate_init_data(x_telegram_init_data)
    if not user or "id" not in user:
        raise HTTPException(401, "Invalid initData")
    return int(user["id"])


app = FastAPI(title="POLYANA API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "https://coiqastore-ai.github.io",
        "https://web.telegram.org",
        "https://telegram.org",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "POLYANA API"}


@app.get("/api/events/shared/{event_id}")
async def get_shared_event(event_id: int, db=Depends(get_db)):
    row = await db.fetchrow(
        "SELECT id, title, date, guests_count, notes FROM events WHERE id=$1", event_id
    )
    if not row:
        raise HTTPException(404, "Not found")
    data = dict(row)
    data["read_only"] = True
    return data


@app.get("/api/events")
async def list_events(user_id: int = Depends(get_current_user), db=Depends(get_db)):
    rows = await db.fetch("SELECT * FROM events WHERE telegram_user_id=$1 ORDER BY date ASC", user_id)
    return [dict(r) for r in rows]


@app.post("/api/events", status_code=201)
async def create_event(body: dict, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    row = await db.fetchrow(
        "INSERT INTO events (title, date, guests_count, notes, telegram_user_id) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING *",
        body.get("title"),
        body.get("date"),
        body.get("guests_count", 1),
        body.get("notes"),
        user_id,
    )
    return dict(row)


@app.get("/api/events/{event_id}")
async def get_event(event_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    row = await db.fetchrow("SELECT * FROM events WHERE id=$1 AND telegram_user_id=$2", event_id, user_id)
    if not row:
        raise HTTPException(404, "Not found")
    return dict(row)


@app.patch("/api/events/{event_id}")
async def update_event(event_id: int, body: dict, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    owner = await db.fetchval("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if owner != user_id:
        raise HTTPException(403, "Access denied")
    fields = {k: v for k, v in body.items() if k in ("title", "date", "guests_count", "notes") and v is not None}
    if not fields:
        raise HTTPException(400, "No fields")
    sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(fields))
    vals = list(fields.values())
    row = await db.fetchrow(f"UPDATE events SET {sets} WHERE id=$1 RETURNING *", event_id, *vals)
    return dict(row)


@app.delete("/api/events/{event_id}", status_code=204)
async def delete_event(event_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    owner = await db.fetchval("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if owner != user_id:
        raise HTTPException(403, "Access denied")
    await db.execute("DELETE FROM events WHERE id=$1", event_id)


@app.post("/api/auth/token")
async def auth_token(body: dict, db=Depends(get_db)):
    token = body.get("token", "")
    row = await db.fetchrow(
        "SELECT id, telegram_user_id FROM login_tokens WHERE token=$1 AND used=false AND expires_at > NOW()",
        token,
    )
    if not row:
        raise HTTPException(401, "Invalid token")
    await db.execute("UPDATE login_tokens SET used=true WHERE id=$1", row["id"])
    return {"telegram_user_id": row["telegram_user_id"], "status": "ok"}


@app.get("/api/auth/generate-token")
async def gen_token(
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-API-Key"),
    auth_uid: int | None = Query(default=None),
    db=Depends(get_db),
):
    if not INTERNAL_API_KEY or not hmac.compare_digest(str(x_internal_api_key or ""), INTERNAL_API_KEY):
        raise HTTPException(401, "Invalid key")
    if not auth_uid:
        raise HTTPException(400, "auth_uid required")
    token = secrets.token_urlsafe(32)
    await db.execute(
        "INSERT INTO login_tokens (token, telegram_user_id, expires_at) VALUES ($1, $2, $3)",
        token, auth_uid, datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return {"token": token}


# Bot
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

BTN_OPEN = "Open POLYANU"
BTN_CODE = "Login code"
BTN_HELP = "Help"


def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_OPEN, web_app=WebAppInfo(url=FRONTEND_URL))],
            [KeyboardButton(text=BTN_CODE), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
    )


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "POLYANA\n\nPlan meals with friends.\n\n"
        "Open POLYANU to enter the app\n"
        "Login code for browser access",
        reply_markup=main_kb(),
    )


@dp.message(F.text == BTN_CODE)
async def cmd_code(message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    token = secrets.token_urlsafe(32)
    async with pool.acquire() as db:
        await db.execute(
            "INSERT INTO login_tokens (token, telegram_user_id, expires_at) VALUES ($1, $2, $3)",
            token, uid, datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    link = f"{FRONTEND_URL}/?token={token}"
    await message.answer(
        f"Your login link (5 min):\n{link}\n\nOpen in browser.",
        reply_markup=main_kb(),
    )


@dp.message(F.text == BTN_HELP)
async def cmd_help(message: Message):
    await message.answer(
        "Commands:\n/start - main menu\n\n"
        "1. Open POLYANU\n2. Create event\n3. Add recipes\n4. Share link",
        reply_markup=main_kb(),
    )


async def run_bot():
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="POLYANA", web_app=WebAppInfo(url=FRONTEND_URL))
        )
        await bot.set_my_commands([
            BotCommand(command="start", description="Main menu"),
            BotCommand(command="help", description="Help"),
        ], scope=BotCommandScopeAllPrivateChats())
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