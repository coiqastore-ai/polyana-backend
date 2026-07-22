from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import Response
from config import _RU_MONTHS, _RU_WDAYS, FRONTEND_URL, ADMIN_CHAT_ID, PRICE_AI_INVITE
from llm import _get_or_client, _alert_admin
import invite
from core import bot
from db import get_db, track
from auth import get_current_user
import base64, logging, json
from aiogram.types import BufferedInputFile

log = logging.getLogger("polyana")

router = APIRouter()


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


async def _openrouter_background(scene_prompt: str) -> bytes:
    """Generate a vertical 9:16 1K background (no text) via gpt-5.4-image-2."""
    from config import OPENROUTER_KEY
    import httpx
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
            from config import SUPPORT_HANDLE
            raise HTTPException(503, f"Генерация временно недоступна. Напишите в поддержку {SUPPORT_HANDLE}")
        log.error("OpenRouter image error: %s", msg)
        from config import SUPPORT_HANDLE
        raise HTTPException(502, f"Не удалось сгенерировать фон, попробуйте ещё раз. Если повторяется — {SUPPORT_HANDLE}")
    try:
        url = data["choices"][0]["message"]["images"][0]["image_url"]["url"]
        return base64.b64decode(url.split(",", 1)[1])
    except Exception:
        raise HTTPException(502, "Модель не вернула изображение")


@router.post("/api/events/{event_id}/invite")
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


@router.get("/api/invite/themes")
async def list_invite_themes(user_id: int = Depends(get_current_user)):
    return [{"key": k, "title": k.capitalize()} for k in invite.THEMES.keys()]


@router.post("/api/events/{event_id}/invite/send")
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
