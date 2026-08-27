"""
Content classifier for Trend Scanner.

Classifies candidates as specific_recipe, recipe_compilation, trend_article,
general_food, restaurant_content, or non_recipe.
"""

import json
import logging
import os
import re

log = logging.getLogger("polyana.trend_scanner.classifier")

# Deterministic patterns for compilation/non-recipe detection
COMPILATION_PATTERNS = [
    r'\btop\s+\d+\b',
    r'\bbest\s+recipes?\b',
    r'\bviral\s+recipes?\b',
    r'\brecipes?\s+of\s+\d{4}\b',
    r'\bi\s+tested\b',
    r'\btesting\s+viral\b',
    r'\bour\s+of\b.*\bhour\b',
    r'\bwhat\s+i\s+eat\b',
    r'\broundup\b',
    r'\bcollection\b',
    r'\bfood\s+trends?\b',
    r'\brecipe\s+ideas?\b',
    r'\brecipes?\s+you\s+(need|should|must)\b',
    r'\brecipes?\s+everyone\s+is\b',
    r'\brecipes?\s+that\s+(went|are)\s+viral\b',
    r'\bmy\s+top\s+\d+\b',
    r'\bdishes?\s+you\s+need\b',
    r'\bfoods?\s+you\s+(need|should|must)\b',
    r'\btik\s*tok\s+recipes?\b',
    r'\btiktok\s+recipes?\b',
    r'\binstagram\s+recipes?\b',
    r'\byoutube\s+recipes?\b',
]

NON_RECIPE_PATTERNS = [
    r'\brestaurant\s+review\b',
    r'\bfood\s+critic\b',
    r'\bcooking\s+class\b',
    r'\bcooking\s+show\b',
    r'\bmasterchef\b',
    r'\bhell\'?s\s+kitchen\b',
    r'\bfood\s+fight\b',
    r'\bfood\s+challenge\b',
    r'\beating\s+contest\b',
    r'\bcocktail\b',
    r'\bmartini\b',
    r'\bmojito\b',
    r'\bwhiskey\b',
    r'\bvodka\b',
    r'\bbeer\b',
    r'\bwine\s+tasting\b',
    r'\bbartending\b',
    r'\bkitchen\s+hack\b',
    r'\blife\s+hack\b',
    r'\basmr\b',
    r'\bmukbang\b',
    r'\bfood\s+review\b',
    r'\btaste\s+test\b',
    r'\branking\b',
    r'\btier\s+list\b',
]

# Patterns that suggest a specific recipe even if they contain viral/trending
SPECIFIC_RECIPE_SIGNALS = [
    r'\b(cottage\s+cheese|творог)\b.*\b(flatbread|лепёшка|bread)\b',
    r'\b(hot\s+honey|горячий\s+мёд)\b.*\b(chicken|курица)\b',
    r'\b(smashed|раздавленн)\b.*\b(potato|картофел)\b',
    r'\b(frozen|замороженн)\b.*\b(watermelon|арбуз)\b',
    r'\b(turkish|турецк)\b.*\b(pasta|паста|макарон)\b',
    r'\b(dubai|дубайск)\b.*\b(chocolate|шоколад)\b',
    r'\b(birria|бирриа)\b',
    r'\b(quesabirria)\b',
    r'\b(focaccia|фокачча)\b',
    r'\b(shakshuka|шакшука)\b',
    r'\b(ramen|рамен)\b',
    r'\b(poke|поке)\b',
    r'\b(buddha\s+bowl|боул)\b',
    r'\b(overnight\s+oats|овсянка)\b',
    r'\b(banana\s+bread|банановый\s+хлеб)\b',
    r'\b(chicken\s+caesar|цезарь)\b',
    r'\b(pasta\s+salad|макаронный\s+салат)\b',
    r'\b(garlic\s+bread|чесночный\s+хлеб)\b',
    r'\b(chicken\s+nuggets|куриные\s+наггетсы)\b',
    r'\b(mac\s+and\s+cheese|макароны\s+по-флотски)\b',
]


def classify_content(candidate: dict) -> dict:
    """
    Classify a candidate's content type.

    Returns dict with:
    - content_type: specific_recipe | recipe_compilation | trend_article |
                    general_food | restaurant_content | non_recipe
    - is_specific_recipe: bool
    - canonical_dish_name: str or None
    - confidence: 0-100
    - reason: str
    """
    title = (candidate.get("title") or "").lower()
    desc = (candidate.get("description") or "").lower()
    title_text = title  # Only use title for classification
    full_text = f"{title} {desc}"  # Use full text for extraction

    # Check non-recipe first (use full text)
    for pattern in NON_RECIPE_PATTERNS:
        if re.search(pattern, full_text, re.IGNORECASE):
            return {
                "content_type": "non_recipe",
                "is_specific_recipe": False,
                "canonical_dish_name": None,
                "confidence": 80,
                "reason": f"Matches non-recipe pattern: {pattern}",
            }

    # Check compilation patterns FIRST (use title only)
    for pattern in COMPILATION_PATTERNS:
        if re.search(pattern, title_text, re.IGNORECASE):
            return {
                "content_type": "recipe_compilation",
                "is_specific_recipe": False,
                "canonical_dish_name": None,
                "confidence": 75,
                "reason": f"Matches compilation pattern: {pattern}",
            }

    # Check if it's a specific recipe despite viral/trending keywords (use title only)
    for pattern in SPECIFIC_RECIPE_SIGNALS:
        match = re.search(pattern, title_text, re.IGNORECASE)
        if match:
            dish_name = _extract_dish_name(title)
            return {
                "content_type": "specific_recipe",
                "is_specific_recipe": True,
                "canonical_dish_name": dish_name,
                "confidence": 85,
                "reason": f"Specific dish identified despite trending keywords",
            }

    # Check compilation patterns
    for pattern in COMPILATION_PATTERNS:
        if re.search(pattern, full_text, re.IGNORECASE):
            return {
                "content_type": "recipe_compilation",
                "is_specific_recipe": False,
                "canonical_dish_name": None,
                "confidence": 75,
                "reason": f"Matches compilation pattern: {pattern}",
            }

    # If title looks like a specific dish name (short, no list indicators)
    if _looks_like_specific_dish(title):
        dish_name = _extract_dish_name(title)
        return {
            "content_type": "specific_recipe",
            "is_specific_recipe": True,
            "canonical_dish_name": dish_name,
            "confidence": 70,
            "reason": "Title appears to be a specific dish name",
        }

    # Default: general_food (needs LLM classification)
    return {
        "content_type": "general_food",
        "is_specific_recipe": False,
        "canonical_dish_name": None,
        "confidence": 40,
        "reason": "Unable to classify deterministically",
    }


def _looks_like_specific_dish(title: str) -> bool:
    """Check if a title looks like a specific dish name."""
    if not title or len(title) < 3:
        return False

    # Too long = likely not a specific dish
    if len(title) > 80:
        return False

    # Contains list indicators
    list_indicators = [",", "&", " and "]
    title_lower = title.lower()
    if any(ind in title_lower for ind in list_indicators):
        return False

    # Contains numbers (likely "Top 10", "5 recipes", etc.)
    if re.search(r'\b\d+\b', title_lower):
        return False

    # Contains "recipes" (plural suggests compilation)
    if re.search(r'\brecipes\b', title_lower):
        return False

    # Contains strong compilation keywords (without a dish name)
    strong_compilation = ["top", "best", "worst", "ranking", "tested", "testing",
                         "ideas", "collection", "roundup"]
    for kw in strong_compilation:
        if re.search(rf'\b{kw}\b', title_lower):
            return False

    # "viral" or "trending" alone doesn't disqualify if there's a dish name
    # e.g., "Viral Cottage Cheese Recipe" is a specific dish
    # But "Viral Recipes" is not
    if re.search(r'\bviral\b|\btrending\b', title_lower):
        # Check if there's a dish name after removing viral/trending
        cleaned = re.sub(r'\bviral\b|\btrending\b', '', title_lower).strip()
        # If after removing viral/trending, we still have meaningful words, it's specific
        if len(cleaned.split()) >= 2:
            return True
        return False

    return True


def _extract_dish_name(title: str) -> str:
    """Extract canonical dish name from title."""
    if not title:
        return ""

    # Clean up
    name = title.strip()

    # Remove common prefixes
    prefixes = [
        "viral ", "trending ", "easy ", "quick ", "best ", "simple ",
        "homemade ", "healthy ", "high protein ", "low carb ",
        "вирусный ", "популярный ", "быстрый ", "простой ",
    ]
    for prefix in prefixes:
        if name.lower().startswith(prefix):
            name = name[len(prefix):]

    # Remove common suffixes
    suffixes = [
        " recipe", " recipes", " trend", " trending", " viral",
        " 2025", " 2026", " this week", " today",
        " рецепт", " рецепты", " тренд",
    ]
    for suffix in suffixes:
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)]

    # Capitalize first letter of each word
    name = name.strip()
    if name:
        name = name[0].upper() + name[1:]

    return name


async def classify_with_llm(candidate: dict) -> dict:
    """
    Use LLM to classify content when deterministic classification is uncertain.
    Returns structured JSON with content_type, canonical_dish_name, confidence, reason.
    """
    import httpx

    title = candidate.get("title", "")
    desc = (candidate.get("description") or "")[:300]
    platform = candidate.get("source_platform", "")

    prompt = f"""Classify this food content for a Russian recipe app.

Title: {title}
Description: {desc}
Platform: {platform}

Return ONLY valid JSON:
{{
  "content_type": "specific_recipe|recipe_compilation|trend_article|general_food|restaurant_content|non_recipe",
  "is_specific_recipe": true/false,
  "canonical_dish_name": "Dish Name or null",
  "confidence": 0-100,
  "reason": "1-2 sentences"
}}"""

    try:
        api_key = _get_env("QWEN_API_KEY")
        base_url = _get_env("QWEN_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = _get_env("QWEN_TEXT_MODEL") or "qwen3.7-flash"

        if not api_key:
            api_key = _get_env("OPENROUTER_API_KEY")
            base_url = "https://openrouter.ai/api/v1"
            model = "qwen/qwen-2.5-72b-instruct"

        if not api_key:
            log.warning("No LLM API key available for classification")
            return None

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "max_tokens": 200,
                },
            )

            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
                if json_match:
                    result = json.loads(json_match.group())
                    # Validate required fields
                    if "content_type" in result and "is_specific_recipe" in result:
                        return result

    except Exception as e:
        log.warning("LLM classification failed: %s", e)

    return None


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
