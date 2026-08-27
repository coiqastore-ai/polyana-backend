"""
Source adapters for Trend Scanner v0.1.1.

Collects candidates from Web, YouTube, RSS with dynamic date-aware queries.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import asyncpg

log = logging.getLogger("polyana.trend_scanner.sources")

# Trend queries config file path
QUERIES_FILE = os.path.join(os.path.dirname(__file__), "trend_queries.json")

# Source relevance weights
SOURCE_RELEVANCE = {
    "web": 1.0,
    "youtube": 1.0,
    "rss": 0.9,
    "reddit": 1.0,
    "instagram": 1.1,
    "tiktok": 1.2,
    "pinterest": 0.9,
    "bilibili": 0.6,
}


def load_queries() -> dict:
    """Load trend queries from config file."""
    try:
        with open(QUERIES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("trend_queries.json not found")
        return {}


def get_dynamic_queries(queries_config: dict) -> list[str]:
    """
    Generate dynamic queries based on current date and day of week.

    Returns list of queries to use for this scan.
    """
    now = datetime.now(timezone.utc)
    month_names = {
        1: "january", 2: "february", 3: "march", 4: "april",
        5: "may", 6: "june", 7: "july", 8: "august",
        9: "september", 10: "october", 11: "november", 12: "december",
    }
    day_names = {0: "monday", 1: "tuesday", 2: "wednesday", 3: "thursday",
                 4: "friday", 5: "saturday", 6: "sunday"}

    current_month = month_names[now.month]
    current_day = day_names[now.weekday()]

    all_queries = []

    # 1. Core queries (every scan)
    core = queries_config.get("core", {})
    all_queries.extend(core.get("en", []))
    all_queries.extend(core.get("ru", []))

    # 2. Day-of-week queries
    day_queries = queries_config.get(current_day, {})
    all_queries.extend(day_queries.get("en", []))
    all_queries.extend(day_queries.get("ru", []))

    # 3. Seasonal/monthly queries
    seasonal = queries_config.get("seasonal", {})
    month_queries = seasonal.get(current_month, {})
    all_queries.extend(month_queries.get("en", []))
    all_queries.extend(month_queries.get("ru", []))

    # 4. Evergreen queries (supplementary)
    evergreen = queries_config.get("evergreen", {})
    all_queries.extend(evergreen.get("en", []))
    all_queries.extend(evergreen.get("ru", []))

    # Remove duplicates while preserving order
    seen = set()
    unique_queries = []
    for q in all_queries:
        q_lower = q.lower().strip()
        if q_lower not in seen:
            seen.add(q_lower)
            unique_queries.append(q)

    return unique_queries


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
    queries_config = load_queries()
    queries = get_dynamic_queries(queries_config)

    log.info("Dynamic queries generated: %d", len(queries))

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


async def _collect_web(queries: list[str]) -> list[dict]:
    """
    Collect from web search via multiple backends.

    Tries: Jina search, then DuckDuckGo fallback.
    """
    candidates = []
    seen_urls = set()

    # Use first 8 queries for web search
    for query in queries[:8]:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                # Try Jina search first
                try:
                    r = await client.get(
                        f"https://s.jina.ai/{query}",
                        headers={"Accept": "application/json"},
                    )
                    if r.status_code == 200:
                        data = r.json()
                        for item in data.get("data", [])[:5]:
                            url = item.get("url", "")
                            if url in seen_urls:
                                continue
                            seen_urls.add(url)

                            # Extract published date if available
                            published_at = None
                            if item.get("publishedDate"):
                                try:
                                    published_at = datetime.fromisoformat(
                                        item["publishedDate"].replace("Z", "+00:00")
                                    )
                                except Exception:
                                    pass

                            candidates.append({
                                "source_platform": "web",
                                "source_url": url,
                                "title": item.get("title", ""),
                                "description": item.get("description", ""),
                                "published_at": published_at,
                                "raw_metadata": {"query": query, "search_backend": "jina"},
                            })
                            continue  # Skip DuckDuckGo if Jina works
                except Exception:
                    pass

                # Fallback: DuckDuckGo instant answer
                try:
                    ddg_url = f"https://api.duckduckgo.com/?q={query}+recipe&format=json&no_html=1"
                    r = await client.get(ddg_url, timeout=15)
                    if r.status_code == 200:
                        data = r.json()
                        # DuckDuckGo returns related topics
                        for topic in data.get("RelatedTopics", [])[:3]:
                            if isinstance(topic, dict) and topic.get("FirstURL"):
                                url = topic["FirstURL"]
                                if url in seen_urls:
                                    continue
                                seen_urls.add(url)
                                candidates.append({
                                    "source_platform": "web",
                                    "source_url": url,
                                    "title": topic.get("Text", "")[:100],
                                    "description": topic.get("Text", ""),
                                    "raw_metadata": {"query": query, "search_backend": "duckduckgo"},
                                })
                except Exception:
                    pass

        except Exception as e:
            log.warning("Web search failed for '%s': %s", query, e)

        # Rate limit
        await asyncio.sleep(1)

    return candidates


async def _collect_youtube(queries: list[str]) -> list[dict]:
    """
    Collect from YouTube via yt-dlp search.

    Prefers: recent upload, specific dish, strong velocity.
    Lowers: compilation, reaction, testing recipes, hour-long video.
    """
    candidates = []
    venv_ytdlp = "/opt/trend-scanner/venv/bin/yt-dlp"

    # Check if yt-dlp exists
    if not os.path.exists(venv_ytdlp):
        # Try system yt-dlp
        venv_ytdlp = "yt-dlp"

    # Use first 6 queries for YouTube
    for query in queries[:6]:
        try:
            cmd = [
                venv_ytdlp,
                "--flat-playlist",
                "--print", "%(id)s\t%(title)s\t%(view_count)s\t%(upload_date)s\t%(channel)s\t%(duration)s",
                f"ytsearch5:{query}",
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

                    # Skip very long videos (>20 min) - likely compilations
                    if duration and duration > 1200:
                        continue

                    # Parse upload date
                    published_at = None
                    if upload_date and upload_date != "NA" and len(upload_date) == 8:
                        try:
                            published_at = datetime.strptime(upload_date, "%Y%m%d").replace(
                                tzinfo=timezone.utc
                            )
                        except Exception:
                            pass

                    candidates.append({
                        "source_platform": "youtube",
                        "source_url": f"https://www.youtube.com/watch?v={video_id}",
                        "title": title,
                        "source_author": channel,
                        "published_at": published_at,
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
    """
    Collect from RSS feeds.

    Includes Reddit food subreddits and other food blogs.
    """
    candidates = []

    feeds = [
        # Reddit
        ("https://www.reddit.com/r/recipes/.rss", "reddit"),
        ("https://www.reddit.com/r/Cooking/.rss", "reddit"),
        ("https://www.reddit.com/r/MealPrepSunday/.rss", "reddit"),
        ("https://www.reddit.com/r/recipes/hot/.rss", "reddit"),
        # Food blogs (if available)
        # ("https://www.simplyrecipes.com/index.xml", "web"),
        # ("https://www.budgetbytes.com/feed/", "web"),
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

                # Parse published date
                published_at = None
                if hasattr(entry, "published"):
                    published_at = _parse_date(entry.published)

                candidates.append({
                    "source_platform": platform,
                    "source_url": entry.get("link", ""),
                    "title": entry.get("title", ""),
                    "description": entry.get("summary", "")[:500],
                    "published_at": published_at,
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
