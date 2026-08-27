"""
Storage operations for Trend Scanner.
"""

import json
import logging
from datetime import datetime, timezone

import asyncpg

log = logging.getLogger("polyana.trend_scanner.storage")


async def create_run(db: asyncpg.Connection, run_id: str):
    """Create a new scan run record."""
    await db.execute(
        """
        INSERT INTO trend_scan_runs (id, started_at, status)
        VALUES ($1, NOW(), 'running')
        """,
        run_id,
    )


async def finish_run(db: asyncpg.Connection, run_id: str, metrics: dict):
    """Update run record with final metrics."""
    await db.execute(
        """
        UPDATE trend_scan_runs
        SET finished_at = NOW(),
            status = 'completed',
            total_found = $2,
            total_unique = $3,
            shortlisted = $4
        WHERE id = $1
        """,
        run_id,
        metrics.get("total_found", 0),
        metrics.get("total_unique", 0),
        metrics.get("shortlisted", 0),
    )


async def save_candidates(db: asyncpg.Connection, candidates: list[dict]):
    """Save candidates to DB (upsert by source_url)."""
    for c in candidates:
        try:
            await db.execute(
                """
                INSERT INTO trend_candidates (
                    source_platform, source_url, source_author,
                    title, description, published_at,
                    raw_engagement, raw_metadata,
                    normalized_title,
                    freshness_score, engagement_score, visual_score,
                    simplicity_score, ru_availability_score, poliana_fit_score,
                    trend_score, status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, 'candidate')
                ON CONFLICT (source_url) DO UPDATE SET
                    title = COALESCE(EXCLUDED.title, trend_candidates.title),
                    trend_score = GREATEST(EXCLUDED.trend_score, trend_candidates.trend_score),
                    raw_engagement = EXCLUDED.raw_engagement
                """,
                c.get("source_platform"),
                c.get("source_url"),
                c.get("source_author"),
                c.get("title"),
                c.get("description"),
                c.get("published_at"),
                json.dumps(c.get("raw_engagement") or {}),
                json.dumps(c.get("raw_metadata") or {}),
                c.get("normalized_title"),
                c.get("freshness_score"),
                c.get("engagement_score"),
                c.get("visual_score"),
                c.get("simplicity_score"),
                c.get("ru_availability_score"),
                c.get("poliana_fit_score"),
                c.get("trend_score"),
            )
        except Exception as e:
            log.warning("Failed to save candidate %s: %s", c.get("source_url", "?"), e)


async def get_top_candidates(db: asyncpg.Connection, limit: int = 10) -> list[dict]:
    """Get top candidates by trend score."""
    rows = await db.fetch(
        """
        SELECT * FROM trend_candidates
        WHERE status = 'candidate'
        ORDER BY trend_score DESC NULLS LAST
        LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


async def update_candidate_status(db: asyncpg.Connection, candidate_id: int, status: str):
    """Update candidate status."""
    await db.execute(
        "UPDATE trend_candidates SET status=$2 WHERE id=$1",
        candidate_id, status,
    )
