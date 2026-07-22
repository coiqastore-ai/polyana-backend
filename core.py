from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher
from aiogram.types import Message, CallbackQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.state import State, StatesGroup
from config import FRONTEND_URL, BOT_TOKEN

pool = None
_db_ready = False
_db_error: str | None = None

_or_client = None

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

_bot_username: str | None = None

_low_balance_alerted = False

_last_403_ts = 0.0

# FSM states for voice recipe editing flow
class VoiceStates(StatesGroup):
    editing = State()   # User is typing a corrected transcript

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
