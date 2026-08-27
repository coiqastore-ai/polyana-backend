"""
Source adapters for Trend Scanner.

Collects candidates from Web, YouTube, RSS, and optionally Reddit/Instagram/X.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone

import asyncpg

log = logging.getLogger("polyana.trend_scanner.sources")

# Trend queries config file path
QUERIES_FILE = os.path.join(os.path.dirname(__file__), "trend_queries.json")

# Default queries if config file not found
DEFAULT_QUERIES = {
    "ru": [
        "вирусный рецепт",
        "популярный рецепт",
        "быстрый ужин",
        "простой десерт",
        "белковый рецепт",
        "рецепт из 5 ингредиентов",
        "ужин за 20 минут",
        "рецепт с творогом",
        "рецепт с курицей",
    ],
    "en": [
        "viral recipe",
        "trending recipe",
        "easy dinner",
        "quick dinner",
        "high protein recipe",
        "5 ingredient recipe",
        "viral dessert",
        "cottage cheese recipe",
        "chicken recipe trend",
        "food trend 2026",
    ],
}


def load_queries() -> dict:
    """Load trend queries from config file."""
    try:
        with open(QUERIES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("trend_queries.json not found, using defaults")
        return DEFAULT_QUERIES


async def collect_from_all_sources(
    *,
    db: asyncpg.Connection,
    run_id: str,
    max_candidates: int = 150,
) -> tuple[list[dict], dict]:
    """
    Collect candidates from all enabled sources.
    Returns (candidates, source_health).
    """
    queries = load_queries()
    all_candidates = []
    source_health = {}

    # Run sources in parallel
    tasks = [
        ("web", _collect_web(queries)),
        ("youtube", _collect_youtube(queries)),
        ("rss", _collect_rss()),
    ]

    results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)

    for (source_name, _), result in zip(tasks, results):
        if isinstance(result, Exception):
            log.error("Source %s failed: %s", source_name, result)
            source_health[source_name] = {"status": "error", "error": str(result), "count": 0}
            await _log_source_run(db, run_id, source_name, "error", 0, str(result))
        else:
            all_candidates.extend(result)
            source_health[source_name] = {"status": "ok", "count": len(result)}
            await _log_source_run(db, run_id, source_name, "ok", len(result))

    # Limit total candidates
    if len(all_candidates) > max_candidates:
        all_candidates = all_candidates[:max_candidates]

    return all_candidates, source_health


async def _collect_web(queries: dict) -> list[dict]:
    """Collect from web search via Jina Reader + direct search."""
    candidates = []

    for query in queries.get("en", [])[:5]:
        try:
            # Use Jina search
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"https://s.jina.ai/{query}",
                    headers={"Accept": "application/json"},
                )
                if r.status_code == 200:
                    data = r.json()
                    for item in data.get("data", [])[:5]:
                        candidates.append({
                            "source_platform": "web",
                            "source_url": item.get("url", ""),
                            "title": item.get("title", ""),
                            "description": item.get("description", ""),
                            "raw_metadata": {"query": query},
                        })
        except Exception as e:
            log.warning("Web search failed for '%s': %s", query, e)

        # Rate limit
        await asyncio.sleep(1)

    return candidates


async def _collect_youtube(queries: dict) -> list[dict]:
    """Collect from YouTube via yt-dlp search."""
    candidates = []
    venv_ytdlp = "/opt/trend-scanner/venv/bin/yt-dlp"

    for query in queries.get("en", [])[:5]:
        try:
            cmd = [
                venv_ytdlp,
                "--flat-playlist",
                "--print", "%(id)s\t%(title)s\t%(view_count)s\t%(upload_date)s\t%(channel)s\t%(duration)s",
                f"ytsearch5:{query} recipe",
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            for line in stdout.decode().strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) >= 2:
                    video_id = parts[0]
                    title = parts[1]
                    views = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
                    upload_date = parts[3] if len(parts) > 3 else None
                    channel = parts[4] if len(parts) > 4 else None
                    duration = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else None

                    candidates.append({
                        "source_platform": "youtube",
                        "source_url": f"https://www.youtube.com/watch?v={video_id}",
                        "title": title,
                        "source_author": channel,
                        "raw_engagement": {"views": views},
                        "raw_metadata": {
                            "upload_date": upload_date,
                            "duration": duration,
                            "query": query,
                        },
                    })
        except Exception as e:
            log.warning("YouTube search failed for '%s': %s", query, e)

        await asyncio.sleep(0.5)

    return candidates


async def _collect_rss() -> list[dict]:
    """Collect from RSS feeds."""
    candidates = []

    feeds = [
        ("https://www.reddit.com/r/recipes/.rss", "reddit"),
        ("https://www.reddit.com/r/Cooking/.rss", "reddit"),
        ("https://www.reddit.com/r/MealPrepSunday/.rss", "reddit"),
    ]

    for feed_url, platform in feeds:
        try:
            import feedparser
            # Run in executor to avoid blocking
            loop = asyncio.get_event_loop()
            d = await loop.run_in_executor(None, feedparser.parse, feed_url)

            for entry in d.entries[:10]:
                # Extract engagement from Reddit
                ups = None
                if hasattr(entry, "score"):
                    ups = entry.score

                candidates.append({
                    "source_platform": platform,
                    "source_url": entry.get("link", ""),
                    "title": entry.get("title", ""),
                    "description": entry.get("summary", "")[:500],
                    "published_at": _parse_date(entry.get("published")),
                    "source_author": entry.get("author", ""),
                    "raw_engagement": {"upvotes": ups},
                    "raw_metadata": {"feed": feed_url},
                })
        except Exception as e:
            log.warning("RSS feed %s failed: %s", feed_url, e)

    return candidates


def _parse_date(date_str) -> datetime | None:
    """Parse date string to datetime."""
    if not date_str:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_str)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(date_str)
    except Exception:
        return None


async def _log_source_run(
    db: asyncpg.Connection,
    run_id: str,
    source_platform: str,
    status: str,
    candidates_found: int,
    error: str | None = None,
):
    """Log source run to DB."""
    try:
        await db.execute(
            """
            INSERT INTO trend_source_runs (run_id, source_platform, status, candidates_found, error, started_at, finished_at)
            VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
            """,
            run_id, source_platform, status, candidates_found, error,
        )
    except Exception:
        pass
