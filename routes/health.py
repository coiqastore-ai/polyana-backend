from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import Response
import core
from core import bot
from db import get_db
from auth import get_current_user
from config import ADMIN_CHAT_ID
import io

router = APIRouter()


@router.get("/health")
async def health():
    return {
        "status": "ok", "service": "ПОЛЯНА API v3.0",
        "rev": "audit-fixes-1",
        "db_ready": core._db_ready,
        "db_error": core._db_error,
    }


# ── GET /api/files/photo/{file_id}  (proxy a Telegram photo) ─────────────────
# Streams the image bytes through the backend so the bot token stays server-side.
# Public (no auth) — <img> tags cannot send the init-data header. file_id is opaque.

@router.get("/api/files/photo/{file_id}")
async def get_recipe_photo(file_id: str):
    if not file_id or len(file_id) > 256:
        raise HTTPException(404, "Bad file id")
    try:
        tg_file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path, buf)
    except Exception:
        raise HTTPException(404, "Photo not available")
    data = buf.getvalue()
    if not data:
        raise HTTPException(404, "Empty photo")
    # Telegram photos are JPEG
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/api/admin/migration-check")
async def migration_check(user_id: int = Depends(get_current_user), db=Depends(get_db)):
    """Structural migration verification — admin only."""
    if user_id != ADMIN_CHAT_ID:
        raise HTTPException(403, "Forbidden")
    # БЛОК 1: orphan check
    recipes_without_user = await db.fetchval(
        "SELECT COUNT(*) FROM recipes WHERE user_id IS NULL"
    )
    recipes_with_zero = await db.fetchval(
        "SELECT COUNT(*) FROM recipes WHERE user_id = 0"
    )

    # БЛОК 1: priority check sample (first 10 rows)
    priority_rows = await db.fetch("""
        SELECT r.id,
               r.user_id,
               r.added_by_user_id,
               CASE
                 WHEN r.added_by_user_id IS NOT NULL
                      THEN r.user_id = r.added_by_user_id
                 ELSE NULL
               END AS priority_correct
        FROM recipes r
        LIMIT 10
    """)

    # БЛОК 1: duplicates in event_recipes
    dup_count = await db.fetchval("""
        SELECT COUNT(*) FROM (
            SELECT event_id, recipe_id FROM event_recipes
            GROUP BY event_id, recipe_id HAVING COUNT(*) > 1
        ) x
    """)

    # БЛОК 4: indexes on recipes
    indexes = await db.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'recipes' ORDER BY indexname"
    )

    # БЛОК 4: constraints on recipes
    constraints = await db.fetch("""
        SELECT conname, contype, pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE conrelid = 'recipes'::regclass
        ORDER BY conname
    """)

    # event_recipes columns (verify added_by_id exists)
    er_columns = await db.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name='event_recipes' ORDER BY ordinal_position"
    )

    # Analytics funnel snapshot (deploy verification + lightweight K-factor dashboard)
    try:
        ae = await db.fetch(
            "SELECT event_type, COUNT(*) c, COUNT(DISTINCT user_id) u FROM analytics_events GROUP BY event_type"
        )
        analytics = {r["event_type"]: {"count": r["c"], "users": r["u"]} for r in ae}
        joined = await db.fetchval("SELECT COUNT(*) FROM analytics_events WHERE event_type='guest_joined'")
        became = await db.fetchval("SELECT COUNT(*) FROM analytics_events WHERE event_type='guest_became_organizer'")
        avg_guests = await db.fetchval(
            "SELECT COALESCE(AVG(c),0) FROM (SELECT event_ref, COUNT(*) c FROM analytics_events "
            "WHERE event_type='guest_joined' GROUP BY event_ref) t"
        )
        g2o = (became / joined) if joined else 0.0
        analytics["_guest_to_organizer"] = round(g2o, 3)
        analytics["_k_factor"] = round(float(avg_guests or 0) * g2o, 3)
    except Exception as e:
        analytics = {"error": type(e).__name__}

    return {
        "блок1_recipes_without_user": recipes_without_user,
        "блок1_recipes_user_id_zero": recipes_with_zero,
        "блок1_priority_sample": [dict(r) for r in priority_rows],
        "блок1_event_recipes_duplicates": dup_count,
        "блок4_indexes_on_recipes": [{"name": r["indexname"], "def": r["indexdef"]} for r in indexes],
        "блок4_constraints_on_recipes": [{"name": r["conname"], "type": r["contype"], "def": r["def"]} for r in constraints],
        "event_recipes_columns": [r["column_name"] for r in er_columns],
        "analytics": analytics,
    }
