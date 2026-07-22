import json, logging, asyncio, asyncpg, secrets
from fastapi import HTTPException
import core
from config import DATABASE_URL

log = logging.getLogger("polyana")


async def get_db():
    if core.pool is None:
        raise HTTPException(503, "Сервис запускается, попробуйте через секунду")
    async with core.pool.acquire() as c:
        yield c


async def track(user_id, event_type, props=None, event_ref=None, src_payload=None):
    """Fire-and-forget analytics. Own connection, swallows errors — never breaks a request.
    Server-truth for North Star (K-factor), activation and the viral loop."""
    if core.pool is None or not event_type:
        return
    try:
        async with core.pool.acquire() as c:
            await c.execute(
                "INSERT INTO analytics_events (user_id, event_type, props, event_ref, src_payload) "
                "VALUES ($1,$2,$3::jsonb,$4,$5)",
                user_id, str(event_type)[:64], json.dumps(props or {}),
                event_ref, (str(src_payload)[:128] if src_payload else None),
            )
    except Exception:
        log.exception("analytics track failed: %s", event_type)


async def init_db():
    core.pool = await asyncio.wait_for(
        asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, command_timeout=30),
        timeout=30,
    )
    async with core.pool.acquire() as c:

        # ── Create tables (target schema for fresh deploys) ───────────────────
        await c.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id               SERIAL PRIMARY KEY,
                name             TEXT NOT NULL,
                event_date       TIMESTAMPTZ,
                location         TEXT,
                description      TEXT,
                template         TEXT,
                share_token      TEXT UNIQUE,
                guests_count     INT DEFAULT 1,
                telegram_user_id BIGINT NOT NULL,
                created_at       TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS collaborators (
                id               SERIAL PRIMARY KEY,
                event_id         INT REFERENCES events(id) ON DELETE CASCADE,
                telegram_user_id BIGINT NOT NULL,
                first_name       TEXT,
                username         TEXT,
                role             TEXT DEFAULT 'collaborator',
                joined_at        TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(event_id, telegram_user_id)
            );

            -- Personal recipe library (user-owned, not event-bound)
            CREATE TABLE IF NOT EXISTS recipes (
                id                SERIAL PRIMARY KEY,
                user_id           BIGINT NOT NULL DEFAULT 0,
                name              TEXT NOT NULL,
                name_original     TEXT,
                emoji             TEXT DEFAULT '🍽',
                source_url        TEXT,
                source_type       TEXT DEFAULT 'manual',
                original_language TEXT,
                servings          INT DEFAULT 4,
                cook_time_minutes INT,
                category          TEXT,
                tags              TEXT[] DEFAULT '{}',
                times_cooked      INT DEFAULT 0,
                rating            INT,
                notes             TEXT,
                created_at        TIMESTAMPTZ DEFAULT NOW(),
                updated_at        TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS ingredients (
                id          SERIAL PRIMARY KEY,
                recipe_id   INT REFERENCES recipes(id) ON DELETE CASCADE,
                name        TEXT NOT NULL,
                qty         FLOAT,
                unit        TEXT,
                category    TEXT,
                sort_order  INT DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS recipe_steps (
                id          SERIAL PRIMARY KEY,
                recipe_id   INT REFERENCES recipes(id) ON DELETE CASCADE,
                step_number INT NOT NULL,
                text        TEXT NOT NULL
            );

            -- M2M: which recipes appear in which events
            CREATE TABLE IF NOT EXISTS event_recipes (
                id                  SERIAL PRIMARY KEY,
                event_id            INT REFERENCES events(id) ON DELETE CASCADE,
                recipe_id           INT REFERENCES recipes(id) ON DELETE CASCADE,
                servings_multiplier FLOAT DEFAULT 1.0,
                added_by_id         BIGINT NOT NULL DEFAULT 0,
                added_at            TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(event_id, recipe_id)
            );

            CREATE TABLE IF NOT EXISTS shopping_items (
                id       SERIAL PRIMARY KEY,
                event_id INT REFERENCES events(id) ON DELETE CASCADE,
                name     TEXT NOT NULL,
                quantity TEXT,
                bought   BOOLEAN DEFAULT FALSE
            );

            CREATE TABLE IF NOT EXISTS login_tokens (
                id               SERIAL PRIMARY KEY,
                token            TEXT UNIQUE NOT NULL,
                telegram_user_id BIGINT NOT NULL,
                used             BOOLEAN DEFAULT FALSE,
                created_at       TIMESTAMPTZ DEFAULT NOW(),
                expires_at       TIMESTAMPTZ NOT NULL
            );
        """)

        # ── Migration A: Column renames & additions ───────────────────────────
        await c.execute("""
            DO $$
            BEGIN
                -- events: title→name
                IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='title')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='name')
                THEN ALTER TABLE events RENAME COLUMN title TO name; END IF;

                -- events: date→event_date
                IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='date')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='event_date')
                THEN ALTER TABLE events RENAME COLUMN date TO event_date; END IF;

                -- events: add missing columns
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='share_token')
                    THEN ALTER TABLE events ADD COLUMN share_token TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='location')
                    THEN ALTER TABLE events ADD COLUMN location TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='description')
                    THEN ALTER TABLE events ADD COLUMN description TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='template')
                    THEN ALTER TABLE events ADD COLUMN template TEXT; END IF;

                -- collaborators
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='collaborators' AND column_name='first_name')
                    THEN ALTER TABLE collaborators ADD COLUMN first_name TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='collaborators' AND column_name='username')
                    THEN ALTER TABLE collaborators ADD COLUMN username TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='collaborators' AND column_name='role')
                    THEN ALTER TABLE collaborators ADD COLUMN role TEXT DEFAULT 'collaborator'; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='collaborators' AND column_name='joined_at')
                    THEN ALTER TABLE collaborators ADD COLUMN joined_at TIMESTAMPTZ DEFAULT NOW(); END IF;

                -- shopping_items: ingredient_name→name
                IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='shopping_items' AND column_name='ingredient_name')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='shopping_items' AND column_name='name')
                THEN ALTER TABLE shopping_items RENAME COLUMN ingredient_name TO name; END IF;

                -- recipes: title→name (very old schema)
                IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='title')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='name')
                THEN ALTER TABLE recipes RENAME COLUMN title TO name; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='name')
                    THEN ALTER TABLE recipes ADD COLUMN name TEXT NOT NULL DEFAULT ''; END IF;
            END $$;
        """)

        # ── Migration B: Recipes → user_id-based personal library ────────────
        await c.execute("""
            DO $$
            BEGIN
                -- Add user_id if missing (old schema used event_id instead)
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='recipes' AND column_name='user_id')
                THEN ALTER TABLE recipes ADD COLUMN user_id BIGINT; END IF;

                -- Backfill user_id from added_by_user_id
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name='recipes' AND column_name='added_by_user_id') THEN
                    UPDATE recipes SET user_id = added_by_user_id
                    WHERE user_id IS NULL AND added_by_user_id IS NOT NULL;
                END IF;

                -- Backfill user_id from event owner for recipes linked to events
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name='recipes' AND column_name='event_id') THEN
                    UPDATE recipes r SET user_id = e.telegram_user_id
                    FROM events e
                    WHERE r.event_id = e.id AND r.user_id IS NULL;
                END IF;

                -- Default any remaining nulls to -1 (orphan marker — not a real Telegram ID)
                UPDATE recipes SET user_id = -1 WHERE user_id IS NULL;

                -- Set NOT NULL now that every row has a value
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='recipes' AND column_name='user_id' AND is_nullable = 'YES'
                ) THEN
                    ALTER TABLE recipes ALTER COLUMN user_id SET NOT NULL;
                END IF;

                -- Rename cook_time_min → cook_time_minutes
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name='recipes' AND column_name='cook_time_min')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_name='recipes' AND column_name='cook_time_minutes')
                THEN ALTER TABLE recipes RENAME COLUMN cook_time_min TO cook_time_minutes; END IF;

                -- Add new columns (no-op if already present)
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='cook_time_minutes')
                    THEN ALTER TABLE recipes ADD COLUMN cook_time_minutes INT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='name_original')
                    THEN ALTER TABLE recipes ADD COLUMN name_original TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='source_type')
                    THEN ALTER TABLE recipes ADD COLUMN source_type TEXT DEFAULT 'manual'; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='original_language')
                    THEN ALTER TABLE recipes ADD COLUMN original_language TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='category')
                    THEN ALTER TABLE recipes ADD COLUMN category TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='tags')
                    THEN ALTER TABLE recipes ADD COLUMN tags TEXT[] DEFAULT '{}'; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='times_cooked')
                    THEN ALTER TABLE recipes ADD COLUMN times_cooked INT DEFAULT 0; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='rating')
                    THEN ALTER TABLE recipes ADD COLUMN rating INT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='notes')
                    THEN ALTER TABLE recipes ADD COLUMN notes TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='recipes' AND column_name='updated_at')
                    THEN ALTER TABLE recipes ADD COLUMN updated_at TIMESTAMPTZ DEFAULT NOW(); END IF;
            END $$;
        """)

        # ── Migration E: Ensure event_recipes has all required columns ──────────
        # Handles the case where event_recipes was created with an older/partial schema
        await c.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='event_recipes' AND column_name='servings_multiplier')
                    THEN ALTER TABLE event_recipes ADD COLUMN servings_multiplier FLOAT DEFAULT 1.0; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='event_recipes' AND column_name='added_by_id')
                    THEN ALTER TABLE event_recipes ADD COLUMN added_by_id BIGINT NOT NULL DEFAULT 0; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='event_recipes' AND column_name='added_at')
                    THEN ALTER TABLE event_recipes ADD COLUMN added_at TIMESTAMPTZ DEFAULT NOW(); END IF;
            END $$;
        """)

        # ── Migration G: Extend shopping_items for aggregated list ──────────────
        await c.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='shopping_items' AND column_name='qty')
                    THEN ALTER TABLE shopping_items ADD COLUMN qty FLOAT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='shopping_items' AND column_name='unit')
                    THEN ALTER TABLE shopping_items ADD COLUMN unit TEXT DEFAULT ''; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='shopping_items' AND column_name='category')
                    THEN ALTER TABLE shopping_items ADD COLUMN category TEXT DEFAULT 'прочее'; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='shopping_items' AND column_name='is_generated')
                    THEN ALTER TABLE shopping_items ADD COLUMN is_generated BOOLEAN DEFAULT FALSE; END IF;
            END $$;
        """)

        # ── Migration F: Ensure ingredients has all required columns ──────────
        await c.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='ingredients' AND column_name='qty')
                    THEN ALTER TABLE ingredients ADD COLUMN qty FLOAT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='ingredients' AND column_name='unit')
                    THEN ALTER TABLE ingredients ADD COLUMN unit TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='ingredients' AND column_name='category')
                    THEN ALTER TABLE ingredients ADD COLUMN category TEXT; END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='ingredients' AND column_name='sort_order')
                    THEN ALTER TABLE ingredients ADD COLUMN sort_order INT DEFAULT 0; END IF;
            END $$;
        """)

        # ── Migration H: shopping_items — add added_by column ─────────────────
        await c.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='shopping_items' AND column_name='added_by')
                    THEN ALTER TABLE shopping_items ADD COLUMN added_by BIGINT; END IF;
            END $$;
        """)

        # ── Migration I: recipes — store source photo file_id ─────────────────
        await c.execute("""
            ALTER TABLE recipes ADD COLUMN IF NOT EXISTS source_photo_file_id TEXT;
        """)

        # ── Migration J: shopping_items — add `quantity` (legacy table had
        #    total_grams/total_display instead). ALL inserts write `quantity`,
        #    so without this column generation and manual-add both fail. ───────
        await c.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='shopping_items' AND column_name='quantity')
                THEN
                    ALTER TABLE shopping_items ADD COLUMN quantity TEXT;
                    -- Backfill from the legacy display column if it exists
                    IF EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='shopping_items' AND column_name='total_display')
                    THEN
                        UPDATE shopping_items SET quantity = total_display
                        WHERE quantity IS NULL AND total_display IS NOT NULL;
                    END IF;
                END IF;
            END $$;
        """)

        # ── Migration K: payments — balance, ledger, invite grants ────────────
        await c.execute("""
            CREATE TABLE IF NOT EXISTS user_balance (
                telegram_user_id BIGINT PRIMARY KEY,
                balance          INT NOT NULL DEFAULT 0,   -- kopecks
                updated_at       TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS payment_txns (
                id               SERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                kind             TEXT NOT NULL,
                amount           INT NOT NULL,             -- kopecks: +credit / -debit
                balance_after    INT,
                ref              TEXT,                     -- external id (idempotency)
                meta             JSONB,
                created_at       TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_txn_kind_ref
                ON payment_txns(kind, ref) WHERE ref IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_txn_user ON payment_txns(telegram_user_id);
            CREATE TABLE IF NOT EXISTS invite_grants (
                id               SERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                event_id         INT NOT NULL,
                remaining        INT NOT NULL DEFAULT 0,
                created_at       TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_grant_user_event
                ON invite_grants(telegram_user_id, event_id);
        """)

        # ── Migration L: referrals ────────────────────────────────────────────
        await c.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referee_id   BIGINT PRIMARY KEY,
                referrer_id  BIGINT NOT NULL,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_ref_referrer ON referrals(referrer_id);
            CREATE TABLE IF NOT EXISTS referral_bonuses (
                id            SERIAL PRIMARY KEY,
                referrer_id   BIGINT NOT NULL,
                referee_id    BIGINT NOT NULL,
                source_ref    TEXT UNIQUE,        -- idempotency per charge
                amount        INT NOT NULL,       -- kopecks
                available_at  TIMESTAMPTZ NOT NULL,
                paid          BOOLEAN DEFAULT FALSE,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_refbonus_due
                ON referral_bonuses(available_at) WHERE NOT paid;
        """)

        # ── Analytics: append-only event log (North Star / K-factor / funnel) ──
        await c.execute("""
            CREATE TABLE IF NOT EXISTS analytics_events (
                id          BIGSERIAL PRIMARY KEY,
                ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                user_id     BIGINT,
                event_type  TEXT NOT NULL,
                props       JSONB NOT NULL DEFAULT '{}',
                event_ref   BIGINT,
                src_payload TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ae_type_ts ON analytics_events(event_type, ts);
            CREATE INDEX IF NOT EXISTS idx_ae_user    ON analytics_events(user_id);
            CREATE INDEX IF NOT EXISTS idx_ae_ref     ON analytics_events(event_ref);
        """)

        # ── Migration C: Seed event_recipes from old recipes.event_id ────────
        await c.execute("""
            DO $$
            BEGIN
                -- Migrate existing event_id links on recipes → event_recipes
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name='recipes' AND column_name='event_id') THEN
                    INSERT INTO event_recipes
                        (event_id, recipe_id, servings_multiplier, added_by_id, added_at)
                    SELECT r.event_id, r.id, 1.0,
                           COALESCE(r.user_id, 0),
                           COALESCE(r.created_at, NOW())
                    FROM recipes r
                    WHERE r.event_id IS NOT NULL
                    ON CONFLICT (event_id, recipe_id) DO NOTHING;
                END IF;

                -- Also migrate from event_menu_items if that legacy table still exists
                IF EXISTS (SELECT 1 FROM information_schema.tables
                           WHERE table_name='event_menu_items') THEN
                    -- Insert unique items into recipe library
                    INSERT INTO recipes
                        (user_id, name, emoji, servings, source_type, created_at)
                    SELECT COALESCE(m.added_by_user_id, e.telegram_user_id, 0),
                           m.name, m.emoji, m.servings, 'manual', m.added_at
                    FROM event_menu_items m
                    JOIN events e ON e.id = m.event_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM recipes r2
                        WHERE r2.user_id = COALESCE(m.added_by_user_id, e.telegram_user_id, 0)
                          AND r2.name = m.name
                          AND r2.created_at = m.added_at
                    );

                    -- Link them to events
                    INSERT INTO event_recipes
                        (event_id, recipe_id, servings_multiplier, added_by_id, added_at)
                    SELECT m.event_id, r.id, 1.0,
                           COALESCE(m.added_by_user_id, e.telegram_user_id, 0),
                           m.added_at
                    FROM event_menu_items m
                    JOIN events e ON e.id = m.event_id
                    JOIN recipes r
                        ON r.name = m.name
                       AND r.user_id = COALESCE(m.added_by_user_id, e.telegram_user_id, 0)
                       AND r.created_at = m.added_at
                    ON CONFLICT (event_id, recipe_id) DO NOTHING;
                END IF;
            END $$;
        """)

        # ── Migration D: Constraints ──────────────────────────────────────────
        await c.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'collaborators_event_user_uniq'
                ) THEN
                    DELETE FROM collaborators a USING collaborators b
                    WHERE a.id > b.id
                      AND a.event_id = b.event_id
                      AND a.telegram_user_id = b.telegram_user_id;
                    ALTER TABLE collaborators
                        ADD CONSTRAINT collaborators_event_user_uniq
                        UNIQUE (event_id, telegram_user_id);
                END IF;
            END $$;
        """)

        # ── Indexes ───────────────────────────────────────────────────────────
        await c.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_user        ON events(telegram_user_id);
            CREATE INDEX IF NOT EXISTS idx_collab_event       ON collaborators(event_id);
            CREATE INDEX IF NOT EXISTS idx_collab_user        ON collaborators(telegram_user_id);
            CREATE INDEX IF NOT EXISTS idx_recipes_user_id    ON recipes(user_id);
            CREATE INDEX IF NOT EXISTS idx_recipes_user_name  ON recipes(user_id, name);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_recipes_user_source_url
                ON recipes(user_id, source_url) WHERE source_url IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_event_recipes_evt  ON event_recipes(event_id);
            CREATE INDEX IF NOT EXISTS idx_event_recipes_rec  ON event_recipes(recipe_id);
            CREATE INDEX IF NOT EXISTS idx_ingredients_rec    ON ingredients(recipe_id);
            CREATE INDEX IF NOT EXISTS idx_shopping_event     ON shopping_items(event_id);
        """)

        # Drop stale duplicate indexes from old schema (simple DROP, no CONCURRENTLY needed for small DB)
        await c.execute("DROP INDEX IF EXISTS idx_recipes_user")
        await c.execute("DROP INDEX IF EXISTS idx_recipes_event")

        # ── Backfill share_token ──────────────────────────────────────────────
        rows = await c.fetch("SELECT id FROM events WHERE share_token IS NULL")
        for row in rows:
            await c.execute(
                "UPDATE events SET share_token=$1 WHERE id=$2",
                secrets.token_urlsafe(16), row["id"]
            )

    core._db_ready = True
    log.info("DB ready ✓  (recipes-as-library schema v3)")
