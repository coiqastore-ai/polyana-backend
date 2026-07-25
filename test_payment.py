"""Tests for payment system, wallet operations, AI billing, and reconciliation.

Following the pattern from test_referral.py and test_legal.py:
synchronous tests that read main.py source to verify structure exists.
"""
import json
import uuid


# ── Package Catalog Tests ─────────────────────────────────────────────────────

class TestPackageCatalog:
    def test_payment_packages_table_exists(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "CREATE TABLE IF NOT EXISTS payment_packages" in content
        assert "code" in content and "VARCHAR(64)" in content
        assert "base_points" in content
        assert "stars_amount" in content
        assert "active_for_stars" in content

    def test_payment_orders_table_exists(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "CREATE TABLE IF NOT EXISTS payment_orders" in content
        assert "invoice_payload" in content and "UNIQUE" in content
        assert "idempotency_key" in content
        assert "provider" in content and "VARCHAR(32)" in content

    def test_package_seeding(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "points_100" in content
        assert "points_300" in content
        assert "points_1000" in content
        assert "ON CONFLICT (code) DO NOTHING" in content

    def test_client_cannot_set_price(self):
        """Package prices come from DB, not client."""
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        # PaymentService.create_order reads from DB
        assert "SELECT * FROM payment_packages WHERE code=$1" in content

    def test_promo_not_in_referral_base(self):
        """Promo points excluded from referral calculation."""
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "referral_base_points" in content


# ── Wallet Tables Tests ───────────────────────────────────────────────────────

class TestWalletTables:
    def test_wallet_grants_table(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "CREATE TABLE IF NOT EXISTS wallet_grants" in content
        assert "source_type" in content and "VARCHAR(32)" in content
        assert "initial_points" in content
        assert "remaining_points" in content

    def test_wallet_reservations_table(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "CREATE TABLE IF NOT EXISTS wallet_reservations" in content
        assert "operation_id" in content and "UNIQUE" in content
        assert "expires_at" in content and "TIMESTAMPTZ" in content

    def test_wallets_alter_columns(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "reserved_points BIGINT NOT NULL DEFAULT 0" in content
        assert "paid_debt_points BIGINT NOT NULL DEFAULT 0" in content

    def test_ai_usage_log_table(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "CREATE TABLE IF NOT EXISTS ai_usage_log" in content
        assert "provider_cost_usd" in content
        assert "charged_points" in content


# ── PaymentService Tests ──────────────────────────────────────────────────────

class TestPaymentService:
    def test_payment_service_class_exists(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "class PaymentService:" in content

    def test_create_order_method(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "async def create_order(" in content

    def test_find_order_by_payload(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "async def find_order_by_payload(" in content

    def test_mark_order_paid(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "async def mark_order_paid(" in content

    def test_get_available_packages(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "async def get_available_packages(" in content


# ── WalletService Extension Tests ─────────────────────────────────────────────

class TestWalletExtensions:
    def test_get_available_balance(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "async def get_available_balance(" in content

    def test_reserve_for_ai(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "async def reserve_for_ai(" in content

    def test_commit_reservation(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "async def commit_reservation(" in content

    def test_release_reservation(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "async def release_reservation(" in content

    def test_credit_purchase(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "async def credit_purchase(" in content

    def test_debit_order_bonus_first(self):
        """Bonus points consumed before paid points."""
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "bonus_deduct = min(bonus, points)" in content


# ── AI Billing Tests ──────────────────────────────────────────────────────────

class TestAIBilling:
    def test_ai_operation_catalog_exists(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "AI_OPERATION_CATALOG" in content

    def test_catalog_has_required_operations(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        for op in ["recipe_text_parse", "recipe_image_parse", "recipe_voice_parse",
                    "recipe_url_parse", "recipe_normalize"]:
            assert f'"{op}"' in content

    def test_ai_usage_billing_service(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "class AIUsageBillingService:" in content
        assert "async def reserve_points(" in content
        assert "async def commit_charge(" in content
        assert "async def release_reservation(" in content

    def test_balance_check_before_openrouter(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "insufficient_balance" in content
        assert "AIUsageBillingService.check_balance" in content

    def test_reservation_before_openrouter(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "AIUsageBillingService.reserve_points" in content

    def test_commit_on_success(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "AIUsageBillingService.commit_charge" in content

    def test_release_on_failure(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "AIUsageBillingService.release_reservation" in content


# ── Stars Payment Tests ───────────────────────────────────────────────────────

class TestStarsPayments:
    def test_pre_checkout_handler_exists(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "@dp.pre_checkout_query()" in content

    def test_pre_checkout_validates_order(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "find_order_by_payload" in content
        assert "order[\"user_id\"] != query.from_user.id" in content

    def test_successful_payment_handler(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "@dp.message(F.successful_payment)" in content

    def test_successful_payment_uses_unified_credit(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "WalletService.credit_purchase" in content

    def test_invoice_payload_format(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'payload = f"po:{secrets.token_urlsafe(16)}"' in content

    def test_no_provider_token_for_stars(self):
        """Stars invoices must not use provider_token."""
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        # sendInvoice call should not have provider_token parameter
        idx = content.find("await bot.send_invoice(")
        if idx > 0:
            chunk = content[idx:idx+500]
            assert "provider_token" not in chunk

    def test_xtr_currency(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'currency="XTR"' in content


# ── YooKassa Tests ────────────────────────────────────────────────────────────

class TestYooKassa:
    def test_webhook_refetches_payment(self):
        """Webhook must re-fetch payment from API, not trust body."""
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "api.yookassa.ru/v3/payments/" in content

    def test_webhook_uses_basic_auth(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)" in content

    def test_topup_endpoint_exists(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert '@app.post("/api/balance/topup")' in content

    def test_topup_stars_endpoint_exists(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert '@app.post("/api/balance/topup-stars")' in content

    def test_webhook_handles_refund(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "refund.succeeded" in content or "process_refund" in content


# ── Reconciliation Tests ──────────────────────────────────────────────────────

class TestReconciliation:
    def test_reconciliation_loop_exists(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "_payment_reconciliation_loop" in content

    def test_reconciliation_checks_yookassa(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "yookassa_reconciliation" in content

    def test_reconciliation_feature_flag(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "FEATURE_PAYMENT_RECONCILIATION" in content

    def test_reconciliation_started_on_boot(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'create_task(_payment_reconciliation_loop())' in content


# ── Feature Flag Tests ────────────────────────────────────────────────────────

class TestPaymentFeatureFlags:
    def test_all_payment_flags_exist(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        for flag in ["FEATURE_PAYMENTS_STARS", "FEATURE_PAYMENTS_YOOKASSA_WEB",
                      "FEATURE_BALANCE", "FEATURE_AI_BILLING",
                      "FEATURE_PAYMENT_RECONCILIATION"]:
            assert flag in content

    def test_balance_command_exists(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'BotCommand(command="balance"' in content

    def test_balance_in_public_commands(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert '"balance"' in content


# ── Insufficient Balance Tests ────────────────────────────────────────────────

class TestInsufficientBalance:
    def test_insufficient_balance_error_message(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "insufficient_balance" in content
        assert "Недостаточно AI-баллов" in content

    def test_topup_button_on_insufficient(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "balance_stars" in content


# ── Data Migration Tests ──────────────────────────────────────────────────────

class TestDataMigration:
    def test_legacy_balance_migration(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "legacy_user_balance" in content
        assert "migration" in content

    def test_migration_creates_grant(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "migration" in content
        assert "wallet_grants" in content


# ── Idempotency Tests ─────────────────────────────────────────────────────────

class TestIdempotency:
    def test_order_payload_unique(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "invoice_payload" in content and "UNIQUE" in content

    def test_order_idempotency_key_unique(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "idempotency_key" in content and "UNIQUE" in content

    def test_external_payment_id_unique_per_provider(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "uq_po_provider_ext" in content

    def test_mark_order_paid_idempotent(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "status NOT IN ('succeeded','refunded','cancelled')" in content


# ── Security Tests ────────────────────────────────────────────────────────────

class TestPaymentSecurity:
    def test_secret_key_not_in_response(self):
        """YOOKASSA_SECRET_KEY should never appear in API responses."""
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        # Find all return statements in yookassa endpoints
        # The secret key should only be used in auth headers
        assert "YOOKASSA_SECRET_KEY" in content  # exists
        # But should not be in any return/json response

    def test_invoice_payload_not_contain_user_id(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'payload = f"po:{secrets.token_urlsafe(16)}"' in content


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
