"""Focused tests for the prepared-message Share contract."""

from datetime import datetime, timezone
from pathlib import Path
import unittest

from share_utils import (
    build_mini_app_deep_link,
    build_recipe_share_text,
    prepared_expiration_value,
    recipe_share_title,
)


class ShareUtilsTests(unittest.TestCase):
    def test_direct_mini_app_link_uses_registered_short_name(self):
        self.assertEqual(
            build_mini_app_deep_link(
                "@reciptesbot", "polyana", "shared_token-1"
            ),
            "https://t.me/reciptesbot/polyana?startapp=shared_token-1",
        )

    def test_direct_mini_app_link_requires_bot_and_app_names(self):
        with self.assertRaises(ValueError):
            build_mini_app_deep_link("", "polyana", "shared_token")
        with self.assertRaises(ValueError):
            build_mini_app_deep_link("reciptesbot", "", "shared_token")

    def test_recipe_message_escapes_all_dynamic_html(self):
        text = build_recipe_share_text({
            "emoji": "🍲",
            "name": '<b onclick="x">Суп & соус</b>',
            "category": "Обед <ужин>",
            "servings": "2 & 3",
            "ingredients": [{
                "name": '<img src=x onerror="alert(1)">',
                "qty": "1.25",
                "unit": "cup<script>",
            }],
            "steps": [{"step_number": 1, "text": "Смешать <быстро> & подать"}],
        })

        self.assertIn("<b>&lt;b onclick=\"x\"&gt;Суп &amp; соус&lt;/b&gt;</b>", text)
        self.assertIn("Обед &lt;ужин&gt;", text)
        self.assertIn("2 &amp; 3", text)
        self.assertIn(
            "&lt;img src=x onerror=\"alert(1)\"&gt; — 1.25 cup&lt;script&gt;",
            text,
        )
        self.assertIn("Смешать &lt;быстро&gt; &amp; подать", text)
        self.assertNotIn("<script>", text)

    def test_recipe_message_is_compact_and_reports_omitted_items(self):
        text = build_recipe_share_text({
            "name": "Большой рецепт",
            "ingredients": [{"name": f"Ингредиент {i}"} for i in range(15)],
            "steps": [{"step_number": i, "text": f"Шаг {i}"} for i in range(1, 9)],
        })

        self.assertIn("… и ещё 3", text)
        self.assertIn("… и ещё 2 шагов", text)
        self.assertIn("Ингредиент 11", text)
        self.assertNotIn("Ингредиент 12", text)
        self.assertIn("Шаг 6", text)
        self.assertNotIn("Шаг 7", text)

    def test_share_title_and_expiration_are_bot_api_safe(self):
        self.assertEqual(len(recipe_share_title({"name": "а" * 100})), 64)
        prepared = type("Prepared", (), {
            "expiration_date": datetime(2030, 1, 1, tzinfo=timezone.utc)
        })()
        self.assertEqual(prepared_expiration_value(prepared), 1893456000)
        numeric = type("Prepared", (), {"expiration_date": 123.9})()
        self.assertEqual(prepared_expiration_value(numeric), 123)
        self.assertIsNone(prepared_expiration_value(object()))

    def test_main_share_paths_use_direct_mini_app_contract(self):
        source = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
        self.assertNotIn('f"https://t.me/{bot_username}?startapp=', source)
        self.assertGreaterEqual(source.count("await _get_mini_app_url("), 4)
        self.assertIn('MINI_APP_SHORT_NAME = ENV("MINI_APP_SHORT_NAME", "polyana")', source)
        self.assertGreaterEqual(source.count('"mini_app_url": mini_app_url'), 4)
        self.assertGreaterEqual(source.count('"token": token'), 4)


if __name__ == "__main__":
    unittest.main()
