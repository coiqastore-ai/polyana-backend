"""
Deduplication for Trend Scanner candidates.
"""

import logging
import re
from difflib import SequenceMatcher

log = logging.getLogger("polyana.trend_scanner.dedupe")


def deduplicate_candidates(candidates: list[dict]) -> list[dict]:
    """
    Remove duplicates based on:
    1. Exact source_url
    2. Normalized title similarity
    3. Cross-source signal aggregation
    """
    # Group by normalized title
    groups: dict[str, list[dict]] = {}
    seen_urls: set[str] = set()

    for c in candidates:
        url = c.get("source_url", "")

        # Skip exact URL duplicates
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Normalize title for grouping
        norm_title = _normalize_title(c.get("title", ""))

        if not norm_title:
            # Can't group, keep as-is
            groups[f"_ungrouped_{len(groups)}"] = [c]
            continue

        # Find matching group
        matched = False
        for key, group in groups.items():
            if key.startswith("_ungrouped_"):
                continue
            if _titles_similar(norm_title, key):
                group.append(c)
                matched = True
                break

        if not matched:
            groups[norm_title] = [c]

    # Merge groups and calculate cross-source scores
    result = []
    for key, group in groups.items():
        if len(group) == 1:
            c = group[0]
            c["cross_source_score"] = 20
            c["source_count"] = 1
            result.append(c)
        else:
            # Merge: keep the one with best engagement, add cross-source signal
            merged = _merge_group(group)
            result.append(merged)

    log.info("Dedup: %d → %d candidates (%d groups with multiple sources)",
             len(candidates), len(result), sum(1 for g in groups.values() if len(g) > 1))

    return result


def _normalize_title(title: str) -> str:
    """Normalize title for comparison."""
    if not title:
        return ""
    # Lowercase, remove punctuation, collapse spaces
    t = title.lower().strip()
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t)
    # Remove common prefixes
    for prefix in ["recipe:", "how to make", "easy", "best", "quick"]:
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
    return t


def _titles_similar(t1: str, t2: str, threshold: float = 0.75) -> bool:
    """Check if two normalized titles are similar enough to be the same recipe."""
    if not t1 or not t2:
        return False
    return SequenceMatcher(None, t1, t2).ratio() >= threshold


def _merge_group(group: list[dict]) -> dict:
    """Merge a group of duplicate candidates from different sources."""
    # Sort by engagement (best first)
    def engagement_score(c):
        eng = c.get("raw_engagement") or {}
        return (eng.get("views") or 0) + (eng.get("upvotes") or 0) * 100

    group.sort(key=engagement_score, reverse=True)

    # Keep the best one as base
    best = dict(group[0])

    # Collect all sources
    sources = list(set(c.get("source_platform", "") for c in group))
    source_urls = [c.get("source_url", "") for c in group]

    # Update cross-source score
    source_count = len(sources)
    if source_count >= 4:
        cross_score = 100
    elif source_count == 3:
        cross_score = 75
    elif source_count == 2:
        cross_score = 50
    else:
        cross_score = 20

    best["cross_source_score"] = cross_score
    best["source_count"] = source_count
    best["all_sources"] = sources
    best["all_source_urls"] = source_urls

    # Merge engagement signals
    all_views = sum((c.get("raw_engagement") or {}).get("views") or 0 for c in group)
    all_upvotes = sum((c.get("raw_engagement") or {}).get("upvotes") or 0 for c in group)
    if best.get("raw_engagement"):
        best["raw_engagement"]["total_views"] = all_views
        best["raw_engagement"]["total_upvotes"] = all_upvotes

    return best
