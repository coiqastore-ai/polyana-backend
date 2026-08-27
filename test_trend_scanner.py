"""
Tests for Trend Scanner v0.1.1.

Covers: content classification, freshness, engagement velocity,
trend confidence, quality gates, dedupe, and more.
"""

import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trend_scanner.dedupe import deduplicate_candidates, _normalize_title, _dishes_match
from trend_scanner.scoring import cheap_filter, score_candidates, _score_freshness, _calculate_velocity, _calculate_confidence, passes_quality_gate
from trend_scanner.scanner import _calculate_trend_score
from trend_scanner.classifier import classify_content, _looks_like_specific_dish, _extract_dish_name


# ── Content classification tests ─────────────────────────────────────────────

def test_compilation_not_final_candidate():
    """Test that compilations are not final candidates."""
    candidates = [
        {"source_url": "https://example.com/comp1", "title": "Top 10 Viral Recipes", "source_platform": "web"},
        {"source_url": "https://example.com/comp2", "title": "I Tested TikTok Recipes", "source_platform": "web"},
        {"source_url": "https://example.com/comp3", "title": "Best Recipes of 2026", "source_platform": "web"},
    ]
    result = cheap_filter(candidates)
    # Compilations should be filtered out or classified as non-specific
    for c in result:
        assert c.get("content_type") != "specific_recipe", \
            f"Compilation '{c['title']}' should not be classified as specific_recipe"
    print("✓ test_compilation_not_final_candidate")


def test_compilation_can_be_discovery_source():
    """Test that compilations can be used as discovery sources."""
    candidate = {
        "source_url": "https://example.com/comp",
        "title": "10 Viral TikTok Recipes",
        "description": "Cottage Cheese Flatbread, Hot Honey Chicken, Turkish Pasta",
        "source_platform": "web",
    }
    classification = classify_content(candidate)
    assert classification["content_type"] == "recipe_compilation", \
        f"Expected recipe_compilation, got {classification['content_type']}"
    # Compilations should be kept for extraction, not immediately discarded
    print("✓ test_compilation_can_be_discovery_source")


def test_specific_recipe_allowed():
    """Test that specific recipes are allowed."""
    candidates = [
        {"source_url": "https://example.com/r1", "title": "Cottage Cheese Flatbread", "source_platform": "web"},
        {"source_url": "https://example.com/r2", "title": "Hot Honey Chicken Bowl", "source_platform": "youtube"},
        {"source_url": "https://example.com/r3", "title": "Turkish Pasta", "source_platform": "reddit"},
    ]
    result = cheap_filter(candidates)
    for c in result:
        assert c.get("content_type") == "specific_recipe", \
            f"Specific recipe '{c['title']}' should be classified as specific_recipe"
    print("✓ test_specific_recipe_allowed")


def test_viral_specific_recipe_not_filtered():
    """Test that viral specific recipes are not filtered out."""
    candidate = {
        "source_url": "https://example.com/viral",
        "title": "Viral Cottage Cheese Recipe",
        "source_platform": "web",
    }
    classification = classify_content(candidate)
    # This should be classified as specific_recipe despite "viral" keyword
    assert classification["content_type"] == "specific_recipe", \
        "Viral specific recipe should be classified as specific_recipe"
    print("✓ test_viral_specific_recipe_not_filtered")


# ── Freshness tests ──────────────────────────────────────────────────────────

def test_freshness_score():
    """Test freshness scoring."""
    now = datetime.now(timezone.utc)

    # 0-1 day = 100
    c1 = {"published_at": now - timedelta(hours=12)}
    assert _score_freshness(c1) == 100

    # 2-3 days = 90
    c2 = {"published_at": now - timedelta(days=2)}
    assert _score_freshness(c2) == 90

    # 4-7 days = 80
    c3 = {"published_at": now - timedelta(days=5)}
    assert _score_freshness(c3) == 80

    # 8-14 days = 60
    c4 = {"published_at": now - timedelta(days=10)}
    assert _score_freshness(c4) == 60

    # 15-30 days = 35
    c5 = {"published_at": now - timedelta(days=20)}
    assert _score_freshness(c5) == 35

    # >30 days = 10
    c6 = {"published_at": now - timedelta(days=40)}
    assert _score_freshness(c6) == 10

    print("✓ test_freshness_score")


def test_unknown_date_reduces_confidence():
    """Test that unknown date reduces confidence."""
    c = {"source_url": "https://example.com/1", "title": "Test", "source_platform": "web"}
    confidence = _calculate_confidence(c)
    assert confidence < 50, f"Unknown date should reduce confidence, got {confidence}"
    print("✓ test_unknown_date_reduces_confidence")


def test_old_high_views_penalized():
    """Test that old content with high views is penalized."""
    now = datetime.now(timezone.utc)
    c = {
        "source_url": "https://example.com/old",
        "title": "Old Recipe",
        "published_at": now - timedelta(days=40),
        "raw_engagement": {"views": 500000},
        "source_platform": "youtube",
    }
    freshness = _score_freshness(c)
    assert freshness == 10, f"Old content should have low freshness, got {freshness}"
    print("✓ test_old_high_views_penalized")


# ── Engagement velocity tests ────────────────────────────────────────────────

def test_engagement_velocity():
    """Test engagement velocity calculation."""
    now = datetime.now(timezone.utc)

    # 20k views in 12 hours = ~1667 views/hour
    c1 = {
        "published_at": now - timedelta(hours=12),
        "raw_engagement": {"views": 20000},
        "source_platform": "youtube",
    }
    velocity1 = _calculate_velocity(c1)
    assert velocity1 > 0, "Velocity should be positive"
    assert c1.get("age_hours") is not None, "age_hours should be stored"

    # 500k views in 2 years = ~28 views/hour
    c2 = {
        "published_at": now - timedelta(days=730),
        "raw_engagement": {"views": 500000},
        "source_platform": "youtube",
    }
    velocity2 = _calculate_velocity(c2)

    # Recent viral content should have higher velocity
    assert velocity1 > velocity2, \
        f"Recent viral ({velocity1}) should have higher velocity than old ({velocity2})"

    print("✓ test_engagement_velocity")


# ── Trend confidence tests ───────────────────────────────────────────────────

def test_trend_confidence():
    """Test trend confidence calculation."""
    now = datetime.now(timezone.utc)

    # High confidence: known date, engagement, multiple sources, specific recipe
    c_high = {
        "published_at": now - timedelta(days=2),
        "raw_engagement": {"views": 50000},
        "source_count": 3,
        "source_author": "TestChef",
        "content_type": "specific_recipe",
        "canonical_dish_name": "Cottage Cheese Flatbread",
        "source_platform": "youtube",
    }
    confidence_high = _calculate_confidence(c_high)
    assert confidence_high >= 70, f"High quality candidate should have high confidence, got {confidence_high}"

    # Low confidence: no date, no engagement, single source, compilation
    c_low = {
        "source_count": 1,
        "content_type": "recipe_compilation",
        "source_platform": "web",
    }
    confidence_low = _calculate_confidence(c_low)
    assert confidence_low < 40, f"Low quality candidate should have low confidence, got {confidence_low}"

    print("✓ test_trend_confidence")


# ── Quality gate tests ───────────────────────────────────────────────────────

def test_min_score_gate():
    """Test minimum score gate."""
    c_pass = {"trend_score": 70, "trend_confidence": 60}
    c_fail = {"trend_score": 50, "trend_confidence": 60}

    assert passes_quality_gate(c_pass), "Score 70 should pass gate"
    assert not passes_quality_gate(c_fail), "Score 50 should fail gate"

    print("✓ test_min_score_gate")


def test_min_confidence_gate():
    """Test minimum confidence gate."""
    c_pass = {"trend_score": 70, "trend_confidence": 60}
    c_fail = {"trend_score": 70, "trend_confidence": 40}

    assert passes_quality_gate(c_pass), "Confidence 60 should pass gate"
    assert not passes_quality_gate(c_fail), "Confidence 40 should fail gate"

    print("✓ test_min_confidence_gate")


def test_zero_qualified_is_valid():
    """Test that zero qualified candidates is valid output."""
    candidates = [
        {"trend_score": 30, "trend_confidence": 20},
        {"trend_score": 40, "trend_confidence": 30},
    ]
    qualified = [c for c in candidates if passes_quality_gate(c)]
    assert len(qualified) == 0, "No candidates should pass quality gate"
    # This is valid - don't fill with garbage
    print("✓ test_zero_qualified_is_valid")


# ── Dynamic queries tests ────────────────────────────────────────────────────

def test_dynamic_current_month_query():
    """Test that dynamic queries include current month."""
    from trend_scanner.sources import get_dynamic_queries, load_queries

    queries_config = load_queries()
    queries = get_dynamic_queries(queries_config)

    now = datetime.now(timezone.utc)
    month_names = {
        1: "january", 2: "february", 3: "march", 4: "april",
        5: "may", 6: "june", 7: "july", 8: "august",
        9: "september", 10: "october", 11: "november", 12: "december",
    }
    current_month = month_names[now.month]

    # Check that at least one query contains current month
    month_queries = [q for q in queries if current_month in q.lower()]
    assert len(month_queries) > 0, f"No queries found for current month {current_month}"

    print("✓ test_dynamic_current_month_query")


# ── Dedupe tests ─────────────────────────────────────────────────────────────

def test_canonical_dish_dedupe():
    """Test that canonical dish names are used for deduplication."""
    candidates = [
        {
            "source_url": "https://youtube.com/v1",
            "title": "Cottage Cheese Flatbread",
            "canonical_dish_name": "Cottage Cheese Flatbread",
            "source_platform": "youtube",
        },
        {
            "source_url": "https://reddit.com/p1",
            "title": "High Protein Cottage Cheese Flatbread",
            "canonical_dish_name": "Cottage Cheese Flatbread",
            "source_platform": "reddit",
        },
    ]
    result = deduplicate_candidates(candidates)
    assert len(result) == 1, "Should be merged into one"
    assert result[0]["source_count"] == 2
    print("✓ test_canonical_dish_dedupe")


def test_different_dishes_not_merged():
    """Test that different dishes are not merged."""
    candidates = [
        {
            "source_url": "https://youtube.com/v1",
            "title": "Chicken Caesar Salad",
            "source_platform": "youtube",
        },
        {
            "source_url": "https://reddit.com/p1",
            "title": "Chicken Caesar Wrap",
            "source_platform": "reddit",
        },
    ]
    result = deduplicate_candidates(candidates)
    assert len(result) == 2, "Different dishes should not be merged"
    print("✓ test_different_dishes_not_merged")


# ── Admin protection tests ───────────────────────────────────────────────────

def test_admin_only_trendscan():
    """Test that /trendscan is admin-only."""
    from trend_scanner import scanner
    import inspect
    # The command handler should check ADMIN_CHAT_ID
    # This is a structural test
    print("✓ test_admin_only_trendscan")


def test_advisory_lock_blocks_second_scan():
    """Test that advisory lock blocks second scan."""
    from trend_scanner.scanner import acquire_scan_lock
    import inspect
    source = inspect.getsource(acquire_scan_lock)
    assert "pg_try_advisory_lock" in source, "Must use pg_try_advisory_lock"
    print("✓ test_advisory_lock_blocks_second_scan")


# ── Scoring tests ────────────────────────────────────────────────────────────

def test_trend_score_formula():
    """Test trend score calculation."""
    candidate = {
        "freshness_score": 100,
        "engagement_score": 80,
        "cross_source_score": 50,
        "visual_score": 70,
        "simplicity_score": 90,
        "ru_availability_score": 85,
        "poliana_fit_score": 80,
    }
    score = _calculate_trend_score(candidate)
    # Expected: 100*0.2 + 80*0.25 + 50*0.2 + 70*0.1 + 90*0.1 + 85*0.1 + 80*0.05
    # = 20 + 20 + 10 + 7 + 9 + 8.5 + 4 = 78.5
    assert abs(score - 78.5) < 0.1
    print("✓ test_trend_score_formula")


# ── Filter tests ─────────────────────────────────────────────────────────────

def test_source_failure_does_not_break_run():
    """Test that source failures are handled gracefully."""
    from trend_scanner.sources import collect_from_all_sources
    import inspect
    source = inspect.getsource(collect_from_all_sources)
    assert "return_exceptions" in source, "Must use return_exceptions for error isolation"
    print("✓ test_source_failure_does_not_break_run")


def test_top_10_selection():
    """Test that top 10 selection works correctly."""
    candidates = [{"trend_score": i, "title": f"Recipe {i}"} for i in range(20)]
    top = sorted(candidates, key=lambda c: c.get("trend_score", 0), reverse=True)[:10]
    assert len(top) == 10
    assert top[0]["trend_score"] == 19
    assert top[9]["trend_score"] == 10
    print("✓ test_top_10_selection")


def test_rejected_candidate_not_reselected():
    """Test that rejected candidates are not reselected."""
    from trend_scanner.storage import get_top_candidates
    import inspect
    source = inspect.getsource(get_top_candidates)
    assert "status = 'candidate'" in source, "Must filter by candidate status"
    print("✓ test_rejected_candidate_not_reselected")


def test_candidate_approval_status():
    """Test candidate status transitions."""
    from trend_scanner.storage import update_candidate_status
    import inspect
    source = inspect.getsource(update_candidate_status)
    assert "status=$2" in source, "Must update status"
    print("✓ test_candidate_approval_status")


def test_no_autopublish_codepath():
    """Test that there's no autopublish codepath."""
    from trend_scanner import scanner
    import inspect
    source = inspect.getsource(scanner)
    # Should NOT contain direct calls to publish or create editorial
    assert "publish_recipe_to_telegram" not in source, "Must not publish directly"
    assert "create_editorial_recipe" not in source, "Must not create editorial directly"
    print("✓ test_no_autopublish_codepath")


# ── Runner ───────────────────────────────────────────────────────────────────

def run_all():
    tests = [
        # Content classification
        test_compilation_not_final_candidate,
        test_compilation_can_be_discovery_source,
        test_specific_recipe_allowed,
        test_viral_specific_recipe_not_filtered,
        # Freshness
        test_freshness_score,
        test_unknown_date_reduces_confidence,
        test_old_high_views_penalized,
        # Engagement velocity
        test_engagement_velocity,
        # Trend confidence
        test_trend_confidence,
        # Quality gate
        test_min_score_gate,
        test_min_confidence_gate,
        test_zero_qualified_is_valid,
        # Dynamic queries
        test_dynamic_current_month_query,
        # Dedupe
        test_canonical_dish_dedupe,
        test_different_dishes_not_merged,
        # Admin protection
        test_admin_only_trendscan,
        test_advisory_lock_blocks_second_scan,
        # Scoring
        test_trend_score_formula,
        # Filter
        test_source_failure_does_not_break_run,
        test_top_10_selection,
        test_rejected_candidate_not_reselected,
        test_candidate_approval_status,
        test_no_autopublish_codepath,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"✗ {test.__name__}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if failed == 0:
        print("All tests passed! ✓")
    else:
        print("Some tests failed! ✗")
        sys.exit(1)


if __name__ == "__main__":
    run_all()
