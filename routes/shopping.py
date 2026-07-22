from fastapi import APIRouter, HTTPException, Depends
from config import _UNIT_CANON, _TASTE_UNITS, CATEGORY_ORDER
from utils import categorize_ingredient
from db import get_db, track
from auth import get_current_user
import asyncpg
import logging

log = logging.getLogger("polyana")

router = APIRouter()


def _fmt_qty(qty: float | None) -> str:
    """Format a float quantity to a clean string (1.5 → '1.5', 2.0 → '2')."""
    if qty is None or qty == 0:
        return ""
    if qty == int(qty):
        return str(int(qty))
    return f"{qty:.2f}".rstrip("0").rstrip(".")


def _norm_name(name: str) -> str:
    """Grouping key for the same product (lowercase, whitespace-collapsed)."""
    return " ".join((name or "").lower().split())


def _merge_measures(entries: list) -> str:
    """entries: list of (qty_float, unit_str) for ONE product. Sum per dimension
    (mass→г/кг, vol→мл/л, count→шт), list unknown units separately, fold
    unquantified ('по вкусу') in. Returns one human display string."""
    mass = vol = count = 0.0
    raw: dict = {}
    taste = False
    for qty, unit in entries:
        u = (unit or "").strip().lower()
        q = qty or 0.0
        c = _UNIT_CANON.get(u)
        if c:
            dim, f = c
            if dim == "mass":
                mass += q * f
            elif dim == "vol":
                vol += q * f
            else:
                count += q * f
        elif u in _TASTE_UNITS:
            taste = True
        elif q > 0:
            key = (unit or "").strip()
            raw[key] = raw.get(key, 0.0) + q
        else:
            taste = True
    parts = []
    if mass > 0:
        parts.append(f"{_fmt_qty(mass / 1000)} кг" if mass >= 1000 else f"{_fmt_qty(mass)} г")
    if vol > 0:
        parts.append(f"{_fmt_qty(vol / 1000)} л" if vol >= 1000 else f"{_fmt_qty(vol)} мл")
    if count > 0:
        parts.append(f"{_fmt_qty(count)} шт")
    for u, q in raw.items():
        parts.append(f"{_fmt_qty(q)} {u}".strip())
    if not parts and taste:
        return "по вкусу"
    return " + ".join(parts)


_last_gen_error: dict[int, str] = {}  # TEMP diagnostics: last generation error per event


async def _generate_shopping_list(event_id: int, db) -> int:
    """Aggregate ingredients from all event recipes into shopping_items.
    Deletes previously generated items, inserts fresh aggregated ones.
    Returns number of items generated."""

    _last_gen_error.pop(event_id, None)

    rows = await db.fetch(
        """
        SELECT i.name, i.qty, i.unit, i.category, er.servings_multiplier
        FROM event_recipes er
        JOIN ingredients i ON i.recipe_id = er.recipe_id
        WHERE er.event_id = $1
        """,
        event_id,
    )

    # Group by normalized product NAME (not name+unit), collecting every (qty,unit)
    # entry so the same product across recipes/units merges into one line.
    agg: dict = {}
    for row in rows:
        raw_name = (row["name"] or "").strip()
        if not raw_name:
            continue  # skip ingredients with empty/NULL name — never crash the list
        key = _norm_name(raw_name)
        try:
            mult = float(row["servings_multiplier"] or 1.0)
        except (TypeError, ValueError):
            mult = 1.0
        try:
            qty = (float(row["qty"]) if row["qty"] else 0.0) * mult
        except (TypeError, ValueError):
            qty = 0.0
        g = agg.get(key)
        if g is None:
            g = {"name": raw_name, "category": row["category"] or "прочее", "entries": []}
            agg[key] = g
        g["entries"].append((qty, (row["unit"] or "").strip()))

    # Preserve "bought" state across regeneration (key by lower name + unit)
    prev = await db.fetch(
        "SELECT name, unit, bought FROM shopping_items WHERE event_id=$1 AND is_generated=TRUE",
        event_id,
    )
    bought_state = {
        _norm_name(p["name"]): p["bought"]
        for p in prev
        if (p["name"] or "").strip()
    }

    # Remove previously generated items (keep manual ones)
    await db.execute(
        "DELETE FROM shopping_items WHERE event_id=$1 AND is_generated=TRUE", event_id
    )

    # Insert aggregated items — per-row guarded so one bad row can't wipe the list
    inserted = 0
    for key, item in agg.items():
        display_qty = _merge_measures(item["entries"]) or None
        was_bought = bought_state.get(key, False)
        try:
            await db.execute(
                """
                INSERT INTO shopping_items (event_id, name, quantity, qty, unit, category, is_generated, bought)
                VALUES ($1,$2,$3,$4,$5,$6,TRUE,$7)
                """,
                event_id, item["name"], display_qty, None, "", item["category"], was_bought,
            )
            inserted += 1
        except asyncpg.UndefinedColumnError:
            # Older schema — insert what the base table guarantees
            await db.execute(
                "INSERT INTO shopping_items (event_id, name, quantity, bought) VALUES ($1,$2,$3,$4)",
                event_id, item["name"], display_qty, was_bought,
            )
            inserted += 1
        except Exception as e:
            log.exception("shopping insert failed for event %s item %r", event_id, item.get("name"))
            _last_gen_error[event_id] = f"{type(e).__name__}: {e}"
            continue
    log.info("shopping generated for event %s: %s/%s items", event_id, inserted, len(agg))

    return inserted


async def _resync_shopping_if_exists(event_id: int, db) -> None:
    """Regenerate the shopping list, but only if one was already generated for
    this event — so adding/removing a recipe keeps an existing list in sync
    without building one for events the user never opened shopping for."""
    has_generated = await db.fetchval(
        "SELECT 1 FROM shopping_items WHERE event_id=$1 AND is_generated=TRUE LIMIT 1", event_id
    )
    if has_generated:
        await _generate_shopping_list(event_id, db)


@router.get("/api/events/{event_id}/shopping")
async def get_shopping_list(
    event_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if ev["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")

    # Auto-generate if no generated items exist yet
    has_generated = await db.fetchval(
        "SELECT 1 FROM shopping_items WHERE event_id=$1 AND is_generated=TRUE LIMIT 1", event_id
    )
    if not has_generated:
        try:
            await _generate_shopping_list(event_id, db)
        except Exception as e:
            # Never let generation failure blank the whole shopping screen —
            # log the real cause and fall through to whatever items exist.
            log.exception("shopping auto-generate failed for event %s", event_id)
            _last_gen_error[event_id] = f"{type(e).__name__}: {e}"

    items = await db.fetch(
        "SELECT * FROM shopping_items WHERE event_id=$1 ORDER BY category, name", event_id
    )
    total = len(items)
    bought_count = sum(1 for i in items if i["bought"])

    # Group by category
    grouped: dict[str, list] = {}
    for item in items:
        cat = item["category"] or "прочее"
        grouped.setdefault(cat, []).append({
            "id": item["id"],
            "name": item["name"],
            "qty": item["qty"],
            "unit": item["unit"] or "",
            "quantity": item["quantity"] or "",
            "category": cat,
            "bought": bool(item["bought"]),
            "is_generated": bool(item["is_generated"]),
        })

    # Sort categories by known order
    def cat_sort(cat):
        try:
            return CATEGORY_ORDER.index(cat)
        except ValueError:
            return 99

    categories = [
        {"name": cat, "items": grouped[cat]}
        for cat in sorted(grouped.keys(), key=cat_sort)
    ]

    # Diagnostics so the UI can explain an empty list (no recipes vs no ingredients)
    linked_recipes = await db.fetchval(
        "SELECT COUNT(*) FROM event_recipes WHERE event_id=$1", event_id
    ) or 0
    ingredient_rows = await db.fetchval(
        """
        SELECT COUNT(*) FROM event_recipes er
        JOIN ingredients i ON i.recipe_id = er.recipe_id
        WHERE er.event_id=$1 AND COALESCE(TRIM(i.name),'') <> ''
        """,
        event_id,
    ) or 0

    return {
        "items": categories, "total": total, "bought": bought_count,
        "linked_recipes": linked_recipes, "ingredient_rows": ingredient_rows,
        "debug_gen_error": _last_gen_error.get(event_id),
    }


@router.post("/api/events/{event_id}/shopping/sync")
async def sync_shopping_list(
    event_id: int, user_id: int = Depends(get_current_user), db=Depends(get_db)
):
    ev = await db.fetchrow("SELECT telegram_user_id FROM events WHERE id=$1", event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    is_collab = await db.fetchval(
        "SELECT 1 FROM collaborators WHERE event_id=$1 AND telegram_user_id=$2", event_id, user_id
    )
    if ev["telegram_user_id"] != user_id and not is_collab:
        raise HTTPException(403, "Access denied")

    count = await _generate_shopping_list(event_id, db)
    return {"generated": count}


@router.post("/api/events/{event_id}/shopping", status_code=201)
async def add_manual_shopping_item(
    event_id: int, body: dict,
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

    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    qty_str = (body.get("quantity") or "").strip() or None

    try:
        row = await db.fetchrow(
            """
            INSERT INTO shopping_items (event_id, name, quantity, is_generated, bought, added_by)
            VALUES ($1, $2, $3, FALSE, FALSE, $4)
            RETURNING *
            """,
            event_id, name, qty_str, user_id,
        )
    except asyncpg.UndefinedColumnError:
        # Older schema may be missing extended columns — fall back to minimal insert
        log.warning("shopping_items missing extended columns; minimal insert for event %s", event_id)
        row = await db.fetchrow(
            "INSERT INTO shopping_items (event_id, name, quantity, bought) VALUES ($1,$2,$3,FALSE) RETURNING *",
            event_id, name, qty_str,
        )
    except Exception as e:
        log.exception("manual shopping add failed for event %s", event_id)
        raise HTTPException(500, f"add failed: {type(e).__name__}: {e}")

    return {"id": row["id"], "name": row["name"], "quantity": row["quantity"],
            "bought": row["bought"], "is_generated": False}


@router.delete("/api/events/{event_id}/shopping/{item_id}", status_code=204)
async def delete_shopping_item(
    event_id: int, item_id: int,
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
    await db.execute(
        "DELETE FROM shopping_items WHERE id=$1 AND event_id=$2", item_id, event_id
    )


@router.patch("/api/events/{event_id}/shopping/{item_id}")
async def toggle_shopping_item(
    event_id: int, item_id: int, body: dict,
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

    bought = bool(body.get("bought", False))
    await db.execute(
        "UPDATE shopping_items SET bought=$1 WHERE id=$2 AND event_id=$3",
        bought, item_id, event_id,
    )
    return {"id": item_id, "bought": bought}
