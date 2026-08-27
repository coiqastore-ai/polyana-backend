"""
Scoring and filtering for Trend Scanner v0.1.1.

Includes: content classification, freshness, engagement velocity,
trend confidence, quality gates, and LLM analysis.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta

log = logging.getLogger("polyana.trend_scanner.scoring")

# Quality gate thresholds (configurable via env)
TREND_MIN_SCORE = int(os.environ.get("TREND_MIN_SCORE", "60"))
TREND_MIN_CONFIDENCE = int(os.environ.get("TREND_MIN_CONFIDENCE", "50"))

# Source relevance weights
SOURCE_RELEVANCE = {
    "web": 1.0,
    "youtube": 1.0,
    "rss": 0.9,
    "reddit": 1.0,
    "instagram": 1.1,
    "tiktok": 1.2,
    "pinterest": 0.9,
    "bilibili": 0.6,
}


def cheap_filter(candidates: list[dict]) -> list[dict]:
    """
    Filter out obviously irrelevant candidates before expensive analysis.
    """
    from .classifier import classify_content

    filtered = []
    now = datetime.now(timezone.utc)

    for c in candidates:
        title = (c.get("title") or "").lower()
        desc = (c.get("description") or "").lower()
        text = f"{title} {desc}"

        # Skip empty titles
        if not title.strip():
            continue

        # Skip very short titles (likely not recipes)
        if len(title.strip()) < 5:
            continue

        # Classify content
        classification = classify_content(c)
        c.update(classification)

        # Skip non-recipe content
        if classification["content_type"] == "non_recipe":
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

        filtered.append(c)

    return filtered


def score_candidates(candidates: list[dict]) -> list[dict]:
    """
    Score candidates without LLM (freshness, engagement, cross-source, velocity).
    """
    for c in candidates:
        c["freshness_score"] = _score_freshness(c)
        c["engagement_score"] = _score_engagement(c)
        c["engagement_velocity"] = _calculate_velocity(c)
        c["trend_confidence"] = _calculate_confidence(c)
        c["cross_source_score"] = c.get("cross_source_score", 20)  # Default for single source

        # Calculate preliminary trend score
        from .scanner import _calculate_trend_score
        c["trend_score"] = _calculate_trend_score(c)

    return candidates


def _score_freshness(candidate: dict) -> float:
    """
    Score freshness: newer = higher.

    0-1 day    100
    2-3 days    90
    4-7 days    80
    8-14 days   60
    15-30 days  35
    >30 days    10
    Unknown     20 (penalized)
    """
    published = candidate.get("published_at")
    if not published:
        return 20  # Unknown date = penalized

    if isinstance(published, str):
        try:
            published = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except Exception:
            return 20

    now = datetime.now(timezone.utc)
    age_days = (now - published).days

    if age_days < 1:
        return 100
    elif age_days <= 3:
        return 90
    elif age_days <= 7:
        return 80
    elif age_days <= 14:
        return 60
    elif age_days <= 30:
        return 35
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


def _calculate_velocity(candidate: dict) -> float:
    """
    Calculate engagement velocity: views/age_hours or upvotes/age_hours.

    Higher velocity = more trending.
    """
    published = candidate.get("published_at")
    engagement = candidate.get("raw_engagement") or {}

    if not published:
        return 0

    if isinstance(published, str):
        try:
            published = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except Exception:
            return 0

    now = datetime.now(timezone.utc)
    age_hours = max(1, (now - published).total_seconds() / 3600)

    # Store age_hours for confidence calculation
    candidate["age_hours"] = age_hours

    views = engagement.get("views") or 0
    upvotes = engagement.get("upvotes") or 0

    if views > 0:
        velocity = views / age_hours
    elif upvotes > 0:
        velocity = (upvotes * 100) / age_hours  # Normalize upvotes to views
    else:
        return 0

    # Normalize to 0-100 scale
    # 1000 views/hour = 50, 10000 views/hour = 80, 100000 views/hour = 100
    if velocity >= 100000:
        return 100
    elif velocity >= 10000:
        return 80
    elif velocity >= 1000:
        return 60
    elif velocity >= 100:
        return 40
    elif velocity >= 10:
        return 20
    else:
        return 10


def _calculate_confidence(candidate: dict) -> float:
    """
    Calculate trend confidence based on data quality.

    Positive signals:
    - published_at known
    - engagement known
    - multiple independent sources
    - author known
    - specific recipe identified
    - recent source

    Negative signals:
    - date unknown
    - engagement unknown
    - single weak source
    - ambiguous dish
    - compilation extraction only
    """
    confidence = 50  # Base

    # Published date known
    if candidate.get("published_at"):
        confidence += 10
    else:
        confidence -= 15

    # Engagement known
    engagement = candidate.get("raw_engagement") or {}
    if engagement.get("views") or engagement.get("upvotes"):
        confidence += 10
    else:
        confidence -= 10

    # Multiple sources
    source_count = candidate.get("source_count", 1)
    if source_count >= 3:
        confidence += 15
    elif source_count >= 2:
        confidence += 10
    else:
        confidence -= 5

    # Author known
    if candidate.get("source_author"):
        confidence += 5

    # Specific recipe identified
    if candidate.get("content_type") == "specific_recipe":
        confidence += 10
    elif candidate.get("content_type") == "recipe_compilation":
        confidence -= 10

    # Canonical dish name
    if candidate.get("canonical_dish_name"):
        confidence += 5

    # Recent source (within 7 days)
    published = candidate.get("published_at")
    if published:
        if isinstance(published, str):
            try:
                published = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except Exception:
                published = None
        if published:
            age_days = (datetime.now(timezone.utc) - published).days
            if age_days <= 7:
                confidence += 10
            elif age_days <= 14:
                confidence += 5
            elif age_days > 30:
                confidence -= 10

    # Clamp to 0-100
    return max(0, min(100, confidence))


def passes_quality_gate(candidate: dict) -> bool:
    """
    Check if candidate passes quality gate thresholds.
    """
    score = candidate.get("trend_score", 0) or 0
    confidence = candidate.get("trend_confidence", 0) or 0

    return score >= TREND_MIN_SCORE and confidence >= TREND_MIN_CONFIDENCE


async def analyze_with_llm(candidate: dict) -> dict:
    """
    Analyze a candidate with LLM for visual, simplicity, RU availability, and poliana fit scores.
    """
    import httpx

    # Build prompt
    title = candidate.get("title", "")
    desc = candidate.get("description", "")[:300]
    platform = candidate.get("source_platform", "")
    content_type = candidate.get("content_type", "unknown")
    canonical_dish = candidate.get("canonical_dish_name", "")

    prompt = f"""Оцени этот рецепт для российской аудитории приложения "Поляна" (Telegram Mini App для рецептов).

Рецепт: {title}
Описание: {desc}
Источник: {platform}
Тип контента: {content_type}
Каноническое название: {canonical_dish}

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
