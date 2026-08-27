"""
Scoring and filtering for Trend Scanner candidates.
"""

import logging
import os
import re
from datetime import datetime, timezone, timedelta

log = logging.getLogger("polyana.trend_scanner.scoring")

# Cheap filter keywords that indicate non-recipe content
NON_RECIPE_KEYWORDS = [
    "restaurant review", "restaurant opening", "food critic",
    "cooking class", "cooking show", "masterchef", "hell's kitchen",
    "food fight", "food challenge", "eating contest",
    "cocktail", "martini", "mojito", "whiskey", "vodka", "beer",
    "wine tasting", "bartending",
]

# Professional/complex indicators
COMPLEX_INDICATORS = [
    "sous vide", "molecular gastronomy", "fermentation chamber",
    "smoking gun", "anti-griddle", "pacotizer", "rotovap",
    "chef's table", "tasting menu", "degustation",
]


def cheap_filter(candidates: list[dict]) -> list[dict]:
    """
    Filter out obviously irrelevant candidates before expensive LLM analysis.
    """
    filtered = []
    now = datetime.now(timezone.utc)

    for c in candidates:
        title = (c.get("title") or "").lower()
        desc = (c.get("description") or "").lower()
        text = f"{title} {desc}"

        # Skip non-recipe content
        if any(kw in text for kw in NON_RECIPE_KEYWORDS):
            continue

        # Skip overly complex professional dishes
        if any(kw in text for kw in COMPLEX_INDICATORS):
            continue

        # Skip very old content (>60 days) without strong engagement
        published = c.get("published_at")
        if published:
            if isinstance(published, str):
                try:
                    published = datetime.fromisoformat(published.replace("Z", "+00:00"))
                except Exception:
                    published = None
            if published and (now - published).days > 60:
                engagement = c.get("raw_engagement") or {}
                views = engagement.get("views") or 0
                upvotes = engagement.get("upvotes") or 0
                if views < 100000 and upvotes < 1000:
                    continue

        # Skip empty titles
        if not title.strip():
            continue

        # Skip very short titles (likely not recipes)
        if len(title.strip()) < 5:
            continue

        filtered.append(c)

    return filtered


def score_candidates(candidates: list[dict]) -> list[dict]:
    """
    Score candidates without LLM (freshness, engagement, cross-source).
    """
    for c in candidates:
        c["freshness_score"] = _score_freshness(c)
        c["engagement_score"] = _score_engagement(c)
        c["cross_source_score"] = c.get("cross_source_score", 20)  # Default for single source

        # Calculate preliminary trend score
        from .scanner import _calculate_trend_score
        c["trend_score"] = _calculate_trend_score(c)

    return candidates


def _score_freshness(candidate: dict) -> float:
    """Score freshness: newer = higher."""
    published = candidate.get("published_at")
    if not published:
        return 30  # Unknown date = moderate score

    if isinstance(published, str):
        try:
            published = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except Exception:
            return 30

    now = datetime.now(timezone.utc)
    age_days = (now - published).days

    if age_days < 1:
        return 100
    elif age_days <= 3:
        return 90
    elif age_days <= 7:
        return 75
    elif age_days <= 14:
        return 55
    elif age_days <= 30:
        return 30
    else:
        return 10


def _score_engagement(candidate: dict) -> float:
    """Score engagement: normalize per platform."""
    engagement = candidate.get("raw_engagement") or {}
    platform = candidate.get("source_platform", "")

    if platform == "youtube":
        views = engagement.get("views") or 0
        if views >= 1000000:
            return 100
        elif views >= 100000:
            return 85
        elif views >= 50000:
            return 70
        elif views >= 10000:
            return 55
        elif views >= 1000:
            return 40
        else:
            return 20

    elif platform == "reddit":
        upvotes = engagement.get("upvotes") or 0
        if upvotes >= 10000:
            return 100
        elif upvotes >= 5000:
            return 85
        elif upvotes >= 1000:
            return 70
        elif upvotes >= 500:
            return 55
        elif upvotes >= 100:
            return 40
        else:
            return 20

    elif platform == "web":
        # Web doesn't have direct engagement metrics
        return 40

    else:
        return 30


async def analyze_with_llm(candidate: dict) -> dict:
    """
    Analyze a candidate with LLM for visual, simplicity, RU availability, and poliana fit scores.
    """
    import httpx

    # Build prompt
    title = candidate.get("title", "")
    desc = candidate.get("description", "")[:300]
    platform = candidate.get("source_platform", "")

    prompt = f"""Оцени этот рецепт для российской аудитории приложения "Поляна" (Telegram Mini App для рецептов).

Рецепт: {title}
Описание: {desc}
Источник: {platform}

Оцени по шкале 0-100 и верни ТОЛЬКО JSON:
{{
  "visual_score": <насколько фотогенично>,
  "simplicity_score": <насколько просто приготовить>,
  "ru_availability_score": <доступность ингредиентов в РФ>,
  "poliana_fit_score": <общая подходит ли аудитории>,
  "reason": "<1-2 предложения почему>",
  "recipe_type": "<dinner/lunch/breakfast/dessert/snack>",
  "keywords": ["<ключевое слово>", "<ключевое слово>"]
}}"""

    # Try to use the LLM router from main app
    try:
        # Read API key from env
        api_key = _get_env("QWEN_API_KEY")
        base_url = _get_env("QWEN_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = _get_env("QWEN_TEXT_MODEL") or "qwen3.7-flash"

        if not api_key:
            # Fallback to OpenRouter
            api_key = _get_env("OPENROUTER_API_KEY")
            base_url = "https://openrouter.ai/api/v1"
            model = "qwen/qwen-2.5-72b-instruct"

        if not api_key:
            log.warning("No LLM API key available, skipping analysis")
            return _default_analysis()

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 300,
                },
            )

            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                # Extract JSON from response
                json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
            else:
                log.warning("LLM request failed: %s %s", r.status_code, r.text[:200])

    except Exception as e:
        log.warning("LLM analysis failed: %s", e)

    return _default_analysis()


def _default_analysis() -> dict:
    """Return default analysis when LLM is unavailable."""
    return {
        "visual_score": 50,
        "simplicity_score": 50,
        "ru_availability_score": 50,
        "poliana_fit_score": 50,
        "reason": "LLM analysis unavailable",
        "recipe_type": "unknown",
        "keywords": [],
    }


def _get_env(key: str) -> str | None:
    """Get env var from environment or /etc/polyana/env."""
    val = os.environ.get(key)
    if val:
        return val
    try:
        with open("/etc/polyana/env") as f:
            for line in f:
                if line.startswith(f"{key}="):
                    return line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    try:
        with open("/etc/polyana/llm.env") as f:
            for line in f:
                if line.startswith(f"{key}="):
                    return line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    return None
