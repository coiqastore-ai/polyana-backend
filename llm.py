import time, logging, asyncio
import httpx
import core
from config import OPENROUTER_KEY, OPENROUTER_PROXY_URL, OPENROUTER_PROXY_SECRET, OPENROUTER_LOW_BALANCE_USD, ADMIN_CHAT_ID, RECIPE_SYSTEM_PROMPT

log = logging.getLogger("polyana")


def _get_or_client():
    if core._or_client is None:
        if not OPENROUTER_KEY:
            raise RuntimeError("OPENROUTER_API_KEY не задан в env")
        from openai import AsyncOpenAI
        _base = OPENROUTER_PROXY_URL.rstrip("/") + "/api/v1" if OPENROUTER_PROXY_URL else "https://openrouter.ai/api/v1"
        core._or_client = AsyncOpenAI(
            api_key=OPENROUTER_KEY,
            base_url=_base,
            default_headers={"X-Proxy-Secret": OPENROUTER_PROXY_SECRET} if OPENROUTER_PROXY_SECRET else {},
        )
    return core._or_client


async def _llm_normalize_ingredients(raw_strings: list[str]) -> list[dict]:
    """
    Post-process raw ingredient strings from recipe-scrapers
    (e.g. '500г свинины шейки') into structured dicts with qty/unit/category.
    Falls back gracefully: if LLM fails, returns original strings as name-only.
    """
    import json
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
    import json
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
    import json
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
    import json
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


# ── Receipt parsing (split expenses) ─────────────────────────────────────────

RECEIPT_SYSTEM_PROMPT = """Ты — парсер кассовых чеков РФ. Распознай чек с фотографии и верни СТРОГО JSON:
{
  "store": "название магазина (Пятёрочка, Магнит, Перекрёсток, Лента и т.п.)",
  "total": 2847.00,
  "items": [
    {"name": "название товара", "price": 349.00, "quantity": 1.0, "sum": 349.00}
  ]
}

Правила:
- price — цена за ЕДИНИЦУ товара, sum — цена × количество (в рублях, не копейках)
- quantity — количество (1.0 если одна штука, 0.5 если полкило и т.п.)
- Пропускай служебные строки: «КАССИР», «ИТОГО», «СУММА», «СДАЧА», «СПАСИБО ЗА ПОКУПКУ», «ПРОЕЗД», QR-коды
- Если название товара нечитаемо или сокращено — восстанови до стандартного («МОЛ 3.2%» → «Молоко 3.2%»)
- total — это итоговая сумма чека из строки «ИТОГО» (если видна), иначе сумма всех items
- Если на фото НЕ чек (рецепт, документ, случайное фото) — верни {"not_a_receipt": true}
- Все суммы в рублях с копейками (349.00, не 34900)"""


async def _llm_parse_receipt(image_bytes: bytes) -> dict | None:
    """Parse a store receipt from a photo. Returns {store, total, items} or None
    if the image is not a receipt. Tries gemini-flash first (cheap, multimodal),
    qwen-vl as fallback so a single provider 429 doesn't kill the split flow."""
    import json
    import base64
    client = _get_or_client()
    b64 = base64.b64encode(image_bytes).decode()
    messages = [
        {"role": "system", "content": RECEIPT_SYSTEM_PROMPT},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": "Распознай этот кассовый чек и верни JSON с товарами."},
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
            if data.get("not_a_receipt"):
                return None
            # Normalize: coerce numeric fields, default missing ones
            items = []
            for it in (data.get("items") or [])[:60]:  # cap at 60 lines
                try:
                    price = round(float(it.get("price", 0)), 2)
                    qty = float(it.get("quantity", 1) or 1)
                    s = round(float(it.get("sum", price * qty) or price * qty), 2)
                    name = str(it.get("name", "")).strip()[:120]
                    if name and price >= 0:
                        items.append({"name": name, "price": price, "quantity": qty, "sum": s})
                except (TypeError, ValueError):
                    continue
            total = round(float(data.get("total", 0) or 0), 2)
            # Sanity: if total missing/wrong, recompute from items
            items_sum = round(sum(i["sum"] for i in items), 2)
            if not total or abs(total - items_sum) > max(total, items_sum) * 0.5:
                total = items_sum
            if not items:
                return None
            return {
                "store": str(data.get("store", "Магазин")).strip()[:120] or "Магазин",
                "total": total,
                "items": items,
            }
        except Exception as e:
            last_err = e
            log.warning("receipt vision parse via %s failed: %s", model, e)
            continue
    log.error("all vision models failed for receipt: %s", last_err)
    raise ValueError(
        "Не удалось распознать чек. Попробуй фото получше или чек с QR-кодом."
    )


_WHISPER_PROMPT = (
    "Кулинарный рецепт. Точно распознай названия продуктов, цифры и единицы измерения: "
    "граммы, килограммы, штуки, ложки, стаканы. "
    "Пример правильного ввода: «возьмите 500 граммов свинины, 3 луковицы, 2 столовые ложки масла»."
)

async def _transcribe_voice(audio_bytes: bytes) -> str:
    """Transcribe voice via OpenRouter (openai/whisper-large-v3) with culinary hint."""
    import io
    client = _get_or_client()
    resp = await client.audio.transcriptions.create(
        model="openai/whisper-large-v3",
        file=("voice.ogg", io.BytesIO(audio_bytes), "audio/ogg"),
        language="ru",
        temperature=0.1,
        prompt=_WHISPER_PROMPT,
    )
    return resp.text


async def _alert_admin(text: str) -> None:
    """Send an operational alert to the admin chat (best-effort)."""
    if not ADMIN_CHAT_ID:
        return
    try:
        await core.bot.send_message(ADMIN_CHAT_ID, text)
    except Exception:
        log.exception("admin alert failed")


async def _openrouter_remaining_usd() -> float | None:
    """Remaining OpenRouter credit in USD, or None if unavailable."""
    if not OPENROUTER_KEY:
        return None
    if core._last_403_ts and (time.time() - core._last_403_ts) < 3600:
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
            core._last_403_ts = time.time()
            return None
        d = (r.json() or {}).get("data") or {}
        core._last_403_ts = 0.0
        return float(d.get("total_credits", 0)) - float(d.get("total_usage", 0))
    except Exception:
        log.exception("openrouter credits check failed")
        return None

async def _openrouter_balance_loop():
    """Alert admin once when OpenRouter credit drops below threshold; reset on recovery."""
    while True:
        try:
            rem = await _openrouter_remaining_usd()
            if rem is not None:
                if rem < OPENROUTER_LOW_BALANCE_USD and not core._low_balance_alerted:
                    core._low_balance_alerted = True
                    await _alert_admin(
                        f"⚠️ OpenRouter: остаток ${rem:.2f} (< ${OPENROUTER_LOW_BALANCE_USD:.0f}). "
                        f"Пополни, пока генерация не встала: https://openrouter.ai/settings/credits"
                    )
                elif rem >= OPENROUTER_LOW_BALANCE_USD and core._low_balance_alerted:
                    core._low_balance_alerted = False  # recovered → re-arm
        except Exception:
            log.exception("openrouter balance loop error")
        await asyncio.sleep(1800)   # every 30 min
