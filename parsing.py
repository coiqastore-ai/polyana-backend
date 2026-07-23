import re, logging, secrets
import httpx
from config import _URL_RE, RECIPE_SYSTEM_PROMPT, _BROWSER_HEADERS, FRONTEND_URL
from db import track
from llm import _get_or_client, _llm_normalize_ingredients, _llm_parse_text, _llm_parse_image, _transcribe_voice
import core

log = logging.getLogger("polyana")


async def _ensure_public_url(u: str) -> None:
    """SSRF guard: allow only http(s) to a publicly-routable host. Raises ValueError."""
    import ipaddress
    import asyncio
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
        import logging
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
    import asyncpg
    from utils import categorize_ingredient
    if core.pool is None:
        raise RuntimeError("DB not ready")

    async with core.pool.acquire() as db:
        try:
            rec = await db.fetchrow(
                """
                INSERT INTO recipes
                    (user_id, name, name_original, emoji, source_url, source_type,
                     original_language, servings, cook_time_minutes, category, source_photo_file_id, share_token)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
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
                secrets.token_urlsafe(16),
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
