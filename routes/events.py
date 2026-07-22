from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import Response
from core import bot
from db import get_db, track
from auth import get_current_user
from utils import compute_progress, next_step_hint
from utils import categorize_ingredient
import secrets
from datetime import datetime
import asyncpg

router = APIRouter()


@router.get("/api/events")
async def list_events(user_id: int = Depends(get_current_user), db=Depends(get_db)):
    rows = await db.fetch(
        """
        SELECT e.id, e.name, e.event_date, e.location, e.template, e.share_token, e.telegram_user_id,
               (SELECT COUNT(*) FROM event_recipes er WHERE er.event_id = e.id) AS recipes_count,
               (SELECT COUNT(*) FROM shopping_items s WHERE s.event_id = e.id)  AS shopping_total,
               (SELECT COUNT(*) FROM shopping_items s WHERE s.event_id = e.id AND s.bought) AS shopping_bought,
               (SELECT COUNT(*) FROM collaborators c WHERE c.event_id = e.id)   AS collab_count
        FROM events e
        WHERE e.telegram_user_id = $1
           OR EXISTS (SELECT 1 FROM collaborators c WHERE c.event_id = e.id AND c.telegram_user_id = $1)
        ORDER BY e.event_date ASC NULLS LAST
        """,
        user_id,
    )
    events = []
    for r in rows:
        rc = r["recipes_count"] or 0
        st = r["shopping_total"] or 0
        sb = r["shopping_bought"] or 0
        events.append({
            "id": r["id"],
            "name": r["name"],
            "event_date": r["event_date"].isoformat() if r["event_date"] else None,
            "location": r["location"],
            "template": r["template"],
            "share_token": r["share_token"],
            "guests_count": (r["collab_count"] or 0) + 1,
            "recipes_count": rc,
            "shopping_items_count": st,
            "progress_percent": compute_progress(rc, st, sb),
            "is_owner": r["telegram_user_id"] == user_id,
            "owner_id": r["telegram_user_id"],
        })
    return {"events": events}


@router.post("/api/events", status_code=201)
async def create_event(body: dict, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")

    # K-factor: read state BEFORE creating this event.
    #  prior_owned == 0 AND has_joined > 0  → a guest just converted into an organizer.
    prior_owned = await db.fetchval(
        "SELECT COUNT(*) FROM events WHERE telegram_user_id=$1", user_id
    )
    has_joined = await db.fetchval(
        "SELECT COUNT(*) FROM collaborators WHERE telegram_user_id=$1 AND role<>'owner'", user_id
    )

    event_date = None
    raw = body.get("event_date")
    if raw:
        try:
            event_date = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, "Invalid event_date (ISO 8601 expected)")

    share_token = secrets.token_urlsafe(16)
    row = await db.fetchrow(
        """
        INSERT INTO events (name, event_date, location, description, template, share_token, telegram_user_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id, name, share_token, telegram_user_id
        """,
        name, event_date,
        body.get("location"), body.get("description"), body.get("template"),
        share_token, user_id,
    )
    await db.execute(
        """
        INSERT INTO collaborators (event_id, telegram_user_id, first_name, username, role)
        VALUES ($1,$2,$3,$4,'owner') ON CONFLICT DO NOTHING
        """,
        row["id"], user_id,
        body.get("owner_first_name", ""), body.get("owner_username", ""),
    )
    await track(user_id, "event_created", props={"event_id": row["id"]}, event_ref=row["id"])
    if (prior_owned or 0) == 0 and (has_joined or 0) > 0:
        await track(user_id, "guest_became_organizer", props={"event_id": row["id"]}, event_ref=row["id"])
    return {"id": row["id"], "name": row["name"], "share_token": row["share_token"], "owner_id": user_id}


@router.get("/api/events/shared/{event_id}")
async def get_shared_event(event_id: int, db=Depends(get_db)):
    row = await db.fetchrow(
        "SELECT id, name, event_date, location, guests_count FROM events WHERE id=$1", event_id
    )
    if not row:
        raise HTTPException(404, "Not found")
    return {
        "id": row["id"], "name": row["name"],
        "event_date": row["event_date"].isoformat() if row["event_date"] else None,
        "location": row["location"], "guests_count": row["guests_count"], "read_only": True,
    }


@router.get("/api/events/{event_id}")
async def get_event(event_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    row = await db.fetchrow("SELECT * FROM events WHERE id=$1", event_id)
    if not row:
        raise HTTPException(404, "Event not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if row["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")

    collabs = await db.fetch(
        "SELECT * FROM collaborators WHERE event_id=$1 ORDER BY joined_at ASC", event_id
    )

    # Recipes via event_recipes M2M join
    recipes = await db.fetch(
        """
        SELECT r.id, r.name, r.emoji, r.servings, r.cook_time_minutes,
               r.user_id AS recipe_owner_id,
               er.servings_multiplier, er.added_by_id, er.added_at AS linked_at,
               (SELECT COUNT(*) FROM ingredients i WHERE i.recipe_id = r.id) AS ingredients_count
        FROM event_recipes er
        JOIN recipes r ON r.id = er.recipe_id
        WHERE er.event_id = $1
        ORDER BY er.added_at ASC
        """,
        event_id,
    )

    shop_row = await db.fetchrow(
        "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE bought) AS bought FROM shopping_items WHERE event_id=$1",
        event_id,
    )
    rc, st, sb = len(recipes), (shop_row["total"] or 0), (shop_row["bought"] or 0)

    # Collaborator name lookup
    collab_names = {c["telegram_user_id"]: c["first_name"] or "Гость" for c in collabs}

    return {
        "id": row["id"],
        "name": row["name"],
        "event_date": row["event_date"].isoformat() if row["event_date"] else None,
        "location": row.get("location") or "",
        "description": row.get("description") or "",
        "template": row.get("template") or "",
        "share_token": row["share_token"],
        "owner_id": row["telegram_user_id"],
        "is_owner": row["telegram_user_id"] == user_id,
        "progress_percent": compute_progress(rc, st, sb),
        "next_step": next_step_hint(rc),
        "collaborators": [
            {
                "user_id": c["telegram_user_id"],
                "first_name": c["first_name"] or "Гость",
                "username": c["username"],
                "role": c["role"],
                "recipes_count": sum(1 for r in recipes if r["added_by_id"] == c["telegram_user_id"]),
            }
            for c in collabs
        ],
        "recipes": [
            {
                "id": r["id"],
                "name": r["name"],
                "emoji": r["emoji"] or "🍽",
                "servings": r["servings"],
                "cook_time_min": r["cook_time_minutes"],        # compat alias
                "cook_time_minutes": r["cook_time_minutes"],
                "servings_multiplier": float(r["servings_multiplier"] or 1.0),
                "ingredients_count": r["ingredients_count"] or 0,
                "added_by": {
                    "user_id": r["added_by_id"],
                    "first_name": collab_names.get(r["added_by_id"], "Гость"),
                },
                "added_at": r["linked_at"].isoformat() if r["linked_at"] else None,
            }
            for r in recipes
        ],
    }


@router.patch("/api/events/{event_id}")
async def update_event(event_id: int, body: dict, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    owner = await db.fetchval("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if owner != user_id:
        raise HTTPException(403, "Access denied")
    allowed = ("name", "event_date", "location", "description", "guests_count")
    fields = {k: v for k, v in body.items() if k in allowed and v is not None}
    if not fields:
        raise HTTPException(400, "No updatable fields")
    sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(fields))
    row = await db.fetchrow(
        f"UPDATE events SET {sets} WHERE id=$1 RETURNING *", event_id, *fields.values()
    )
    return dict(row)


@router.delete("/api/events/{event_id}", status_code=204)
async def delete_event(event_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    owner = await db.fetchval("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if owner is None:
        raise HTTPException(404, "Event not found")
    if owner != user_id:
        raise HTTPException(403, "Access denied")
    # Explicitly remove children first — don't rely on FK ON DELETE CASCADE,
    # since legacy tables in production may have been created without it.
    # (Recipes are library-owned and shared, so they are NOT deleted here.)
    await db.execute("DELETE FROM shopping_items WHERE event_id=$1", event_id)
    await db.execute("DELETE FROM event_recipes  WHERE event_id=$1", event_id)
    await db.execute("DELETE FROM collaborators   WHERE event_id=$1", event_id)
    # Legacy table from an older schema — clean up only if it still exists.
    try:
        await db.execute("DELETE FROM event_menu_items WHERE event_id=$1", event_id)
    except asyncpg.UndefinedTableError:
        pass
    await db.execute("DELETE FROM events WHERE id=$1", event_id)


@router.post("/api/events/{event_id}/recipes", status_code=201)
async def add_recipe_to_event(event_id: int, body: dict, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    from routes.shopping import _resync_shopping_if_exists
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if ev["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")

    recipe_id = body.get("recipe_id")

    if recipe_id:
        # ── Mode 1: link existing recipe from user's library ──────────────────
        rec = await db.fetchrow(
            "SELECT id, name, emoji, servings FROM recipes WHERE id=$1 AND user_id=$2",
            int(recipe_id), user_id
        )
        if not rec:
            raise HTTPException(404, "Recipe not found in your library")

        mult = float(body.get("servings_multiplier") or 1.0)
        await db.execute(
            """
            INSERT INTO event_recipes (event_id, recipe_id, servings_multiplier, added_by_id)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (event_id, recipe_id) DO UPDATE
                SET servings_multiplier = EXCLUDED.servings_multiplier
            """,
            event_id, rec["id"], mult, user_id,
        )
        await _resync_shopping_if_exists(event_id, db)
        return {
            "id": rec["id"], "name": rec["name"],
            "emoji": rec["emoji"] or "🍽",
            "servings": rec["servings"],
            "servings_multiplier": mult,
        }

    else:
        # ── Mode 2: create new recipe in library, then link to event ──────────
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name required")

        rec = await db.fetchrow(
            """
            INSERT INTO recipes
                (user_id, name, emoji, servings, cook_time_minutes, source_url, source_type)
            VALUES ($1,$2,$3,$4,$5,$6,'manual')
            RETURNING *
            """,
            user_id, name,
            body.get("emoji", "🍽"),
            body.get("servings", 4),
            body.get("cook_time_min") or body.get("cook_time_minutes"),
            body.get("source_url"),
        )

        # Persist ingredients
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

        # Persist steps
        for i, step in enumerate(body.get("steps", [])):
            step_text = (step.get("text") or "").strip()
            if not step_text:
                continue
            await db.execute(
                "INSERT INTO recipe_steps (recipe_id, step_number, text) VALUES ($1,$2,$3)",
                rec["id"], i + 1, step_text,
            )

        # Link to event via event_recipes
        await db.execute(
            """
            INSERT INTO event_recipes (event_id, recipe_id, servings_multiplier, added_by_id)
            VALUES ($1,$2,1.0,$3)
            ON CONFLICT (event_id, recipe_id) DO NOTHING
            """,
            event_id, rec["id"], user_id,
        )

        await _resync_shopping_if_exists(event_id, db)
        return {
            "id": rec["id"], "name": rec["name"],
            "emoji": rec["emoji"] or "🍽",
            "servings": rec["servings"],
            "servings_multiplier": 1.0,
            "added_at": rec["created_at"].isoformat() if rec["created_at"] else None,
        }


@router.patch("/api/events/{event_id}/recipes/{recipe_id}")
async def update_event_recipe(
    event_id: int, recipe_id: int, body: dict,
    user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if ev["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")

    mult = float(body.get("servings_multiplier") or 1.0)
    await db.execute(
        "UPDATE event_recipes SET servings_multiplier=$1 WHERE event_id=$2 AND recipe_id=$3",
        mult, event_id, recipe_id,
    )
    return {"servings_multiplier": mult}


@router.delete("/api/events/{event_id}/recipes/{recipe_id}", status_code=204)
async def unlink_recipe_from_event(
    event_id: int, recipe_id: int,
    user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    from routes.shopping import _resync_shopping_if_exists
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    er = await db.fetchrow(
        "SELECT added_by_id FROM event_recipes WHERE event_id=$1 AND recipe_id=$2",
        event_id, recipe_id,
    )
    if not er:
        raise HTTPException(404, "Recipe not linked to this event")
    rec_owner = await db.fetchval("SELECT user_id FROM recipes WHERE id=$1", recipe_id)
    if ev["telegram_user_id"] != user_id and er["added_by_id"] != user_id and rec_owner != user_id:
        raise HTTPException(403, "Access denied")
    await db.execute(
        "DELETE FROM event_recipes WHERE event_id=$1 AND recipe_id=$2", event_id, recipe_id
    )
    await _resync_shopping_if_exists(event_id, db)


@router.get("/api/events/{event_id}/share-link")
async def get_share_link(event_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    row = await db.fetchrow("SELECT id, name, event_date, telegram_user_id FROM events WHERE id=$1", event_id)
    if not row:
        raise HTTPException(404, "Not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if row["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")
    return {
        "share_link": f"https://t.me/reciptesbot?start=event_{event_id}",
        "event_name": row["name"],
        "event_date": row["event_date"].isoformat() if row["event_date"] else None,
    }


@router.post("/api/events/{event_id}/join")
async def join_event(event_id: int, body: dict, user_id: int = Depends(get_current_user), db=Depends(get_db)):
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Not found")
    was_new = not await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    await db.execute(
        """
        INSERT INTO collaborators (event_id, telegram_user_id, first_name, username, role)
        VALUES ($1,$2,$3,$4,'collaborator')
        ON CONFLICT (event_id, telegram_user_id) DO UPDATE SET first_name=EXCLUDED.first_name
        """,
        event_id, user_id, body.get("first_name", ""), body.get("username", ""),
    )
    if was_new and ev["telegram_user_id"] != user_id:
        await track(user_id, "guest_joined",
                    props={"event_id": event_id, "owner_id": ev["telegram_user_id"]},
                    event_ref=event_id)
    return {"status": "joined", "role": "collaborator"}
