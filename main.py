"""Polyana API + Telegram bot — thin entrypoint.

Wires routers into the FastAPI app and registers aiogram handlers,
then starts DB init, bot polling and the OpenRouter balance monitor
in background so /health responds immediately.
"""
import asyncio
import logging

import uvicorn

import core
from core import app
from db import init_db
from llm import _openrouter_balance_loop
from config import PORT

# Register API routers
from routes import health, events, recipes, shopping, balance, invite

app.include_router(health.router)
app.include_router(events.router)
app.include_router(recipes.router)
app.include_router(shopping.router)
app.include_router(balance.router)
app.include_router(invite.router)

# Import for side-effect: registers @dp.message / @dp.callback handlers
import bot.handlers  # noqa: F401, E402

log = logging.getLogger("polyana")


async def run_bot():
    from aiogram.types import (
        MenuButtonWebApp, WebAppInfo, BotCommand, BotCommandScopeAllPrivateChats,
    )
    from config import FRONTEND_URL
    from core import bot, dp
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
    try:
        await init_db()
    except asyncio.TimeoutError:
        core._db_error = "DB connection timed out after 30s"
        log.error(core._db_error)
    except Exception as e:
        core._db_error = f"{type(e).__name__}: {e}"
        log.error("init_db error: %s", e)
    # Start bot regardless
    asyncio.create_task(run_bot())
    # Referral maturation loop disabled — referral bonuses aren't spendable now.
    # OpenRouter low-balance monitor stays: recipe text/photo/voice parsing still uses it.
    asyncio.create_task(_openrouter_balance_loop())


@app.on_event("startup")
async def startup():
    log.info("FastAPI starting on port %d", PORT)
    asyncio.create_task(_bg_init())


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
