from fastapi import APIRouter, HTTPException, Depends, Query
from core import bot
from db import get_db, track
from auth import get_current_user
from parsing import parse_and_save_recipe
from llm import _llm_normalize_ingredients
from utils import categorize_ingredient
from routes.shopping import _resync_shopping_if_exists
import logging

log = logging.getLogger("polyana")

router = APIRouter()


@router.get("/api/recipes")
async def list_recipes(
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    user_id: int = Depends(get_current_user),
    db=Depends(get_db),
):
    where_parts = ["r.user_id = $1"]
    params: list = [user_id]

    if q:
        params.append(f"%{q.lower()}%")
        where_parts.append(f"LOWER(r.name) LIKE ${len(params)}")
    if category:
        params.append(category)
        where_parts.append(f"r.category = ${len(params)}")

    where_sql = " AND ".join(where_parts)
    rows = await db.fetch(
        f"""
        SELECT r.id, r.name, r.name_original, r.emoji, r.servings, r.cook_time_minutes,
               r.category, r.tags, r.times_cooked, r.rating, r.source_url, r.source_type,
               r.notes, r.created_at,
               (SELECT COUNT(*) FROM ingredients i WHERE i.recipe_id = r.id) AS ingredients_count
        FROM recipes r
        WHERE {where_sql}
        ORDER BY r.created_at DESC
        """,
        *params,
    )
    return {
        "recipes": [
            {
                "id": r["id"],
                "name": r["name"],
                "name_original": r["name_original"],
                "emoji": r["emoji"] or "🍽",
                "servings": r["servings"],
                "cook_time_minutes": r["cook_time_minutes"],
                "cook_time_min": r["cook_time_minutes"],   # compat
                "category": r["category"],
                "tags": list(r["tags"] or []),
                "times_cooked": r["times_cooked"] or 0,
                "rating": r["rating"],
                "source_url": r["source_url"],
                "source_type": r["source_type"] or "manual",
                "notes": r["notes"],
                "ingredients_count": r["ingredients_count"] or 0,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.post("/api/recipes", status_code=201)
async def create_recipe(body: dict, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")

    rec = await db.fetchrow(
        """
        INSERT INTO recipes
            (user_id, name, name_original, emoji, source_url, source_type,
             original_language, servings, cook_time_minutes, category, notes)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        RETURNING *
        """,
        user_id, name,
        body.get("name_original"),
        body.get("emoji", "🍽"),
        body.get("source_url"),
        body.get("source_type", "manual"),
        body.get("original_language"),
        body.get("servings", 4),
        body.get("cook_time_min") or body.get("cook_time_minutes"),
        body.get("category"),
        body.get("notes"),
    )

    for i, ing in enumerate(body.get("ingredients", [])):
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
            "INSERT INTO ingredients (recipe_id, name, qty, unit, category, sort_order) VALUES ($1,$2,$3,$4,$5,$6)",
            rec["id"], ing_name, qty_val,
            (ing.get("unit") or "").strip(),
            categorize_ingredient(ing_name),
            i,
        )

    for i, step in enumerate(body.get("steps", [])):
        step_text = (step.get("text") or "").strip()
        if not step_text:
            continue
        await db.execute(
            "INSERT INTO recipe_steps (recipe_id, step_number, text) VALUES ($1,$2,$3)",
            rec["id"], i + 1, step_text,
        )

    return {
        "id": rec["id"], "name": rec["name"], "emoji": rec["emoji"] or "🍽",
        "user_id": rec["user_id"], "servings": rec["servings"],
        "cook_time_minutes": rec["cook_time_minutes"],
        "created_at": rec["created_at"].isoformat() if rec["created_at"] else None,
    }


@router.patch("/api/recipes/{recipe_id}")
async def update_recipe(
    recipe_id: int, body: dict,
    user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    rec = await db.fetchrow("SELECT user_id FROM recipes WHERE id=$1", recipe_id)
    if not rec:
        raise HTTPException(404, "Recipe not found")
    if rec["user_id"] != user_id:
        raise HTTPException(403, "Access denied")

    # Update scalar fields that are present in the body
    scalar_map = {
        "name": "name",
        "emoji": "emoji",
        "servings": "servings",
        "category": "category",
        "notes": "notes",
    }
    sets, params = [], []
    for body_key, col in scalar_map.items():
        if body_key in body and body[body_key] is not None:
            params.append(body[body_key])
            sets.append(f"{col} = ${len(params)}")
    # cook time accepts either alias
    if "cook_time_min" in body or "cook_time_minutes" in body:
        params.append(body.get("cook_time_min") or body.get("cook_time_minutes"))
        sets.append(f"cook_time_minutes = ${len(params)}")
    if sets:
        params.append(recipe_id)
        await db.execute(
            f"UPDATE recipes SET {', '.join(sets)} WHERE id = ${len(params)}", *params
        )

    # Replace ingredients if the key is present (even if empty list = clear all)
    if "ingredients" in body:
        await db.execute("DELETE FROM ingredients WHERE recipe_id=$1", recipe_id)
        for i, ing in enumerate(body.get("ingredients") or []):
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
                "INSERT INTO ingredients (recipe_id, name, qty, unit, category, sort_order) VALUES ($1,$2,$3,$4,$5,$6)",
                recipe_id, ing_name, qty_val,
                (ing.get("unit") or "").strip(),
                categorize_ingredient(ing_name),
                i,
            )

    # Replace steps if present
    if "steps" in body:
        await db.execute("DELETE FROM recipe_steps WHERE recipe_id=$1", recipe_id)
        for i, step in enumerate(body.get("steps") or []):
            step_text = (step.get("text") or "").strip()
            if not step_text:
                continue
            await db.execute(
                "INSERT INTO recipe_steps (recipe_id, step_number, text) VALUES ($1,$2,$3)",
                recipe_id, i + 1, step_text,
            )

    # If ingredients changed, resync shopping for every event using this recipe
    if "ingredients" in body:
        evt_rows = await db.fetch(
            "SELECT event_id FROM event_recipes WHERE recipe_id=$1", recipe_id
        )
        for er in evt_rows:
            await _resync_shopping_if_exists(er["event_id"], db)

    return {"id": recipe_id, "ok": True}


@router.post("/api/recipes/{recipe_id}/normalize-ingredients")
async def normalize_recipe_ingredients(
    recipe_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    rec = await db.fetchrow("SELECT user_id FROM recipes WHERE id=$1", recipe_id)
    if not rec:
        raise HTTPException(404, "Recipe not found")
    if rec["user_id"] != user_id:
        raise HTTPException(403, "Access denied")

    ings = await db.fetch(
        "SELECT name FROM ingredients WHERE recipe_id=$1 ORDER BY sort_order, id", recipe_id
    )
    raw = [i["name"] for i in ings if (i["name"] or "").strip()]
    if not raw:
        return {"updated": 0}

    normalized = await _llm_normalize_ingredients(raw)

    await db.execute("DELETE FROM ingredients WHERE recipe_id=$1", recipe_id)
    for idx, ing in enumerate(normalized):
        ing_name = (ing.get("name") or "").strip()
        if not ing_name:
            continue
        await db.execute(
            "INSERT INTO ingredients (recipe_id, name, qty, unit, category, sort_order) VALUES ($1,$2,$3,$4,$5,$6)",
            recipe_id, ing_name, ing.get("qty"),
            (ing.get("unit") or "").strip(),
            ing.get("category") or categorize_ingredient(ing_name),
            idx,
        )

    # Keep shopping lists in sync for events using this recipe
    evt_rows = await db.fetch("SELECT event_id FROM event_recipes WHERE recipe_id=$1", recipe_id)
    for er in evt_rows:
        await _resync_shopping_if_exists(er["event_id"], db)

    return {"updated": len(normalized)}


@router.get("/api/recipes/{recipe_id}")
async def get_recipe(recipe_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    rec = await db.fetchrow("SELECT * FROM recipes WHERE id=$1", recipe_id)
    if not rec:
        raise HTTPException(404, "Recipe not found")

    # Access: recipe owner OR collaborator in any event that contains this recipe
    if rec["user_id"] != user_id:
        has_access = await db.fetchval(
            """
            SELECT 1 FROM event_recipes er
            JOIN collaborators c ON c.event_id = er.event_id
            WHERE er.recipe_id = $1 AND c.telegram_user_id = $2
            LIMIT 1
            """,
            recipe_id, user_id,
        )
        if not has_access:
            raise HTTPException(403, "Access denied")

    ingredients = await db.fetch(
        "SELECT * FROM ingredients WHERE recipe_id=$1 ORDER BY sort_order, id", recipe_id
    )
    steps = await db.fetch(
        "SELECT * FROM recipe_steps WHERE recipe_id=$1 ORDER BY step_number", recipe_id
    )

    rec_dict = dict(rec)
    cook_time = rec_dict.get("cook_time_minutes") or rec_dict.get("cook_time_min")

    return {
        "id": rec["id"],
        "user_id": rec["user_id"],
        "name": rec["name"],
        "name_original": rec_dict.get("name_original"),
        "emoji": rec["emoji"] or "🍽",
        "servings": rec["servings"],
        "cook_time_minutes": cook_time,
        "cook_time_min": cook_time,   # compat
        "source_url": rec_dict.get("source_url"),
        "source_type": rec_dict.get("source_type") or "manual",
        "source_photo_file_id": rec_dict.get("source_photo_file_id"),
        "category": rec_dict.get("category"),
        "tags": list(rec_dict.get("tags") or []),
        "times_cooked": rec_dict.get("times_cooked") or 0,
        "rating": rec_dict.get("rating"),
        "notes": rec_dict.get("notes"),
        "created_at": rec["created_at"].isoformat() if rec["created_at"] else None,
        "ingredients": [
            {
                "id": i["id"], "name": i["name"],
                "qty": i["qty"], "unit": i["unit"] or "",
                "category": i["category"] or "прочее",
            }
            for i in ingredients
        ],
        "steps": [
            {"step_number": s["step_number"], "text": s["text"]}
            for s in steps
        ],
    }


@router.post("/api/recipes/import-url", status_code=201)
async def import_recipe_url(
    body: dict,
    user_id: int = Depends(get_current_user),
    db=Depends(get_db),
):
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")
    if not url.startswith("http"):
        raise HTTPException(400, "Invalid URL")
    try:
        recipe = await parse_and_save_recipe(user_id, url=url)
        return recipe
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        log.error("import-url error: %s", e)
        raise HTTPException(500, f"Parsing failed: {str(e)[:200]}")


@router.post("/api/recipes/import-text", status_code=201)
async def import_recipe_text(
    body: dict,
    user_id: int = Depends(get_current_user),
    db=Depends(get_db),
):
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    try:
        recipe = await parse_and_save_recipe(user_id, text=text)
        return recipe
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        log.error("import-text error: %s", e)
        raise HTTPException(500, f"Parsing failed: {str(e)[:200]}")


@router.delete("/api/recipes/{recipe_id}", status_code=204)
async def delete_recipe_from_library(
    recipe_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    rec = await db.fetchrow("SELECT user_id FROM recipes WHERE id=$1", recipe_id)
    if not rec:
        raise HTTPException(404, "Recipe not found")
    if rec["user_id"] != user_id:
        raise HTTPException(403, "Access denied")
    # CASCADE removes ingredients, recipe_steps, event_recipes links
    await db.execute("DELETE FROM recipes WHERE id=$1", recipe_id)
