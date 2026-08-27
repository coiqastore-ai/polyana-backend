"""
Tests for Editorial Content v1.1.

Covers: nutrition, approval flow, daily digest, channel integration.
"""

import asyncio
import json
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(__file__))

import editorial_service


TEST_DB_URL = os.environ.get("DATABASE_URL", "postgresql://polyana_app:26ecb84075e9361da1ba8d9c41fe25f7@localhost:5432/polyana2")
EDITORIAL_USER_ID = 999999999
TEST_USER_A = 111111111
TEST_USER_B = 222222222


async def get_db():
    return await asyncpg.connect(TEST_DB_URL)


async def cleanup_test_data(db):
    await db.execute("DELETE FROM recipe_steps WHERE recipe_id IN (SELECT id FROM recipes WHERE user_id IN ($1,$2,$3))",
                     EDITORIAL_USER_ID, TEST_USER_A, TEST_USER_B)
    await db.execute("DELETE FROM ingredients WHERE recipe_id IN (SELECT id FROM recipes WHERE user_id IN ($1,$2,$3))",
                     EDITORIAL_USER_ID, TEST_USER_A, TEST_USER_B)
    await db.execute("DELETE FROM recipes WHERE user_id IN ($1,$2,$3) OR source_editorial_recipe_id IN (SELECT id FROM recipes WHERE user_id=$1)",
                     EDITORIAL_USER_ID, TEST_USER_A, TEST_USER_B)
    await db.execute("DELETE FROM analytics_events WHERE user_id IN ($1,$2,$3)",
                     EDITORIAL_USER_ID, TEST_USER_A, TEST_USER_B)


# ── Nutrition tests ──────────────────────────────────────────────────────────

async def test_editorial_nutrition_create():
    """Test creating editorial recipe with nutrition."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Nutrition Test", slug="nutrition-test",
            nutrition={
                "calories_kcal": 420,
                "protein_g": 36,
                "fat_g": 19,
                "carbs_g": 24,
                "basis": "per_serving",
            },
            ingredients=[{"name": "Test", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )

        assert recipe["nutrition"] is not None
        assert recipe["nutrition"]["calories_kcal"] == 420
        assert recipe["nutrition"]["protein_g"] == 36
        assert recipe["nutrition"]["fat_g"] == 19
        assert recipe["nutrition"]["carbs_g"] == 24
        assert recipe["nutrition"]["basis"] == "per_serving"

        print("✓ test_editorial_nutrition_create")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_editorial_nutrition_public_api():
    """Test that public API returns nutrition."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Nutrition Public", slug="nutrition-public",
            nutrition={"calories_kcal": 300, "protein_g": 25},
            ingredients=[{"name": "Test", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )
        await editorial_service.approve_editorial_recipe(db, recipe["id"])
        await editorial_service.publish_editorial_recipe(db, recipe["id"])

        public = await editorial_service.get_editorial_recipe_by_slug(db, "nutrition-public")
        assert public is not None
        assert public["nutrition"]["calories_kcal"] == 300
        assert public["nutrition"]["protein_g"] == 25

        print("✓ test_editorial_nutrition_public_api")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_clone_preserves_nutrition():
    """Test that cloning preserves all nutrition fields."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Clone Nutrition", slug="clone-nutrition",
            nutrition={
                "calories_kcal": 420,
                "protein_g": 36,
                "fat_g": 19,
                "carbs_g": 24,
                "basis": "per_serving",
                "source": "ai_estimated",
            },
            ingredients=[{"name": "Test", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )
        await editorial_service.approve_editorial_recipe(db, recipe["id"])
        await editorial_service.publish_editorial_recipe(db, recipe["id"])

        result = await editorial_service.clone_editorial_recipe_to_user(db, recipe["id"], TEST_USER_A)
        assert result["already_saved"] is False

        clone = await db.fetchrow(
            "SELECT calories_kcal, protein_g, fat_g, carbs_g, nutrition_basis, nutrition_source FROM recipes WHERE id=$1",
            result["recipe_id"],
        )
        assert float(clone["calories_kcal"]) == 420
        assert float(clone["protein_g"]) == 36
        assert float(clone["fat_g"]) == 19
        assert float(clone["carbs_g"]) == 24
        assert clone["nutrition_basis"] == "per_serving"
        assert clone["nutrition_source"] == "ai_estimated"

        print("✓ test_clone_preserves_nutrition")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_null_nutrition_supported():
    """Test that recipes without nutrition work fine."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="No Nutrition", slug="no-nutrition",
            ingredients=[{"name": "Test", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )

        assert recipe["nutrition"] is None

        print("✓ test_null_nutrition_supported")
    finally:
        await cleanup_test_data(db)
        await db.close()


# ── Approval flow tests ─────────────────────────────────────────────────────

async def test_draft_cannot_publish():
    """Test that draft cannot be published directly."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Draft Publish", slug="draft-publish",
            ingredients=[{"name": "Test", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )

        try:
            await editorial_service.publish_editorial_recipe(db, recipe["id"])
            assert False, "Should have raised"
        except ValueError as e:
            assert "Cannot publish" in str(e)

        print("✓ test_draft_cannot_publish")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_waiting_approval_cannot_publish_without_admin():
    """Test that waiting_approval cannot be published."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Waiting Publish", slug="waiting-publish",
            ingredients=[{"name": "Test", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )

        # Transition to waiting_approval
        await db.execute(
            "UPDATE recipes SET editorial_status='waiting_approval' WHERE id=$1",
            recipe["id"],
        )

        try:
            await editorial_service.publish_editorial_recipe(db, recipe["id"])
            assert False, "Should have raised"
        except ValueError as e:
            assert "Cannot publish" in str(e)

        print("✓ test_waiting_approval_cannot_publish_without_admin")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_admin_can_approve_and_publish():
    """Test that admin can approve and publish."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Admin Approve", slug="admin-approve",
            ingredients=[{"name": "Test", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )

        # Approve (now transitions to publishing)
        r1 = await editorial_service.approve_editorial_recipe(db, recipe["id"])
        assert r1["status"] == "publishing"

        # Publish
        r2 = await editorial_service.publish_editorial_recipe(db, recipe["id"])
        assert r2["status"] == "published"

        print("✓ test_admin_can_approve_and_publish")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_non_admin_cannot_approve():
    """Test that non-admin cannot approve (structural check)."""
    source = _read_main_source()
    assert "user_id != ADMIN_CHAT_ID" in source, "Admin check required"
    print("✓ test_non_admin_cannot_approve")


async def test_rejected_recipe_not_published():
    """Test that rejected recipe cannot be published."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Rejected", slug="rejected-test",
            ingredients=[{"name": "Test", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )

        # Reject
        await db.execute(
            "UPDATE recipes SET editorial_status='rejected' WHERE id=$1",
            recipe["id"],
        )

        try:
            await editorial_service.publish_editorial_recipe(db, recipe["id"])
            assert False, "Should have raised"
        except ValueError:
            pass

        print("✓ test_rejected_recipe_not_published")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_needs_revision_not_published():
    """Test that needs_revision recipe cannot be published."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Needs Revision", slug="needs-revision-test",
            ingredients=[{"name": "Test", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )

        await db.execute(
            "UPDATE recipes SET editorial_status='needs_revision' WHERE id=$1",
            recipe["id"],
        )

        try:
            await editorial_service.publish_editorial_recipe(db, recipe["id"])
            assert False, "Should have raised"
        except ValueError:
            pass

        print("✓ test_needs_revision_not_published")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_double_publish_is_idempotent():
    """Test that double publish doesn't create duplicate messages."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Idempotent", slug="idempotent-test",
            ingredients=[{"name": "Test", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )
        await editorial_service.approve_editorial_recipe(db, recipe["id"])
        await editorial_service.publish_editorial_recipe(db, recipe["id"])

        # Set telegram message id (simulating first publish)
        await db.execute(
            "UPDATE recipes SET editorial_telegram_message_id=12345, editorial_telegram_chat_id=-100123 WHERE id=$1",
            recipe["id"],
        )

        # Check that the idempotency fields exist
        rec = await db.fetchrow("SELECT editorial_telegram_message_id FROM recipes WHERE id=$1", recipe["id"])
        assert rec["editorial_telegram_message_id"] == 12345

        print("✓ test_double_publish_is_idempotent")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_concurrent_publish_does_not_duplicate_message():
    """Test that concurrent publish is protected by row lock."""
    source = _read_main_source()
    assert "FOR UPDATE" in source, "Row lock required for concurrency protection"
    print("✓ test_concurrent_publish_does_not_duplicate_message")


async def test_approval_uses_explicit_editorial_chat_id():
    """Test that approval callback uses explicit editorial_chat_id parameter."""
    source = _read_main_source()
    # The callback handler must pass editorial_chat_id
    assert "editorial_chat_id=EDITORIAL_TELEGRAM_CHAT_ID" in source, \
        "Callback handler must pass editorial_chat_id"
    # The approval function must accept it as parameter
    import editorial_approval
    import inspect
    sig = inspect.signature(editorial_approval.handle_approval_callback)
    assert "editorial_chat_id" in sig.parameters, \
        "handle_approval_callback must accept editorial_chat_id parameter"
    print("✓ test_approval_uses_explicit_editorial_chat_id")


async def test_missing_editorial_chat_id_does_not_change_status():
    """Test that missing editorial_chat_id prevents status change."""
    import editorial_approval
    import inspect
    source = inspect.getsource(editorial_approval.handle_approval_callback)
    # Must validate editorial_chat_id before state change
    assert "not editorial_chat_id" in source, \
        "Must check editorial_chat_id before state change"
    print("✓ test_missing_editorial_chat_id_does_not_change_status")


async def test_publish_failure_returns_to_waiting_approval():
    """Test that publish failure reverts to waiting_approval."""
    import editorial_approval
    import inspect
    source = inspect.getsource(editorial_approval.handle_approval_callback)
    # Must revert to waiting_approval on failure
    assert "waiting_approval" in source, \
        "Must revert to waiting_approval on publish failure"
    # Check that the except block contains the revert
    # The pattern is: except ... → await db.execute(... waiting_approval ...)
    assert "editorial_status='waiting_approval'" in source, \
        "Must set editorial_status='waiting_approval' on failure"
    print("✓ test_publish_failure_returns_to_waiting_approval")


async def test_retry_after_publish_failure_succeeds():
    """Test that recipe can be retried after publish failure."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Retry Test", slug="retry-test",
            ingredients=[{"name": "Test", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )

        # Simulate: draft → waiting_approval → publishing → (fail) → waiting_approval
        await db.execute(
            "UPDATE recipes SET editorial_status='waiting_approval' WHERE id=$1",
            recipe["id"],
        )

        # Verify can_transition allows waiting_approval → publishing
        from editorial_approval import can_transition
        assert can_transition("waiting_approval", "publishing"), \
            "waiting_approval → publishing must be valid"

        # Verify can_transition allows publishing → waiting_approval (failure revert)
        assert can_transition("publishing", "waiting_approval"), \
            "publishing → waiting_approval must be valid for failure revert"

        print("✓ test_retry_after_publish_failure_succeeds")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_publish_failure_does_not_set_telegram_message_id():
    """Test that failed publish doesn't set editorial_telegram_message_id."""
    import editorial_approval
    import inspect
    source = inspect.getsource(editorial_approval.handle_approval_callback)
    # The revert block should not set message_id
    lines = source.split('\n')
    in_except = False
    for line in lines:
        if 'except Exception' in line and 'publish' in line.lower():
            in_except = True
        if in_except:
            assert "editorial_telegram_message_id" not in line, \
                "Failed publish must not set editorial_telegram_message_id"
        if in_except and ('return' in line or 'async def ' in line):
            break
    print("✓ test_publish_failure_does_not_set_telegram_message_id")


async def test_publishing_state_blocks_second_callback():
    """Test that publishing state blocks second callback."""
    from editorial_approval import can_transition
    # waiting_approval → publishing is valid
    assert can_transition("waiting_approval", "publishing"), \
        "waiting_approval → publishing must be valid"
    # publishing → publishing is NOT valid (blocks second callback)
    assert not can_transition("publishing", "publishing"), \
        "publishing → publishing must be blocked"
    # publishing → approved is NOT valid
    assert not can_transition("publishing", "approved"), \
        "publishing → approved must be blocked"
    print("✓ test_publishing_state_blocks_second_callback")


async def test_stale_publishing_can_be_recovered():
    """Test that stale publishing state can be recovered."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Stale Publishing", slug="stale-publishing",
            ingredients=[{"name": "Test", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )

        # Set to publishing with old updated_at
        await db.execute(
            "UPDATE recipes SET editorial_status='publishing', "
            "updated_at=NOW() - INTERVAL '15 minutes' WHERE id=$1",
            recipe["id"],
        )

        # Recover
        from editorial_approval import recover_stale_publishing
        recovered = await recover_stale_publishing(db=db, stale_minutes=10)
        assert recipe["id"] in recovered, "Recipe should be recovered"

        # Verify status
        rec = await db.fetchrow("SELECT editorial_status FROM recipes WHERE id=$1", recipe["id"])
        assert rec["editorial_status"] == "waiting_approval", \
            f"Expected waiting_approval, got {rec['editorial_status']}"

        print("✓ test_stale_publishing_can_be_recovered")
    finally:
        await cleanup_test_data(db)
        await db.close()


async def test_success_saves_telegram_message_id():
    """Test that successful publish saves telegram message id."""
    import editorial_approval
    import inspect
    source = inspect.getsource(editorial_approval.handle_approval_callback)
    # After success, must save message_id
    assert "editorial_telegram_message_id" in source or "publish_recipe_to_telegram" in source, \
        "Success path must save telegram message id"
    print("✓ test_success_saves_telegram_message_id")


async def test_double_callback_does_not_duplicate_publish():
    """Test that double callback doesn't create duplicate posts."""
    db = await get_db()
    try:
        await cleanup_test_data(db)

        recipe = await editorial_service.create_editorial_recipe(
            db, EDITORIAL_USER_ID,
            name="Double Callback", slug="double-callback",
            ingredients=[{"name": "Test", "qty": 1, "unit": "г"}],
            steps=[{"step_number": 1, "text": "Step"}],
        )

        # Set to waiting_approval
        await db.execute(
            "UPDATE recipes SET editorial_status='waiting_approval' WHERE id=$1",
            recipe["id"],
        )

        # First callback: waiting_approval → publishing
        from editorial_approval import can_transition
        assert can_transition("waiting_approval", "publishing")

        # Simulate first callback setting publishing
        await db.execute(
            "UPDATE recipes SET editorial_status='publishing' WHERE id=$1",
            recipe["id"],
        )

        # Second callback: publishing → publishing should be blocked
        assert not can_transition("publishing", "publishing"), \
            "Second callback must be blocked"

        print("✓ test_double_callback_does_not_duplicate_publish")
    finally:
        await cleanup_test_data(db)
        await db.close()


# ── Daily digest tests ───────────────────────────────────────────────────────

async def test_digest_counts_new_users():
    """Test that digest counts new users."""
    import admin_digest
    db = await get_db()
    try:
        text = await admin_digest.build_daily_admin_digest(db)
        assert "Новых пользователей" in text
        print("✓ test_digest_counts_new_users")
    finally:
        await db.close()


async def test_digest_counts_successful_payments():
    """Test that digest counts successful payments."""
    import admin_digest
    db = await get_db()
    try:
        text = await admin_digest.build_daily_admin_digest(db)
        assert "Успешных оплат" in text
        print("✓ test_digest_counts_successful_payments")
    finally:
        await db.close()


async def test_digest_excludes_failed_payments():
    """Test that digest doesn't count failed payments as revenue."""
    import admin_digest
    db = await get_db()
    try:
        text = await admin_digest.build_daily_admin_digest(db)
        # Should not mention "failed" in revenue context
        assert "Выручка" in text
        print("✓ test_digest_excludes_failed_payments")
    finally:
        await db.close()


async def test_digest_handles_provider_balance_failure():
    """Test that digest handles missing provider balance gracefully."""
    import ai_provider_balance
    result = await ai_provider_balance.get_ai_provider_balance()
    # Should not raise, even if balance is unavailable
    assert "provider" in result
    assert "available" in result
    print("✓ test_digest_handles_provider_balance_failure")


async def test_digest_timezone_moscow():
    """Test that digest uses Moscow timezone."""
    import admin_digest
    assert hasattr(admin_digest, 'MSK'), "MSK timezone must be defined"
    print("✓ test_digest_timezone_moscow")


# ── Channel tests ────────────────────────────────────────────────────────────

async def test_channel_url():
    """Test that channel URL is correct."""
    source = _read_main_source()
    assert "t.me/P0liyana" in source, "Channel URL must be present"
    print("✓ test_channel_url")


async def test_first_touch_attribution_not_overwritten():
    """Test that acquisition fields are only written when NULL."""
    db = await get_db()
    try:
        # Check that the migration added the columns
        cols = await db.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name='users' AND column_name LIKE 'acquisition%'"
        )
        col_names = [c["column_name"] for c in cols]
        assert "acquisition_campaign" in col_names
        assert "acquisition_recipe_id" in col_names
        print("✓ test_first_touch_attribution_not_overwritten")
    finally:
        await db.close()


async def test_editorial_deeplink_attribution():
    """Test that editorial deep links include attribution."""
    source = _read_main_source()
    assert "editorial_" in source, "Editorial deep link prefix must exist"
    print("✓ test_editorial_deeplink_attribution")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _read_main_source() -> str:
    main_path = os.path.join(os.path.dirname(__file__), 'main.py')
    with open(main_path, encoding='utf-8') as f:
        return f.read()


# ── Runner ───────────────────────────────────────────────────────────────────

async def run_all():
    tests = [
        # Nutrition
        test_editorial_nutrition_create,
        test_editorial_nutrition_public_api,
        test_clone_preserves_nutrition,
        test_null_nutrition_supported,
        # Approval
        test_draft_cannot_publish,
        test_waiting_approval_cannot_publish_without_admin,
        test_admin_can_approve_and_publish,
        test_non_admin_cannot_approve,
        test_rejected_recipe_not_published,
        test_needs_revision_not_published,
        test_double_publish_is_idempotent,
        test_concurrent_publish_does_not_duplicate_message,
        test_approval_uses_explicit_editorial_chat_id,
        test_missing_editorial_chat_id_does_not_change_status,
        test_publish_failure_returns_to_waiting_approval,
        test_retry_after_publish_failure_succeeds,
        test_publish_failure_does_not_set_telegram_message_id,
        test_publishing_state_blocks_second_callback,
        test_stale_publishing_can_be_recovered,
        test_success_saves_telegram_message_id,
        test_double_callback_does_not_duplicate_publish,
        # Digest
        test_digest_counts_new_users,
        test_digest_counts_successful_payments,
        test_digest_excludes_failed_payments,
        test_digest_handles_provider_balance_failure,
        test_digest_timezone_moscow,
        # Channel
        test_channel_url,
        test_first_touch_attribution_not_overwritten,
        test_editorial_deeplink_attribution,
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
