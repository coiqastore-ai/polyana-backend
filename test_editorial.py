"""
Tests for editorial content system.

Tests cover:
- Editorial recipe creation
- Public API access control
- Clone/save functionality
- Publish workflow
- Analytics events
"""

import asyncio
import json
import os
import sys

import asyncpg

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(__file__))

import editorial_service


# ── Test helpers ─────────────────────────────────────────────────────────────

TEST_DB_URL = os.environ.get("DATABASE_URL", "postgresql://polyana_app:26ecb84075e9361da1ba8d9c41fe25f7@localhost:5432/polyana2")
EDITORIAL_USER_ID = 999999999  # System user for tests
TEST_USER_A = 111111111
TEST_USER_B = 222222222


async def get_db():
    return await asyncpg.connect(TEST_DB_URL)


async def cleanup_test_data(db):
    """Remove test data (idempotent)."""
    await db.execute("DELETE FROM recipe_steps WHERE recipe_id IN (SELECT id FROM recipes WHERE user_id IN ($1,$2,$3))",
                     EDITORIAL_USER_ID, TEST_USER_A, TEST_USER_B)
    await db.execute("DELETE FROM ingredients WHERE recipe_id IN (SELECT id FROM recipes WHERE user_id IN ($1,$2,$3))",
                     EDITORIAL_USER_ID, TEST_USER_A, TEST_USER_B)
    await db.execute("DELETE FROM recipes WHERE user_id IN ($1,$2,$3) OR source_editorial_recipe_id IN (SELECT id FROM recipes WHERE user_id=$1)",
                     EDITORIAL_USER_ID, TEST_USER_A, TEST_USER_B)
    await db.execute("DELETE FROM analytics_events WHERE user_id IN ($1,$2,$3)",
                     EDITORIAL_USER_ID, TEST_USER_A, TEST_USER_B)


# ── Tests ────────────────────────────────────────────────────────────────────

async def test_create_editorial_recipe():
    """Test creating an editorial recipe."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db,
            EDITORIAL_USER_ID,
            name="Куриные митболы с рикоттой",
            description="Сочные куриные фрикадельки",
            emoji="🍗",
            slug="chicken-ricotta-meatballs",
            servings=4,
            cook_time_minutes=30,
            category="Ужин",
            tags=["ужин", "курица"],
            ingredients=[
                {"name": "Куриный фарш", "qty": 500, "unit": "г"},
                {"name": "Рикотта", "qty": 100, "unit": "г"},
            ],
            steps=[
                {"step_number": 1, "text": "Смешать фарш с рикоттой"},
                {"step_number": 2, "text": "Сформировать митболы"},
            ],
        )

        assert recipe["id"] > 0
        assert recipe["name"] == "Куриные митболы с рикоттой"
        assert recipe["is_editorial"] is True
        assert recipe["visibility"] == "private"
        assert recipe["editorial_status"] == "draft"
        assert recipe["content_slug"] == "chicken-ricotta-meatballs"
        assert recipe["user_id"] == EDITORIAL_USER_ID
        print("✓ test_create_editorial_recipe")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_public_access_only_published():
    """Test that public endpoint only returns published recipes."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        # Create draft recipe
        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Test Draft", slug="test-draft",
            ingredients=[{"name": "Test", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Test step"}],
        )

        # Should NOT be accessible publicly
        result = await editorial_service.get_editorial_recipe_by_slug(db, "test-draft")
        assert result is None, "Draft should not be publicly accessible"

        # Approve and publish
        await editorial_service.approve_editorial_recipe(db, recipe["id"])
        await editorial_service.publish_editorial_recipe(db, recipe["id"])

        # Now should be accessible
        result = await editorial_service.get_editorial_recipe_by_slug(db, "test-draft")
        assert result is not None, "Published recipe should be accessible"
        assert result["id"] == recipe["id"]

        print("✓ test_public_access_only_published")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_private_recipe_not_public():
    """Test that private recipes are not accessible via public endpoint."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        # Create a regular (non-editorial) recipe
        rec = await db.fetchrow(
            """
            INSERT INTO recipes (user_id, name, is_editorial, visibility, content_slug)
            VALUES ($1, 'Private Recipe', FALSE, 'private', 'private-slug')
            RETURNING id
            """,
            TEST_USER_A,
        )

        # Should NOT be accessible publicly
        result = await editorial_service.get_editorial_recipe_by_slug(db, "private-slug")
        assert result is None, "Private recipe should not be publicly accessible"

        print("✓ test_private_recipe_not_public")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_clone_creates_personal_copy():
    """Test that saving creates a personal copy."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        # Create and publish editorial recipe
        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Test Clone", slug="test-clone",
            ingredients=[{"name": "Ingredient A", "qty": 100, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step 1"}],
        )
        await editorial_service.approve_editorial_recipe(db, recipe["id"])
        await editorial_service.publish_editorial_recipe(db, recipe["id"])

        # Clone to user
        result = await editorial_service.clone_editorial_recipe_to_user(db, recipe["id"], TEST_USER_A)

        assert result["already_saved"] is False
        assert result["recipe_id"] > 0
        assert result["recipe_id"] != recipe["id"]

        # Verify the clone
        clone = await db.fetchrow("SELECT * FROM recipes WHERE id=$1", result["recipe_id"])
        assert clone["user_id"] == TEST_USER_A
        assert clone["is_editorial"] is False
        assert clone["visibility"] == "private"
        assert clone["source_editorial_recipe_id"] == recipe["id"]

        # Verify ingredients were copied
        ings = await db.fetch("SELECT * FROM ingredients WHERE recipe_id=$1", result["recipe_id"])
        assert len(ings) == 1
        assert ings[0]["name"] == "Ingredient A"

        # Verify steps were copied
        steps = await db.fetch("SELECT * FROM recipe_steps WHERE recipe_id=$1", result["recipe_id"])
        assert len(steps) == 1
        assert steps[0]["text"] == "Step 1"

        print("✓ test_clone_creates_personal_copy")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_duplicate_save_returns_existing():
    """Test that saving the same recipe twice returns existing."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        # Create and publish
        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Test Dup", slug="test-dup",
            ingredients=[{"name": "X", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )
        await editorial_service.approve_editorial_recipe(db, recipe["id"])
        await editorial_service.publish_editorial_recipe(db, recipe["id"])

        # First save
        r1 = await editorial_service.clone_editorial_recipe_to_user(db, recipe["id"], TEST_USER_A)
        assert r1["already_saved"] is False

        # Second save — should return existing
        r2 = await editorial_service.clone_editorial_recipe_to_user(db, recipe["id"], TEST_USER_A)
        assert r2["already_saved"] is True
        assert r2["recipe_id"] == r1["recipe_id"]

        print("✓ test_duplicate_save_returns_existing")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_independent_copies_per_user():
    """Test that different users get independent copies."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        # Create and publish
        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Test Multi", slug="test-multi",
            ingredients=[{"name": "Y", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )
        await editorial_service.approve_editorial_recipe(db, recipe["id"])
        await editorial_service.publish_editorial_recipe(db, recipe["id"])

        # Save for user A
        r1 = await editorial_service.clone_editorial_recipe_to_user(db, recipe["id"], TEST_USER_A)
        # Save for user B
        r2 = await editorial_service.clone_editorial_recipe_to_user(db, recipe["id"], TEST_USER_B)

        assert r1["recipe_id"] != r2["recipe_id"]

        # Both are independent
        c1 = await db.fetchrow("SELECT * FROM recipes WHERE id=$1", r1["recipe_id"])
        c2 = await db.fetchrow("SELECT * FROM recipes WHERE id=$1", r2["recipe_id"])
        assert c1["user_id"] == TEST_USER_A
        assert c2["user_id"] == TEST_USER_B

        print("✓ test_independent_copies_per_user")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_editorial_not_modified_on_clone():
    """Test that cloning doesn't modify the original editorial recipe."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Test Original", slug="test-original",
            ingredients=[{"name": "Z", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )
        await editorial_service.approve_editorial_recipe(db, recipe["id"])
        await editorial_service.publish_editorial_recipe(db, recipe["id"])

        # Clone
        await editorial_service.clone_editorial_recipe_to_user(db, recipe["id"], TEST_USER_A)

        # Verify original unchanged
        orig = await db.fetchrow("SELECT * FROM recipes WHERE id=$1", recipe["id"])
        assert orig["name"] == "Test Original"
        assert orig["is_editorial"] is True
        assert orig["user_id"] == EDITORIAL_USER_ID

        print("✓ test_editorial_not_modified_on_clone")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_cannot_publish_draft():
    """Test that publishing a draft fails."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Test Draft Publish", slug="test-draft-publish",
            ingredients=[{"name": "A", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )

        # Should fail — status is draft, not approved
        try:
            await editorial_service.publish_editorial_recipe(db, recipe["id"])
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "Cannot publish" in str(e) or "draft" in str(e)

        print("✓ test_cannot_publish_draft")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_approve_and_publish_workflow():
    """Test the full approve → publish workflow."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Test Workflow", slug="test-workflow",
            ingredients=[{"name": "B", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )

        # Approve
        r1 = await editorial_service.approve_editorial_recipe(db, recipe["id"])
        assert r1["status"] == "approved"

        # Publish
        r2 = await editorial_service.publish_editorial_recipe(db, recipe["id"])
        assert r2["status"] == "published"

        # Verify in DB
        rec = await db.fetchrow("SELECT * FROM recipes WHERE id=$1", recipe["id"])
        assert rec["editorial_status"] == "published"
        assert rec["visibility"] == "public"
        assert rec["published_at"] is not None

        print("✓ test_approve_and_publish_workflow")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_slug_uniqueness():
    """Test that duplicate slugs get a suffix."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        r1 = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Test Slug", slug="test-slug",
            ingredients=[{"name": "C", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )

        r2 = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Test Slug 2", slug="test-slug",
            ingredients=[{"name": "D", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )

        assert r1["content_slug"] == "test-slug"
        assert r2["content_slug"] != "test-slug"  # Should have a suffix
        assert r2["content_slug"].startswith("test-slug-")

        print("✓ test_slug_uniqueness")
    finally:
        await cleanup_test_data(db)
        await db.close()


# ── Runner ───────────────────────────────────────────────────────────────────

async def run_all():
    tests = [
        test_create_editorial_recipe,
        test_public_access_only_published,
        test_private_recipe_not_public,
        test_clone_creates_personal_copy,
        test_duplicate_save_returns_existing,
        test_independent_copies_per_user,
        test_editorial_not_modified_on_clone,
        test_cannot_publish_draft,
        test_approve_and_publish_workflow,
        test_slug_uniqueness,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            await test()
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
    asyncio.run(run_all())
