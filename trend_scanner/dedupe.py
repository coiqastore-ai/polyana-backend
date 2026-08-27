"""
Deduplication for Trend Scanner v0.1.1.

Improved canonical dish deduplication:
- Normalized title comparison
- Canonical dish name matching
- Fuzzy similarity with threshold
- Cross-source signal aggregation
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
    3. Canonical dish name matching
    4. Cross-source signal aggregation
    """
    # Group by normalized title or canonical dish
    groups: dict[str, list[dict]] = {}
    seen_urls: set[str] = set()

    for c in candidates:
        url = c.get("source_url", "")

        # Skip exact URL duplicates
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Get canonical dish name if available
        canonical = c.get("canonical_dish_name", "")
        norm_title = _normalize_title(c.get("title", ""))

        # Use canonical dish name as primary key if available
        if canonical:
            key = _normalize_title(canonical)
        else:
            key = norm_title

        if not key:
            # Can't group, keep as-is
            groups[f"_ungrouped_{len(groups)}"] = [c]
            continue

        # Find matching group
        matched = False
        for group_key, group in groups.items():
            if group_key.startswith("_ungrouped_"):
                continue
            if _dishes_match(key, group_key, canonical, group[0].get("canonical_dish_name", "")):
                group.append(c)
                matched = True
                break

        if not matched:
            groups[key] = [c]

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
    for prefix in ["recipe:", "how to make", "easy", "best", "quick", "viral", "trending"]:
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
    # Remove common suffixes
    for suffix in ["recipe", "recipes", "trend", "trending", "viral", "2025", "2026"]:
        if t.endswith(suffix):
            t = t[:-len(suffix)].strip()
    return t


def _dishes_match(key1: str, key2: str, canonical1: str, canonical2: str) -> bool:
    """
    Check if two dishes match.

    Uses multiple strategies:
    1. Exact match on normalized keys
    2. Canonical dish name match
    3. Fuzzy similarity with threshold
    """
    if not key1 or not key2:
        return False

    # Exact match
    if key1 == key2:
        return True

    # Canonical dish name match
    if canonical1 and canonical2:
        norm1 = _normalize_title(canonical1)
        norm2 = _normalize_title(canonical2)
        if norm1 and norm2 and norm1 == norm2:
            return True

    # Check if last words are different (e.g., "salad" vs "wrap")
    words1 = key1.split()
    words2 = key2.split()
    if len(words1) >= 2 and len(words2) >= 2:
        # If the last word is different, they're likely different dishes
        if words1[-1] != words2[-1]:
            # Check if the rest is similar
            rest1 = " ".join(words1[:-1])
            rest2 = " ".join(words2[:-1])
            rest_similarity = SequenceMatcher(None, rest1, rest2).ratio()
            # If the rest is very similar but last word differs, they're different
            if rest_similarity >= 0.8:
                return False

    # Fuzzy similarity
    similarity = SequenceMatcher(None, key1, key2).ratio()

    # Higher threshold for short titles (to avoid false positives)
    if len(key1) < 10 or len(key2) < 10:
        threshold = 0.90
    else:
        threshold = 0.80

    return similarity >= threshold


def _merge_group(group: list[dict]) -> dict:
    """Merge a group of duplicate candidates from different sources."""
    # Sort by engagement (best first)
    def engagement_score(c):
        eng = c.get("raw_engagement") or {}
        return (eng.get("views") or 0) + (eng.get("upvotes") or 0) * 100

    group.sort(key=engagement_score, reverse=True)

    # Keep the best one as base
    best = dict(group[0])

    # Collect all sources (unique platforms only)
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

    # Use canonical dish name if available
    for c in group:
        if c.get("canonical_dish_name"):
            best["canonical_dish_name"] = c["canonical_dish_name"]
            break

    return best
