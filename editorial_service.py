"""
Editorial recipe service — create, approve, publish, clone editorial recipes.

Editorial recipes are system-owned (EDITORIAL_USER_ID) content pieces
that can be published to Telegram and saved by users as personal copies.
"""

import json
import re
import secrets
from datetime import datetime, timezone

import asyncpg


# ── Helpers ──────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Transliterate + slugify for content_slug."""
    text = text.lower().strip()
    # Cyrillic basic transliteration
    tr = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    }
    result = []
    for ch in text:
        result.append(tr.get(ch, ch))
    slug = ''.join(result)
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = slug.strip('-')
    return slug[:80]


# ── Create ───────────────────────────────────────────────────────────────────

async def create_editorial_recipe(
    db: asyncpg.Connection,
    editorial_user_id: int,
    *,
    name: str,
    description: str | None = None,
    emoji: str = "🍽",
    slug: str | None = None,
    servings: int = 4,
    cook_time_minutes: int | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    source_url: str | None = None,
    source_platform: str | None = None,
    source_author: str | None = None,
    trend_score: float | None = None,
    editorial_image_url: str | None = None,
    ingredients: list[dict] | None = None,
    steps: list[dict] | None = None,
    nutrition: dict | None = None,
) -> dict:
    """Create a new editorial recipe in draft status."""
    if not name or not name.strip():
        raise ValueError("name required")

    if not slug:
        slug = _slugify(name)
    # Verify slug uniqueness
    existing = await db.fetchval(
        "SELECT id FROM recipes WHERE content_slug=$1", slug
    )
    if existing:
        slug = f"{slug}-{secrets.token_hex(3)}"

    # Extract nutrition fields
    calories = protein = fat = carbs = nut_basis = nut_source = None
    if nutrition:
        calories = _validate_nutrition_value(nutrition.get("calories_kcal"))
        protein = _validate_nutrition_value(nutrition.get("protein_g"))
        fat = _validate_nutrition_value(nutrition.get("fat_g"))
        carbs = _validate_nutrition_value(nutrition.get("carbs_g"))
        nut_basis = nutrition.get("basis", "per_serving")
        nut_source = nutrition.get("source", "manual")

    rec = await db.fetchrow(
        """
        INSERT INTO recipes
            (user_id, name, description, emoji, servings, cook_time_minutes,
             category, tags, source_url, source_type,
             is_editorial, visibility, editorial_status,
             source_platform, source_author, trend_score,
             editorial_image_url, content_slug,
             calories_kcal, protein_g, fat_g, carbs_g, nutrition_basis, nutrition_source)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
                TRUE, 'private', 'draft',
                $11,$12,$13,
                $14,$15,
                $16,$17,$18,$19,$20,$21)
        RETURNING *
        """,
        editorial_user_id,
        name.strip(),
        (description or "").strip() or None,
        emoji,
        servings,
        cook_time_minutes,
        category,
        tags or [],
        source_url,
        "editorial",
        source_platform,
        source_author,
        trend_score,
        editorial_image_url,
        slug,
        calories, protein, fat, carbs, nut_basis, nut_source,
    )

    # Insert ingredients
    for i, ing in enumerate(ingredients or []):
        ing_name = (ing.get("name") or "").strip()
        if not ing_name:
            continue
        raw_qty = ing.get("qty")
        qty_val = None
        if raw_qty not in (None, "", 0):
            try:
                qty_val = float(raw_qty)
            except (TypeError, ValueError):
                qty_val = None
        await db.execute(
            "INSERT INTO ingredients (recipe_id, name, qty, unit, category, sort_order) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            rec["id"], ing_name, qty_val,
            (ing.get("unit") or "").strip(),
            ing.get("category") or "прочее",
            i,
        )

    # Insert steps
    for i, step in enumerate(steps or []):
        step_text = (step.get("text") or "").strip()
        if not step_text:
            continue
        await db.execute(
            "INSERT INTO recipe_steps (recipe_id, step_number, text) VALUES ($1,$2,$3)",
            rec["id"], step.get("step_number", i + 1), step_text,
        )

    return _recipe_to_dict(rec)


# ── Get / List ───────────────────────────────────────────────────────────────

async def get_editorial_recipe(
    db: asyncpg.Connection,
    recipe_id: int,
) -> dict | None:
    """Get an editorial recipe by id (any status)."""
    rec = await db.fetchrow(
        "SELECT * FROM recipes WHERE id=$1 AND is_editorial=TRUE", recipe_id
    )
    if not rec:
        return None
    return await _enrich_recipe(db, rec)


async def get_editorial_recipe_by_slug(
    db: asyncpg.Connection,
    slug: str,
) -> dict | None:
    """Get a published editorial recipe by slug (public access)."""
    rec = await db.fetchrow(
        """
        SELECT * FROM recipes
        WHERE content_slug=$1
          AND is_editorial=TRUE
          AND visibility='public'
          AND editorial_status='published'
        """,
        slug,
    )
    if not rec:
        return None
    return await _enrich_recipe(db, rec)


async def list_editorial_recipes(
    db: asyncpg.Connection,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List editorial recipes, optionally filtered by status."""
    where = ["is_editorial=TRUE"]
    params: list = []
    if status:
        params.append(status)
        where.append(f"editorial_status=${len(params)}")
    where_sql = " AND ".join(where)

    params.extend([limit, offset])
    rows = await db.fetch(
        f"""
        SELECT * FROM recipes
        WHERE {where_sql}
        ORDER BY created_at DESC
        LIMIT ${len(params)-1} OFFSET ${len(params)}
        """,
        *params,
    )
    return [_recipe_to_dict(r) for r in rows]


# ── State transitions ────────────────────────────────────────────────────────

async def approve_editorial_recipe(
    db: asyncpg.Connection,
    recipe_id: int,
) -> dict | None:
    """Approve a draft editorial recipe."""
    rec = await db.fetchrow(
        "SELECT * FROM recipes WHERE id=$1 AND is_editorial=TRUE", recipe_id
    )
    if not rec:
        return None
    if rec["editorial_status"] not in ("draft", "archived"):
        raise ValueError(f"Cannot approve from status '{rec['editorial_status']}'")

    await db.execute(
        "UPDATE recipes SET editorial_status='approved', updated_at=NOW() WHERE id=$1",
        recipe_id,
    )
    return {"ok": True, "recipe_id": recipe_id, "status": "approved"}


async def publish_editorial_recipe(
    db: asyncpg.Connection,
    recipe_id: int,
) -> dict | None:
    """Mark editorial recipe as published (after Telegram post)."""
    rec = await db.fetchrow(
        "SELECT * FROM recipes WHERE id=$1 AND is_editorial=TRUE", recipe_id
    )
    if not rec:
        return None
    if rec["editorial_status"] != "approved":
        raise ValueError(f"Cannot publish from status '{rec['editorial_status']}'")

    await db.execute(
        """
        UPDATE recipes
        SET editorial_status='published',
            visibility='public',
            published_at=NOW(),
            updated_at=NOW()
        WHERE id=$1
        """,
        recipe_id,
    )
    return {"ok": True, "recipe_id": recipe_id, "status": "published"}


async def archive_editorial_recipe(
    db: asyncpg.Connection,
    recipe_id: int,
) -> dict | None:
    """Archive an editorial recipe."""
    rec = await db.fetchrow(
        "SELECT * FROM recipes WHERE id=$1 AND is_editorial=TRUE", recipe_id
    )
    if not rec:
        return None

    await db.execute(
        "UPDATE recipes SET editorial_status='archived', updated_at=NOW() WHERE id=$1",
        recipe_id,
    )
    return {"ok": True, "recipe_id": recipe_id, "status": "archived"}


# ── Clone to user ────────────────────────────────────────────────────────────

async def clone_editorial_recipe_to_user(
    db: asyncpg.Connection,
    editorial_recipe_id: int,
    user_id: int,
) -> dict:
    """
    Save a copy of an editorial recipe to user's personal library.
    Returns {"recipe_id": int, "already_saved": bool}.
    """
    # Check if already cloned
    existing = await db.fetchrow(
        """
        SELECT id FROM recipes
        WHERE user_id=$1 AND source_editorial_recipe_id=$2
        """,
        user_id, editorial_recipe_id,
    )
    if existing:
        return {"recipe_id": existing["id"], "already_saved": True}

    # Fetch original
    orig = await db.fetchrow(
        "SELECT * FROM recipes WHERE id=$1 AND is_editorial=TRUE "
        "AND visibility='public' AND editorial_status='published'",
        editorial_recipe_id,
    )
    if not orig:
        raise ValueError("Editorial recipe not found or not published")

    # Create personal copy
    new_rec = await db.fetchrow(
        """
        INSERT INTO recipes
            (user_id, name, description, emoji, servings, cook_time_minutes,
             category, tags, source_url, source_type,
             editorial_image_url,
             calories_kcal, protein_g, fat_g, carbs_g, nutrition_basis, nutrition_source,
             is_editorial, visibility, source_editorial_recipe_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'editorial_clone',
                $10,
                $11,$12,$13,$14,$15,$16,
                FALSE, 'private', $17)
        RETURNING id
        """,
        user_id,
        orig["name"],
        orig["description"],
        orig["emoji"],
        orig["servings"],
        orig["cook_time_minutes"],
        orig["category"],
        orig["tags"],
        orig["source_url"],
        orig["editorial_image_url"],
        orig["calories_kcal"],
        orig["protein_g"],
        orig["fat_g"],
        orig["carbs_g"],
        orig["nutrition_basis"],
        orig["nutrition_source"],
        editorial_recipe_id,
    )
    new_id = new_rec["id"]

    # Copy ingredients
    ings = await db.fetch(
        "SELECT name, qty, unit, category, sort_order FROM ingredients WHERE recipe_id=$1 ORDER BY sort_order, id",
        editorial_recipe_id,
    )
    for ing in ings:
        await db.execute(
            "INSERT INTO ingredients (recipe_id, name, qty, unit, category, sort_order) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            new_id, ing["name"], ing["qty"], ing["unit"], ing["category"], ing["sort_order"],
        )

    # Copy steps
    steps = await db.fetch(
        "SELECT step_number, text FROM recipe_steps WHERE recipe_id=$1 ORDER BY step_number",
        editorial_recipe_id,
    )
    for step in steps:
        await db.execute(
            "INSERT INTO recipe_steps (recipe_id, step_number, text) VALUES ($1,$2,$3)",
            new_id, step["step_number"], step["text"],
        )

    return {"recipe_id": new_id, "already_saved": False}


# ── Internal helpers ─────────────────────────────────────────────────────────

def _validate_nutrition_value(val) -> float | None:
    """Validate nutrition value: must be >= 0 if present."""
    if val is None:
        return None
    try:
        v = float(val)
        if v < 0:
            return None
        if v > 99999:
            return None  # Absurd limit
        return v
    except (TypeError, ValueError):
        return None


def _recipe_to_dict(rec) -> dict:
    """Convert a recipe record to a plain dict."""
    # Build nutrition block
    nutrition = None
    if rec.get("calories_kcal") is not None or rec.get("protein_g") is not None:
        nutrition = {
            "calories_kcal": float(rec["calories_kcal"]) if rec.get("calories_kcal") is not None else None,
            "protein_g": float(rec["protein_g"]) if rec.get("protein_g") is not None else None,
            "fat_g": float(rec["fat_g"]) if rec.get("fat_g") is not None else None,
            "carbs_g": float(rec["carbs_g"]) if rec.get("carbs_g") is not None else None,
            "basis": rec.get("nutrition_basis") or "per_serving",
        }

    return {
        "id": rec["id"],
        "user_id": rec["user_id"],
        "name": rec["name"],
        "description": rec.get("description"),
        "emoji": rec["emoji"] or "🍽",
        "servings": rec["servings"],
        "cook_time_minutes": rec["cook_time_minutes"],
        "category": rec.get("category"),
        "tags": list(rec.get("tags") or []),
        "source_url": rec.get("source_url"),
        "source_type": rec.get("source_type"),
        "is_editorial": rec.get("is_editorial", False),
        "visibility": rec.get("visibility", "private"),
        "editorial_status": rec.get("editorial_status"),
        "source_platform": rec.get("source_platform"),
        "source_author": rec.get("source_author"),
        "trend_score": float(rec["trend_score"]) if rec.get("trend_score") is not None else None,
        "editorial_image_url": rec.get("editorial_image_url"),
        "content_slug": rec.get("content_slug"),
        "nutrition": nutrition,
        "published_at": rec["published_at"].isoformat() if rec.get("published_at") else None,
        "created_at": rec["created_at"].isoformat() if rec.get("created_at") else None,
    }


async def _enrich_recipe(db: asyncpg.Connection, rec) -> dict:
    """Add ingredients and steps to recipe dict."""
    d = _recipe_to_dict(rec)
    ings = await db.fetch(
        "SELECT name, qty, unit, category FROM ingredients WHERE recipe_id=$1 ORDER BY sort_order, id",
        rec["id"],
    )
    steps = await db.fetch(
        "SELECT step_number, text FROM recipe_steps WHERE recipe_id=$1 ORDER BY step_number",
        rec["id"],
    )
    d["ingredients"] = [
        {"name": i["name"], "qty": i["qty"], "unit": i["unit"] or "", "category": i["category"] or "прочее"}
        for i in ings
    ]
    d["steps"] = [
        {"step_number": s["step_number"], "text": s["text"]}
        for s in steps
    ]
    return d
