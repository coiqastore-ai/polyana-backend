-- Trend Scanner v0.1 migration
-- Idempotent — safe to run on any existing DB

-- Trend candidates table
CREATE TABLE IF NOT EXISTS trend_candidates (
    id BIGSERIAL PRIMARY KEY,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    source_platform TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_author TEXT,

    title TEXT,
    description TEXT,
    published_at TIMESTAMPTZ,

    raw_engagement JSONB,
    raw_metadata JSONB,

    normalized_title TEXT,
    recipe_fingerprint TEXT,

    freshness_score NUMERIC(5,2),
    engagement_score NUMERIC(5,2),
    visual_score NUMERIC(5,2),
    simplicity_score NUMERIC(5,2),
    ru_availability_score NUMERIC(5,2),
    poliana_fit_score NUMERIC(5,2),
    trend_score NUMERIC(5,2),

    status TEXT NOT NULL DEFAULT 'candidate',

    rejection_reason TEXT,
    created_editorial_recipe_id INT,

    UNIQUE(source_url)
);

CREATE INDEX IF NOT EXISTS idx_tc_status ON trend_candidates(status);
CREATE INDEX IF NOT EXISTS idx_tc_score ON trend_candidates(trend_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_tc_platform ON trend_candidates(source_platform);

-- Scan runs table
CREATE TABLE IF NOT EXISTS trend_scan_runs (
    id UUID PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    total_found INT DEFAULT 0,
    total_unique INT DEFAULT 0,
    shortlisted INT DEFAULT 0,
    llm_cost_usd NUMERIC(10,4),
    error TEXT
);

-- Source runs table
CREATE TABLE IF NOT EXISTS trend_source_runs (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL,
    source_platform TEXT NOT NULL,
    status TEXT NOT NULL,
    candidates_found INT DEFAULT 0,
    error TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tsr_run ON trend_source_runs(run_id);
