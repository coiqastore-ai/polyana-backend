DO $$
BEGIN
    -- Editorial fields on recipes
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='recipes' AND column_name='is_editorial') THEN
        ALTER TABLE recipes ADD COLUMN is_editorial BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='recipes' AND column_name='visibility') THEN
        ALTER TABLE recipes ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='recipes' AND column_name='editorial_status') THEN
        ALTER TABLE recipes ADD COLUMN editorial_status TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='recipes' AND column_name='source_platform') THEN
        ALTER TABLE recipes ADD COLUMN source_platform TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='recipes' AND column_name='source_author') THEN
        ALTER TABLE recipes ADD COLUMN source_author TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='recipes' AND column_name='trend_score') THEN
        ALTER TABLE recipes ADD COLUMN trend_score NUMERIC(5,2);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='recipes' AND column_name='trend_discovered_at') THEN
        ALTER TABLE recipes ADD COLUMN trend_discovered_at TIMESTAMPTZ;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='recipes' AND column_name='published_at') THEN
        ALTER TABLE recipes ADD COLUMN published_at TIMESTAMPTZ;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='recipes' AND column_name='content_slug') THEN
        ALTER TABLE recipes ADD COLUMN content_slug TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='recipes' AND column_name='description') THEN
        ALTER TABLE recipes ADD COLUMN description TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='recipes' AND column_name='editorial_image_url') THEN
        ALTER TABLE recipes ADD COLUMN editorial_image_url TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='recipes' AND column_name='source_editorial_recipe_id') THEN
        ALTER TABLE recipes ADD COLUMN source_editorial_recipe_id INT REFERENCES recipes(id) ON DELETE SET NULL;
    END IF;
END $$;

-- Unique slug index (only when slug is not null)
CREATE UNIQUE INDEX IF NOT EXISTS idx_recipes_content_slug
    ON recipes(content_slug) WHERE content_slug IS NOT NULL;

-- Prevent duplicate clones: one user can save an editorial recipe only once
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_editorial_clone
    ON recipes(user_id, source_editorial_recipe_id)
    WHERE source_editorial_recipe_id IS NOT NULL;
