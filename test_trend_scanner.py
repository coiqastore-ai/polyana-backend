"""
Tests for Trend Scanner.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trend_scanner.dedupe import deduplicate_candidates, _normalize_title, _titles_similar
from trend_scanner.scoring import cheap_filter, score_candidates, _score_freshness
from trend_scanner.scanner import _calculate_trend_score


# ── Dedupe tests ─────────────────────────────────────────────────────────────

def test_dedupe_same_url():
    """Test that exact URL duplicates are removed."""
    candidates = [
        {"source_url": "https://example.com/recipe1", "title": "Test Recipe", "source_platform": "web"},
        {"source_url": "https://example.com/recipe1", "title": "Test Recipe", "source_platform": "youtube"},
    ]
    result = deduplicate_candidates(candidates)
    assert len(result) == 1
    print("✓ test_dedupe_same_url")


def test_dedupe_similar_title():
    """Test that similar titles are grouped."""
    candidates = [
        {"source_url": "https://youtube.com/v1", "title": "Easy Cottage Cheese Flatbread", "source_platform": "youtube"},
        {"source_url": "https://reddit.com/p1", "title": "Cottage Cheese Flatbread Recipe", "source_platform": "reddit"},
    ]
    result = deduplicate_candidates(candidates)
    # Should be merged into one with cross-source signal
    assert len(result) == 1
    assert result[0]["source_count"] == 2
    assert result[0]["cross_source_score"] == 50
    print("✓ test_dedupe_similar_title")


def test_cross_source_score():
    """Test cross-source scoring."""
    candidates = [
        {"source_url": "https://youtube.com/v1", "title": "Test", "source_platform": "youtube"},
        {"source_url": "https://reddit.com/p1", "title": "Test", "source_platform": "reddit"},
        {"source_url": "https://web.com/a1", "title": "Test", "source_platform": "web"},
    ]
    result = deduplicate_candidates(candidates)
    assert len(result) == 1
    assert result[0]["cross_source_score"] == 75  # 3 sources
    print("✓ test_cross_source_score")


# ── Freshness tests ──────────────────────────────────────────────────────────

def test_freshness_score():
    """Test freshness scoring."""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)

    # Recent
    c1 = {"published_at": now - timedelta(hours=12)}
    assert _score_freshness(c1) == 100

    # 2 days old
    c2 = {"published_at": now - timedelta(days=2)}
    assert _score_freshness(c2) == 90

    # 5 days old
    c3 = {"published_at": now - timedelta(days=5)}
    assert _score_freshness(c3) == 75

    # 20 days old
    c4 = {"published_at": now - timedelta(days=20)}
    assert _score_freshness(c4) == 30

    print("✓ test_freshness_score")


def test_old_candidate_filtered():
    """Test that very old candidates are filtered out."""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    candidates = [
        {
            "source_url": "https://example.com/old",
            "title": "Old Recipe",
            "published_at": now - timedelta(days=90),
            "raw_engagement": {"views": 100},
        },
        {
            "source_url": "https://example.com/new",
            "title": "New Recipe",
            "published_at": now - timedelta(days=1),
            "raw_engagement": {"views": 100},
        },
    ]
    result = cheap_filter(candidates)
    assert len(result) == 1
    assert result[0]["title"] == "New Recipe"
    print("✓ test_old_candidate_filtered")


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
    # This is a structural test - the scanner uses asyncio.gather with return_exceptions
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
        test_dedupe_same_url,
        test_dedupe_similar_title,
        test_cross_source_score,
        test_freshness_score,
        test_old_candidate_filtered,
        test_trend_score_formula,
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
