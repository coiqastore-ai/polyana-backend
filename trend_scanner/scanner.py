"""
Trend Scanner v0.1.1 — main entry point.

Collects trending recipes from multiple sources, classifies them,
scores them, and sends a digest to the admin.

Pipeline:
1. Collect 100-150 raw candidates
2. Deterministic filters + content classification
3. Dedupe
4. Score (freshness, engagement, velocity, cross-source)
5. Quality gate (min score + min confidence)
6. Top 20-30 → LLM Poliana Fit
7. Final top 0-10 admin candidates
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

import asyncpg

log = logging.getLogger("polyana.trend_scanner")

# Default config
DEFAULT_CONFIG = {
    "max_raw_candidates": 150,
    "max_llm_candidates": 30,
    "top_candidates": 10,
    "timezone": "Europe/Moscow",
    "days": ["mon", "wed", "fri"],
    "hour": 10,
}

TREND_WEIGHTS = {
    "freshness": 0.20,
    "engagement": 0.25,
    "cross_source": 0.20,
    "visual": 0.10,
    "simplicity": 0.10,
    "ru_availability": 0.10,
    "poliana_fit": 0.05,
}


def get_config() -> dict:
    """Load config from env with defaults."""
    cfg = dict(DEFAULT_CONFIG)
    cfg["max_raw_candidates"] = int(os.environ.get("TREND_MAX_RAW_CANDIDATES", cfg["max_raw_candidates"]))
    cfg["max_llm_candidates"] = int(os.environ.get("TREND_MAX_LLM_CANDIDATES", cfg["max_llm_candidates"]))
    cfg["top_candidates"] = int(os.environ.get("TREND_TOP_CANDIDATES", cfg["top_candidates"]))
    cfg["timezone"] = os.environ.get("TREND_SCANNER_TIMEZONE", cfg["timezone"])
    days_str = os.environ.get("TREND_SCANNER_DAYS", "mon,wed,fri")
    cfg["days"] = [d.strip().lower() for d in days_str.split(",")]
    cfg["hour"] = int(os.environ.get("TREND_SCANNER_HOUR", cfg["hour"]))
    return cfg


async def run_scan(*, db: asyncpg.Connection, dry_run: bool = False) -> dict:
    """
    Execute a full trend scan cycle.

    Returns run metrics dict.
    """
    from .sources import collect_from_all_sources
    from .dedupe import deduplicate_candidates
    from .scoring import score_candidates, cheap_filter, passes_quality_gate
    from .storage import save_candidates, get_top_candidates, create_run, finish_run
    from .telegram_digest import send_trend_digest

    config = get_config()
    run_id = str(uuid.uuid4())

    log.info("Starting trend scan run %s", run_id)

    if not dry_run:
        await create_run(db, run_id)

    # 1. Collect from sources
    raw_candidates = []
    source_results = {}

    try:
        raw_candidates, source_results = await collect_from_all_sources(
            db=db,
            run_id=run_id,
            max_candidates=config["max_raw_candidates"],
        )
    except Exception as e:
        log.error("Source collection failed: %s", e)

    log.info("Raw candidates collected: %d", len(raw_candidates))

    # 2. Cheap filtering + content classification
    filtered = cheap_filter(raw_candidates)
    log.info("After cheap filter: %d", len(filtered))

    # Count by content type
    content_type_counts = {}
    for c in filtered:
        ct = c.get("content_type", "unknown")
        content_type_counts[ct] = content_type_counts.get(ct, 0) + 1
    log.info("Content types: %s", content_type_counts)

    # 3. Deduplication
    unique = deduplicate_candidates(filtered)
    log.info("After dedup: %d", len(unique))

    # 4. Score candidates (without LLM)
    scored = score_candidates(unique)
    log.info("Scored candidates: %d", len(scored))

    # 5. Quality gate
    qualified = [c for c in scored if passes_quality_gate(c)]
    log.info("After quality gate: %d (min_score=%d, min_confidence=%d)",
             len(qualified), os.environ.get("TREND_MIN_SCORE", 60),
             os.environ.get("TREND_MIN_CONFIDENCE", 50))

    # 6. Select top N for LLM analysis
    top_for_llm = sorted(qualified, key=lambda c: c.get("trend_score", 0), reverse=True)[:config["max_llm_candidates"]]

    # 7. LLM analysis for top candidates
    llm_analyzed = []
    llm_calls = 0
    for candidate in top_for_llm:
        try:
            from .scoring import analyze_with_llm
            analysis = await analyze_with_llm(candidate)
            candidate.update(analysis)
            llm_analyzed.append(candidate)
            llm_calls += 1
        except Exception as e:
            log.warning("LLM analysis failed for %s: %s", candidate.get("title", "?"), e)
            llm_analyzed.append(candidate)

    # 8. Re-score with LLM data
    for c in llm_analyzed:
        c["trend_score"] = _calculate_trend_score(c)

    # 9. Select final top candidates
    final_top = sorted(llm_analyzed, key=lambda c: c.get("trend_score", 0), reverse=True)[:config["top_candidates"]]

    # 10. Save to DB
    if not dry_run:
        await save_candidates(db, scored)
        await finish_run(db, run_id, {
            "total_found": len(raw_candidates),
            "total_unique": len(unique),
            "shortlisted": len(final_top),
        })

    # 11. Send admin digest
    if not dry_run and final_top:
        await send_trend_digest(db, final_top, source_results, run_id)

    result = {
        "run_id": run_id,
        "total_found": len(raw_candidates),
        "after_filter": len(filtered),
        "content_types": content_type_counts,
        "after_dedupe": len(unique),
        "qualified": len(qualified),
        "llm_analyzed": len(llm_analyzed),
        "llm_calls": llm_calls,
        "top_candidates": len(final_top),
        "source_health": source_results,
        "dry_run": dry_run,
    }

    if dry_run:
        log.info("DRY RUN results:")
        log.info("  Raw: %d, Filtered: %d, Deduped: %d, Qualified: %d, LLM: %d, Final: %d",
                 len(raw_candidates), len(filtered), len(unique),
                 len(qualified), len(llm_analyzed), len(final_top))
        for i, c in enumerate(final_top, 1):
            log.info("  %d. %s (score: %.1f, confidence: %.1f, type: %s)",
                     i, c.get("canonical_dish_name") or c.get("title", "?"),
                     c.get("trend_score", 0), c.get("trend_confidence", 0),
                     c.get("content_type", "?"))

    return result


def _calculate_trend_score(candidate: dict) -> float:
    """Calculate weighted trend score."""
    scores = {
        "freshness": candidate.get("freshness_score", 0) or 0,
        "engagement": candidate.get("engagement_score", 0) or 0,
        "cross_source": candidate.get("cross_source_score", 0) or 0,
        "visual": candidate.get("visual_score", 0) or 0,
        "simplicity": candidate.get("simplicity_score", 0) or 0,
        "ru_availability": candidate.get("ru_availability_score", 0) or 0,
        "poliana_fit": candidate.get("poliana_fit_score", 0) or 0,
    }
    total = sum(scores[k] * TREND_WEIGHTS[k] for k in TREND_WEIGHTS)
    return round(total, 2)


async def acquire_scan_lock(db: asyncpg.Connection) -> bool:
    """
    Acquire PostgreSQL advisory lock for trend scan.
    Returns True if lock acquired, False if already locked.
    """
    try:
        # Use a fixed lock ID based on 'trend_scan'
        lock_id = 1234567890
        result = await db.fetchval("SELECT pg_try_advisory_lock($1)", lock_id)
        return result
    except Exception as e:
        log.error("Failed to acquire lock: %s", e)
        return False


async def release_scan_lock(db: asyncpg.Connection):
    """
    Release PostgreSQL advisory lock for trend scan.
    """
    try:
        lock_id = 1234567890
        await db.fetchval("SELECT pg_advisory_unlock($1)", lock_id)
    except Exception as e:
        log.error("Failed to release lock: %s", e)


async def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Polyana Trend Scanner")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to DB or Telegram")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        # Read from /etc/polyana/env
        try:
            with open("/etc/polyana/env") as f:
                for line in f:
                    if line.startswith("DATABASE_URL="):
                        db_url = line.strip().split("=", 1)[1]
                        break
        except FileNotFoundError:
            pass

    if not db_url:
        log.error("DATABASE_URL not set")
        sys.exit(1)

    db = await asyncpg.connect(db_url)
    try:
        # Try to acquire lock
        if not await acquire_scan_lock(db):
            log.error("Another scan is already running")
            sys.exit(1)

        try:
            result = await run_scan(db=db, dry_run=args.dry_run)
            print(json.dumps(result, indent=2, default=str))
        finally:
            await release_scan_lock(db)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
