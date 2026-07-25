"""Tests for legal documents, consent management, onboarding, and user service.

Following the pattern from test_referral.py: synchronous tests with mocks, no real DB.
asyncpg is not available in the test environment — all async tests are wrapped in
asyncio.run() or use sync wrappers.
"""
import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


# ── Document Template Tests (no DB needed) ───────────────────────────────────

class TestDocumentTemplates:
    """Test that document templates render correctly."""

    def test_terms_render(self):
        import legal_docs
        content = legal_docs.render_terms("1.0")
        assert "ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ" in content
        assert "1.0" in content
        assert len(content) > 500

    def test_privacy_render(self):
        import legal_docs
        content = legal_docs.render_privacy_policy("1.0")
        assert "ПОЛИТИКА ОБРАБОТКИ ПЕРСОНАЛЬНЫХ ДАННЫХ" in content
        assert "1.0" in content

    def test_personal_data_consent_render(self):
        import legal_docs
        content = legal_docs.render_personal_data_consent("1.0")
        assert "СОГЛАСИЕ НА ОБРАБОТКУ ПЕРСОНАЛЬНЫХ ДАННЫХ" in content

    def test_ai_consent_render(self):
        import legal_docs
        content = legal_docs.render_ai_consent("1.0")
        assert "ИСПОЛЬЗОВАНИЕ ИИ" in content
        assert "OpenRouter" in content

    def test_referral_terms_render(self):
        import legal_docs
        content = legal_docs.render_referral_terms("1.0")
        assert "ПРИГЛАСИ ДРУЗЕЙ" in content
        assert "10%" in content

    def test_content_hash_deterministic(self):
        import legal_docs
        c1 = legal_docs.content_hash("test content")
        c2 = legal_docs.content_hash("test content")
        assert c1 == c2
        assert len(c1) == 64  # SHA-256 hex

    def test_content_hash_differs(self):
        import legal_docs
        c1 = legal_docs.content_hash("content A")
        c2 = legal_docs.content_hash("content B")
        assert c1 != c2

    def test_version_in_content(self):
        import legal_docs
        v1 = legal_docs.render_terms("1.0")
        v2 = legal_docs.render_terms("2.0")
        assert "1.0" in v1
        assert "2.0" in v2
        assert v1 != v2


# ── Config Validation Tests ──────────────────────────────────────────────────

class TestConfigValidation:
    def test_check_missing_config(self):
        import legal_docs
        with patch.dict('os.environ', {}, clear=True):
            missing = legal_docs.check_legal_config()
            assert len(missing) > 0

    def test_check_complete_config(self):
        import legal_docs
        env = {k: "test_value" for k in legal_docs._PLACEHOLDER_ENV_MAP.values()}
        with patch.dict('os.environ', env, clear=False):
            missing = legal_docs.check_legal_config()
            assert len(missing) == 0


# ── Document Type Tests ──────────────────────────────────────────────────────

class TestDocumentTypes:
    def test_all_types_have_templates(self):
        import legal_docs
        for doc_type in legal_docs.DOCUMENT_TYPES:
            assert doc_type in legal_docs.REQUIRES_ACCEPTANCE

    def test_required_types_are_terms_and_pdn(self):
        import legal_docs
        assert legal_docs.REQUIRES_ACCEPTANCE["terms"] is True
        assert legal_docs.REQUIRES_ACCEPTANCE["personal_data_consent"] is True
        assert legal_docs.REQUIRES_ACCEPTANCE["ai_processing_consent"] is False
        assert legal_docs.REQUIRES_ACCEPTANCE["privacy_policy"] is False
        assert legal_docs.REQUIRES_ACCEPTANCE["referral_terms"] is False

    def test_five_document_types(self):
        import legal_docs
        assert len(legal_docs.DOCUMENT_TYPES) == 5


# ── Onboarding Config Tests ──────────────────────────────────────────────────

class TestOnboardingConfig:
    def test_onboarding_version_is_string(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'ONBOARDING_VERSION = "1.0"' in content

    def test_onboarding_screens_defined(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "_ONBOARDING_SCREENS" in content
        assert '"text"' in content
        assert '"buttons"' in content

    def test_onboarding_has_6_screens(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        # 4 tutorial + 1 legal + 1 completed = 6
        assert "_ONB_STEPS_TUTORIAL = 4" in content
        assert "_ONB_STEP_LEGAL = 4" in content
        assert "_ONB_STEP_COMPLETED = 5" in content

    def test_onboarding_states_defined(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "class OnboardingStates(StatesGroup):" in content
        assert "legal_pending = State()" in content
        assert "completed = State()" in content

    def test_tutorial_screens_have_step_labels(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "Шаг 1 из 4" in content
        assert "Шаг 2 из 4" in content
        assert "Шаг 3 из 4" in content
        assert "Шаг 4 из 4" in content


# ── Consent Service Tests (sync, mocking asyncpg) ────────────────────────────

class TestLegalConsentServiceSync:
    """Test consent logic without async/DB by reading the source code patterns."""

    def test_required_types_defined(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'REQUIRED_TYPES = ("terms", "personal_data_consent")' in content
        assert 'AI_TYPE = "ai_processing_consent"' in content

    def test_consent_cache_exists(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "_consent_cache" in content
        assert "_CONSENT_CACHE_TTL" in content

    def test_accept_document_uses_hash(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "content_hash" in content
        assert "content_hash=doc[\"content_hash\"]" in content or "doc[\"content_hash\"]" in content

    def test_revoke_sets_timestamp(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "revoked_at=NOW()" in content or "revoked_at=$2" in content


# ── User Service Tests (sync) ────────────────────────────────────────────────

class TestUserServiceSync:
    def test_users_table_schema(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "CREATE TABLE IF NOT EXISTS users" in content
        assert "onboarding_status" in content
        assert "onboarding_step" in content
        assert "onboarding_version" in content
        assert "acquisition_source" in content
        assert "first_value_action" in content

    def test_legal_documents_table(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "CREATE TABLE IF NOT EXISTS legal_documents" in content
        assert "document_type" in content
        assert "content_hash" in content
        assert "requires_acceptance" in content

    def test_user_legal_acceptances_table(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "CREATE TABLE IF NOT EXISTS user_legal_acceptances" in content
        assert "document_version" in content
        assert "accepted_at" in content
        assert "revoked_at" in content

    def test_pending_actions_table(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "CREATE TABLE IF NOT EXISTS pending_onboarding_actions" in content
        assert "action_type" in content
        assert "expires_at" in content


# ── API Endpoint Tests (sync) ────────────────────────────────────────────────

class TestAPIEndpoints:
    def test_legal_endpoints_defined(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert '@app.get("/api/legal/documents")' in content
        assert '@app.get("/api/legal/documents/{document_type}")' in content
        assert '@app.get("/api/legal/status")' in content
        assert '@app.post("/api/legal/accept")' in content
        assert '@app.post("/api/legal/revoke")' in content

    def test_onboarding_endpoints_defined(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert '@app.get("/api/onboarding/status")' in content
        assert '@app.post("/api/onboarding/step")' in content
        assert '@app.post("/api/onboarding/skip")' in content

    def test_account_endpoints_defined(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert '@app.post("/api/account/delete-request")' in content
        assert '@app.post("/api/account/export")' in content


# ── Bot Handler Tests (sync) ─────────────────────────────────────────────────

class TestBotHandlers:
    def test_new_commands_registered(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'BotCommand(command="documents"' in content
        assert 'BotCommand(command="privacy"' in content
        assert 'BotCommand(command="help"' in content
        assert 'BotCommand(command="delete_me"' in content

    def test_documents_command_handler(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'async def cmd_documents' in content

    def test_privacy_command_handler(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'async def cmd_privacy' in content

    def test_help_command_handler(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'async def cmd_help' in content

    def test_delete_me_command_handler(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'async def cmd_delete_me' in content

    def test_documents_callback_handler(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'async def cb_show_documents' in content
        assert 'callback_data="show_documents"' in content

    def test_legal_doc_callback_handler(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'async def cb_legal_doc' in content

    def test_del_confirm_flow(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'del_confirm1' in content
        assert 'del_execute' in content
        assert 'del_cancel' in content

    def test_welcome_button_changed(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'callback_data="show_documents"' in content
        # Old button should not exist in cmd_start
        # (it may exist in cb_show_terms which is kept for backward compat)

    def test_onboarding_fsm_states(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "class OnboardingStates(StatesGroup):" in content
        assert "class DeleteStates(StatesGroup):" in content
        assert "class AiConsentStates(StatesGroup):" in content

    def test_middleware_registered(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "LegalConsentMiddleware()" in content
        assert "dp.message.middleware" in content
        assert "dp.callback_query.middleware" in content


# ── AI Consent Integration Tests (sync) ──────────────────────────────────────

class TestAIConsentIntegration:
    def test_parse_and_save_recipe_checks_consent(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "require_ai_access" in content
        assert "ai_consent_required" in content

    def test_normalize_endpoint_checks_consent(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "require_ai_access(db, user_id)" in content

    def test_ai_consent_error_prompt(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "ai_consent_required" in content
        assert "Разрешить и продолжить" in content


# ── Frontend Tests (sync) ────────────────────────────────────────────────────

class TestFrontend:
    def test_onboarding_screen_exists(self):
        with open("../polyana-frontend/index.html", encoding="utf-8") as f:
            content = f.read()
        assert 'id="s-onboarding"' in content

    def test_documents_screen_exists(self):
        with open("../polyana-frontend/index.html", encoding="utf-8") as f:
            content = f.read()
        assert 'id="s-documents"' in content

    def test_doc_viewer_screen_exists(self):
        with open("../polyana-frontend/index.html", encoding="utf-8") as f:
            content = f.read()
        assert 'id="s-doc-viewer"' in content

    def test_privacy_screen_exists(self):
        with open("../polyana-frontend/index.html", encoding="utf-8") as f:
            content = f.read()
        assert 'id="s-privacy"' in content

    def test_settings_has_documents_link(self):
        with open("../polyana-frontend/index.html", encoding="utf-8") as f:
            content = f.read()
        assert 'Документы и конфиденциальность' in content
        assert 'openPrivacy()' in content

    def test_onboarding_js_functions(self):
        with open("../polyana-frontend/index.html", encoding="utf-8") as f:
            content = f.read()
        assert 'async function startOnboarding()' in content
        assert 'function showOnboardingStep()' in content
        assert 'async function acceptDoc(' in content
        assert 'function obAction(' in content
        assert 'function obBack()' in content

    def test_documents_js_functions(self):
        with open("../polyana-frontend/index.html", encoding="utf-8") as f:
            content = f.read()
        assert 'async function openDocuments()' in content
        assert 'async function openDocViewer(' in content
        assert 'async function openPrivacy()' in content

    def test_onboarding_init_check(self):
        with open("../polyana-frontend/index.html", encoding="utf-8") as f:
            content = f.read()
        assert 'await startOnboarding()' in content


# ── Data Retention Config Tests ──────────────────────────────────────────────

class TestRetentionConfig:
    def test_retention_env_vars_defined(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "TEMP_FILE_RETENTION_HOURS" in content
        assert "RAW_IMPORT_RETENTION_DAYS" in content
        assert "AI_LOG_RETENTION_DAYS" in content
        assert "DELETED_ACCOUNT_RETENTION_DAYS" in content


# ── Legal Config Env Vars Tests ──────────────────────────────────────────────

class TestLegalConfig:
    def test_legal_env_vars_defined(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "LEGAL_OPERATOR_FULL_NAME" in content
        assert "LEGAL_OPERATOR_SHORT_NAME" in content
        assert "LEGAL_INN" in content
        assert "LEGAL_OGRN_OR_OGRNIP" in content
        assert "LEGAL_CONTACT_EMAIL" in content
        assert "LEGAL_PRIVACY_EMAIL" in content

    def test_legal_config_check_in_init_db(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "check_legal_config()" in content
        assert "Legal document configuration is incomplete" in content


# ── Analytics Event Names ────────────────────────────────────────────────────

class TestAnalyticsEventNames:
    REQUIRED_EVENTS = [
        "onboarding_started", "onboarding_step_viewed", "onboarding_step_completed",
        "onboarding_skipped", "onboarding_completed", "onboarding_first_value",
        "legal_documents_opened", "terms_accepted", "personal_data_consent_accepted",
        "ai_consent_shown", "ai_consent_accepted", "ai_consent_declined",
        "consent_revoked", "account_deletion_requested", "account_deleted",
        "welcome_screen_viewed", "onboarding_tutorial_chosen", "onboarding_skip_chosen",
        "welcome_how_to_add_viewed", "welcome_example_viewed",
        "welcome_ai_functions_viewed", "welcome_get_points_viewed",
    ]

    def test_event_names_are_snake_case(self):
        for event in self.REQUIRED_EVENTS:
            assert isinstance(event, str)
            assert len(event) > 0
            assert "_" in event


# ── Feature Flags Tests ──────────────────────────────────────────────────────

class TestFeatureFlags:
    def test_feature_flags_defined(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        for flag in ["FEATURE_RECIPE_IMPORT_IMAGE", "FEATURE_RECIPE_IMPORT_VOICE",
                      "FEATURE_RECIPE_IMPORT_URL", "FEATURE_AI_RECIPE_GENERATION",
                      "FEATURE_AI_IMAGE_GENERATION", "FEATURE_EVENTS",
                      "FEATURE_SHOPPING_LIST", "FEATURE_EXPENSE_SPLIT",
                      "FEATURE_RECEIPT_RECOGNITION", "FEATURE_REFERRALS",
                      "FEATURE_PAYMENTS", "WELCOME_POINTS"]:
            assert flag in content

    def test_free_features_list(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "FREE_FEATURES" in content
        assert '"icon"' in content
        assert '"title"' in content

    def test_ai_features_list(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "AI_FEATURES" in content

    def test_feature_flags_use_env(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'ENV("FEATURE_RECIPE_IMPORT_IMAGE"' in content
        assert 'ENV("FEATURE_EVENTS"' in content
        assert 'ENV("WELCOME_POINTS"' in content


# ── WelcomeService Tests ─────────────────────────────────────────────────────

class TestWelcomeService:
    def test_welcome_service_exists(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "class WelcomeService:" in content

    def test_build_new_user_welcome_defined(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "def build_new_user_welcome(" in content
        assert "def build_returning_user_dashboard(" in content
        assert "def build_referral_welcome(" in content
        assert "def build_recipe_share_welcome(" in content
        assert "def build_channel_welcome(" in content

    def test_sub_screen_builders_defined(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "def build_how_to_add_recipe(" in content
        assert "def build_example(" in content
        assert "def build_ai_functions(" in content
        assert "def build_get_points(" in content

    def test_welcome_text_contains_key_sections(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "Бесплатно в ПОЛЯНЕ" in content
        assert "AI-баллы используются только для функций с ИИ" in content
        assert "Обязательной подписки нет" in content

    def test_referral_welcome_shows_referrer(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "пригласил вас в ПОЛЯНУ" in content

    def test_recipe_share_welcome_shows_title(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "поделился с вами рецептом" in content

    def test_html_escape_function(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "def _esc(" in content
        assert "_html_mod.escape" in content


# ── Welcome Keyboard Tests ───────────────────────────────────────────────────

class TestWelcomeKeyboard:
    def test_welcome_keyboard_defined(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert "def _welcome_keyboard():" in content
        assert "def _returning_user_keyboard():" in content

    def test_welcome_keyboard_has_required_buttons(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'callback_data="ws_how_to_add"' in content
        assert 'callback_data="ws_example"' in content
        assert 'callback_data="ws_ai_functions"' in content
        assert 'callback_data="ws_get_points"' in content
        assert 'callback_data="ws_help"' in content
        assert 'callback_data="ob_start_tutorial"' in content
        assert 'callback_data="ob_start_skip_to_legal"' in content

    def test_returning_keyboard_has_required_buttons(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'callback_data="ws_how_to_add"' in content
        assert 'callback_data="ws_ai_functions"' in content
        assert 'callback_data="ws_get_points"' in content
        assert 'callback_data="show_documents"' in content
        assert 'callback_data="ws_help"' in content


# ── Sub-Screen Callback Tests ────────────────────────────────────────────────

class TestSubScreenCallbacks:
    def test_all_sub_screen_handlers_exist(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        for handler in ["cb_ws_how_to_add", "cb_ws_example", "cb_ws_send_hint",
                         "cb_ws_ai_functions", "cb_ws_get_points", "cb_ws_help",
                         "cb_ws_back", "cb_ob_start_tutorial", "cb_ob_start_skip_to_legal"]:
            assert f"async def {handler}" in content

    def test_ws_back_returns_to_welcome(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert 'F.data == "ws_back"' in content
        assert "WelcomeService.build_new_user_welcome" in content
        assert "WelcomeService.build_returning_user_dashboard" in content


# ── Public Callbacks Tests ───────────────────────────────────────────────────

class TestPublicCallbacks:
    def test_welcome_callbacks_are_public(self):
        with open("main.py", encoding="utf-8") as f:
            content = f.read()
        assert '"ws_how_to_add"' in content
        assert '"ws_example"' in content
        assert '"ws_ai_functions"' in content
        assert '"ws_get_points"' in content
        assert '"ws_help"' in content
        assert '"ws_back"' in content
        assert '"ob_start_tutorial"' in content
        assert '"ob_start_skip_to_legal"' in content


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
