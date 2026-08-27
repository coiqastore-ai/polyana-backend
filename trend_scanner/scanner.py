"""
Trend Scanner — main entry point.

Collects trending recipes from multiple sources, scores them,
and sends a digest to the admin.
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
    from .scoring import score_candidates, cheap_filter
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

    # 2. Cheap filtering
    filtered = cheap_filter(raw_candidates)
    log.info("After cheap filter: %d", len(filtered))

    # 3. Deduplication
    unique = deduplicate_candidates(filtered)
    log.info("After dedup: %d", len(unique))

    # 4. Score candidates (without LLM)
    scored = score_candidates(unique)
    log.info("Scored candidates: %d", len(scored))

    # 5. Select top N for LLM analysis
    top_for_llm = sorted(scored, key=lambda c: c.get("trend_score", 0), reverse=True)[:config["max_llm_candidates"]]

    # 6. LLM analysis for top candidates
    llm_analyzed = []
    for candidate in top_for_llm:
        try:
            from .scoring import analyze_with_llm
            analysis = await analyze_with_llm(candidate)
            candidate.update(analysis)
            llm_analyzed.append(candidate)
        except Exception as e:
            log.warning("LLM analysis failed for %s: %s", candidate.get("title", "?"), e)
            llm_analyzed.append(candidate)

    # 7. Re-score with LLM data
    for c in llm_analyzed:
        c["trend_score"] = _calculate_trend_score(c)

    # 8. Select final top candidates
    final_top = sorted(llm_analyzed, key=lambda c: c.get("trend_score", 0), reverse=True)[:config["top_candidates"]]

    # 9. Save to DB
    if not dry_run:
        await save_candidates(db, scored)
        await finish_run(db, run_id, {
            "total_found": len(raw_candidates),
            "total_unique": len(unique),
            "shortlisted": len(final_top),
        })

    # 10. Send admin digest
    if not dry_run and final_top:
        await send_trend_digest(db, final_top, source_results, run_id)

    result = {
        "run_id": run_id,
        "total_found": len(raw_candidates),
        "after_filter": len(filtered),
        "after_dedupe": len(unique),
        "llm_analyzed": len(llm_analyzed),
        "top_candidates": len(final_top),
        "source_health": source_results,
        "dry_run": dry_run,
    }

    if dry_run:
        log.info("DRY RUN results:")
        for i, c in enumerate(final_top, 1):
            log.info("  %d. %s (score: %.1f)", i, c.get("title", "?"), c.get("trend_score", 0))

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
        result = await run_scan(db=db, dry_run=args.dry_run)
        print(json.dumps(result, indent=2, default=str))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
