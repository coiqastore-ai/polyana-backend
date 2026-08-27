-- Editorial Content v1.1 migration
-- Idempotent — safe to run on top of Migration W

-- Nutrition fields
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS calories_kcal NUMERIC(8,2);
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS protein_g NUMERIC(8,2);
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS fat_g NUMERIC(8,2);
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS carbs_g NUMERIC(8,2);
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS nutrition_basis TEXT DEFAULT 'per_serving';
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS nutrition_source TEXT;

-- Approval flow fields
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS editorial_telegram_message_id BIGINT;
ALTER TABLE recipes ADD COLUMN IF NOT EXISTS editorial_telegram_chat_id BIGINT;

-- First-touch attribution
ALTER TABLE users ADD COLUMN IF NOT EXISTS acquisition_campaign TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS acquisition_recipe_id BIGINT;
