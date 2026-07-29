"""
Tests for nutrition module and recipe nutrition features.
"""
import pytest
from nutrition import (
    validate_nutrition,
    nutrition_per_serving,
    format_nutrition_short,
    format_nutrition_details,
    format_nutrition_card,
)


class TestValidateNutrition:
    """Test nutrition validation."""

    def test_valid_nutrition(self):
        data = {
            "total_kcal": 1720,
            "total_protein_g": 104,
            "total_fat_g": 72,
            "total_carbs_g": 168,
            "confidence": "medium",
            "assumptions": ["тест"],
        }
        result = validate_nutrition(data, 4)
        assert result["valid"] is True
        assert result["confidence"] == "medium"
        assert len(result["errors"]) == 0

    def test_missing_fields(self):
        data = {"total_kcal": 100}
        result = validate_nutrition(data, 4)
        assert result["valid"] is False
        assert len(result["errors"]) > 0

    def test_negative_values(self):
        data = {
            "total_kcal": -100,
            "total_protein_g": 10,
            "total_fat_g": 5,
            "total_carbs_g": 20,
        }
        result = validate_nutrition(data, 4)
        assert result["valid"] is False
        assert any("negative" in e for e in result["errors"])

    def test_string_values_rejected(self):
        data = {
            "total_kcal": "300",
            "total_protein_g": 10,
            "total_fat_g": 5,
            "total_carbs_g": 20,
        }
        result = validate_nutrition(data, 4)
        assert result["valid"] is False
        assert any("number" in e for e in result["errors"])

    def test_exceeds_maximum(self):
        data = {
            "total_kcal": 50000,
            "total_protein_g": 10,
            "total_fat_g": 5,
            "total_carbs_g": 20,
        }
        result = validate_nutrition(data, 4)
        assert result["valid"] is False
        assert any("exceeds" in e for e in result["errors"])

    def test_invalid_servings(self):
        data = {
            "total_kcal": 100,
            "total_protein_g": 10,
            "total_fat_g": 5,
            "total_carbs_g": 20,
        }
        result = validate_nutrition(data, 0)
        assert result["valid"] is False
        assert any("servings" in e for e in result["errors"])

    def test_energy_inconsistency(self):
        # kcal far from protein*4 + fat*9 + carbs*4
        data = {
            "total_kcal": 1000,
            "total_protein_g": 10,
            "total_fat_g": 10,
            "total_carbs_g": 10,
        }
        result = validate_nutrition(data, 4)
        assert result["valid"] is True  # Still valid, just low confidence
        assert result["confidence"] == "low"
        assert len(result["warnings"]) > 0

    def test_empty_data(self):
        result = validate_nutrition({}, 4)
        assert result["valid"] is False

    def test_none_data(self):
        result = validate_nutrition(None, 4)
        assert result["valid"] is False

    def test_assumptions_limited_to_3(self):
        data = {
            "total_kcal": 100,
            "total_protein_g": 10,
            "total_fat_g": 5,
            "total_carbs_g": 20,
            "assumptions": ["a", "b", "c", "d", "e"],
        }
        result = validate_nutrition(data, 4)
        assert len(result["notes"]) <= 3


class TestNutritionPerServing:
    """Test per-serving calculations."""

    def test_basic_calculation(self):
        recipe = {
            "nutrition_kcal_total": 1600,
            "nutrition_protein_g_total": 80,
            "nutrition_fat_g_total": 40,
            "nutrition_carbs_g_total": 200,
            "nutrition_servings_base": 4,
        }
        per = nutrition_per_serving(recipe)
        assert per["kcal"] == 400
        assert per["protein_g"] == 20.0
        assert per["fat_g"] == 10.0
        assert per["carbs_g"] == 50.0

    def test_missing_nutrition(self):
        recipe = {"name": "Test"}
        per = nutrition_per_serving(recipe)
        assert per is None

    def test_zero_servings(self):
        recipe = {
            "nutrition_kcal_total": 100,
            "nutrition_servings_base": 0,
        }
        per = nutrition_per_serving(recipe)
        assert per is None

    def test_rounding(self):
        recipe = {
            "nutrition_kcal_total": 1000,
            "nutrition_protein_g_total": 33,
            "nutrition_fat_g_total": 33,
            "nutrition_carbs_g_total": 33,
            "nutrition_servings_base": 3,
        }
        per = nutrition_per_serving(recipe)
        assert per["kcal"] == 333  # rounded
        assert per["protein_g"] == 11.0  # rounded to 1 decimal


class TestFormatNutritionShort:
    """Test short format string."""

    def test_full_format(self):
        recipe = {
            "nutrition_kcal_total": 1720,
            "nutrition_protein_g_total": 104,
            "nutrition_fat_g_total": 72,
            "nutrition_carbs_g_total": 168,
            "nutrition_servings_base": 4,
        }
        result = format_nutrition_short(recipe)
        assert "430 ккал" in result
        assert "Б 26.0г" in result
        assert "Ж 18.0г" in result
        assert "У 42.0г" in result

    def test_no_nutrition(self):
        recipe = {"name": "Test"}
        result = format_nutrition_short(recipe)
        assert result is None


class TestFormatNutritionDetails:
    """Test API response format."""

    def test_full_details(self):
        recipe = {
            "nutrition_kcal_total": 1720,
            "nutrition_protein_g_total": 104,
            "nutrition_fat_g_total": 72,
            "nutrition_carbs_g_total": 168,
            "nutrition_servings_base": 4,
            "nutrition_is_estimated": True,
            "nutrition_confidence": "medium",
            "nutrition_notes": "тест",
        }
        result = format_nutrition_details(recipe)
        assert result is not None
        assert result["estimated"] is True
        assert result["confidence"] == "medium"
        assert result["per_serving"]["kcal"] == 430
        assert result["total"]["kcal"] == 1720
        assert result["notes"] == "тест"

    def test_no_nutrition(self):
        recipe = {"name": "Test"}
        result = format_nutrition_details(recipe)
        assert result is None


class TestFormatNutritionCard:
    """Test Telegram card format."""

    def test_card_format(self):
        per = {
            "kcal": 430,
            "protein_g": 26.0,
            "fat_g": 18.0,
            "carbs_g": 42.0,
        }
        card = format_nutrition_card(per)
        assert "430" in card
        assert "ккал" in card
        assert "Б 26.0 г" in card
        assert "Ж 18.0 г" in card
        assert "У 42.0 г" in card
        assert "ПРИМЕРНО" in card
        assert "приблизительный" in card.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
