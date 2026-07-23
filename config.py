import os, re, logging

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

_URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)

RECIPE_SYSTEM_PROMPT = """Ты — кулинарный редактор. Из присланного контента извлеки рецепт и верни строго JSON.
Если это НЕ связано с едой/готовкой совсем — верни {"not_a_recipe": true}.

ВАЖНО про "не рецепт": список ингредиентов блюда, набор продуктов для готовки, шпаргалка закупки —
это ВСЁ рецепты (steps могут быть пустыми). Не возвращай not_a_recipe только потому что нет пошагового
описания. not_a_recipe — только если текст вообще не про еду (привет, код, новостная статья, переписка).

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
name: если явного названия нет, выведи его из контекста (например «Шашлык» из списка ингредиентов для шашлыка).
steps: если пошагового описания нет — верни пустой массив [], это нормально.
Переведи название на русский если оригинал не русский."""

# Vision models tried in order — gemini first (multimodal, reliable), qwen fallback.
_VISION_MODELS = ["google/gemini-2.5-flash", "qwen/qwen2.5-vl-72b-instruct"]

_WHISPER_PROMPT = (
    "Кулинарный рецепт. Точно распознай названия продуктов, цифры и единицы измерения: "
    "граммы, килограммы, штуки, ложки, стаканы. "
    "Пример правильного ввода: «возьмите 500 граммов свинины, 3 луковицы, 2 столовые ложки масла»."
)

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

CATEGORY_ORDER = [
    "мясо", "рыба", "овощи", "фрукты", "молочное", "яйца",
    "крупы", "мука", "масло", "соусы", "специи", "орехи",
    "сахар", "консервы", "хлеб", "грибы", "напитки", "прочее",
]

_RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_RU_WDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
