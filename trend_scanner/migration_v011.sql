-- Trend Scanner v0.1.1 migration
-- Idempotent — safe to run on any existing DB

-- New columns on trend_candidates
ALTER TABLE trend_candidates ADD COLUMN IF NOT EXISTS content_type TEXT;
ALTER TABLE trend_candidates ADD COLUMN IF NOT EXISTS trend_confidence NUMERIC(5,2);
ALTER TABLE trend_candidates ADD COLUMN IF NOT EXISTS canonical_dish_name TEXT;
ALTER TABLE trend_candidates ADD COLUMN IF NOT EXISTS discovery_source_url TEXT;
ALTER TABLE trend_candidates ADD COLUMN IF NOT EXISTS discovery_source_title TEXT;
ALTER TABLE trend_candidates ADD COLUMN IF NOT EXISTS cross_source_score NUMERIC(5,2);
ALTER TABLE trend_candidates ADD COLUMN IF NOT EXISTS engagement_velocity NUMERIC(10,2);
ALTER TABLE trend_candidates ADD COLUMN IF NOT EXISTS age_hours NUMERIC(10,2);
ALTER TABLE trend_candidates ADD COLUMN IF NOT EXISTS source_count INT DEFAULT 1;
ALTER TABLE trend_candidates ADD COLUMN IF NOT EXISTS all_sources TEXT[];
ALTER TABLE trend_candidates ADD COLUMN IF NOT EXISTS all_source_urls TEXT[];

-- Index on content_type
CREATE INDEX IF NOT EXISTS idx_tc_content_type ON trend_candidates(content_type);

-- Index on canonical_dish_name for dedupe
CREATE INDEX IF NOT EXISTS idx_tc_canonical_dish ON trend_candidates(canonical_dish_name);

-- Index on trend_confidence
CREATE INDEX IF NOT EXISTS idx_tc_confidence ON trend_candidates(trend_confidence DESC NULLS LAST);
