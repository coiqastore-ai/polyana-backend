"""
Nutrition validation and formatting module for ПОЛЯНА.
"""
import logging

log = logging.getLogger("polyana.nutrition")

# Validation bounds
MAX_KCAL = 30000
MAX_PROTEIN = 3000
MAX_FAT = 3000
MAX_CARBS = 5000
MIN_SERVINGS = 1
MAX_SERVINGS = 100

# Macros to kcal conversion
PROTEIN_KCAL_PER_G = 4
FAT_KCAL_PER_G = 9
CARBS_KCAL_PER_G = 4

# Tolerance for kcal vs macros consistency check
KCAL_TOLERANCE = 0.35  # 35%


def validate_nutrition(data: dict, servings: int | float = 1) -> dict:
    """
    Validate nutrition data from AI generation.
    
    Returns:
        dict with keys:
            - valid: bool
            - errors: list of error strings
            - warnings: list of warning strings
            - confidence: str (low/medium/high)
            - notes: list of assumption strings
    """
    errors = []
    warnings = []
    notes = []

    if not data or not isinstance(data, dict):
        return {
            "valid": False,
            "errors": ["No nutrition data provided"],
            "warnings": [],
            "confidence": "low",
            "notes": [],
        }

    confidence = data.get("confidence", "medium")
    
    # Extract values
    kcal = data.get("total_kcal")
    protein = data.get("total_protein_g")
    fat = data.get("total_fat_g")
    carbs = data.get("total_carbs_g")
    
    # Check required fields
    if kcal is None:
        errors.append("total_kcal is missing")
    if protein is None:
        errors.append("total_protein_g is missing")
    if fat is None:
        errors.append("total_fat_g is missing")
    if carbs is None:
        errors.append("total_carbs_g is missing")
    
    if errors:
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "confidence": "low",
            "notes": notes,
        }
    
    # Type checks - must be numbers, not strings
    for name, val in [("total_kcal", kcal), ("total_protein_g", protein), 
                       ("total_fat_g", fat), ("total_carbs_g", carbs)]:
        if not isinstance(val, (int, float)):
            errors.append(f"{name} must be a number, got {type(val).__name__}")
        elif val < 0:
            errors.append(f"{name} cannot be negative, got {val}")
    
    if errors:
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "confidence": "low",
            "notes": notes,
        }
    
    # Range checks
    if kcal > MAX_KCAL:
        errors.append(f"total_kcal ({kcal}) exceeds maximum ({MAX_KCAL})")
    if protein > MAX_PROTEIN:
        errors.append(f"total_protein_g ({protein}) exceeds maximum ({MAX_PROTEIN})")
    if fat > MAX_FAT:
        errors.append(f"total_fat_g ({fat}) exceeds maximum ({MAX_FAT})")
    if carbs > MAX_CARBS:
        errors.append(f"total_carbs_g ({carbs}) exceeds maximum ({MAX_CARBS})")
    
    # Servings check
    if servings < MIN_SERVINGS or servings > MAX_SERVINGS:
        errors.append(f"servings ({servings}) must be between {MIN_SERVINGS} and {MAX_SERVINGS}")
    
    if errors:
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "confidence": "low",
            "notes": notes,
        }
    
    # Energy consistency check
    macro_kcal = protein * PROTEIN_KCAL_PER_G + fat * FAT_KCAL_PER_G + carbs * CARBS_KCAL_PER_G
    
    if kcal > 0:
        diff_ratio = abs(kcal - macro_kcal) / kcal
        if diff_ratio > KCAL_TOLERANCE:
            warnings.append(
                f"Energy inconsistency: stated {kcal:.0f} kcal vs calculated {macro_kcal:.0f} kcal "
                f"from macros (diff {diff_ratio*100:.0f}%)"
            )
            confidence = "low"
    
    # Extract assumptions/notes
    assumptions = data.get("assumptions", [])
    if isinstance(assumptions, list):
        notes = [str(a) for a in assumptions[:3]]  # Max 3 notes
    
    return {
        "valid": True,
        "errors": [],
        "warnings": warnings,
        "confidence": confidence,
        "notes": notes,
    }


def nutrition_per_serving(recipe: dict) -> dict | None:
    """
    Calculate per-serving nutrition from a recipe's total nutrition.
    
    Args:
        recipe: dict with nutrition_kcal_total, nutrition_protein_g_total, etc.
    
    Returns:
        dict with per_serving values, or None if nutrition not available.
    """
    total_kcal = recipe.get("nutrition_kcal_total")
    total_protein = recipe.get("nutrition_protein_g_total")
    total_fat = recipe.get("nutrition_fat_g_total")
    total_carbs = recipe.get("nutrition_carbs_g_total")
    servings_base = recipe.get("nutrition_servings_base")
    
    if total_kcal is None or servings_base is None or servings_base <= 0:
        return None
    
    return {
        "kcal": round(total_kcal / servings_base),
        "protein_g": round(total_protein / servings_base, 1) if total_protein is not None else None,
        "fat_g": round(total_fat / servings_base, 1) if total_fat is not None else None,
        "carbs_g": round(total_carbs / servings_base, 1) if total_carbs is not None else None,
    }


def format_nutrition_short(recipe: dict) -> str | None:
    """
    Format nutrition as a short one-line string for display.
    Example: "430 ккал | Б 26г Ж 18г У 42г"
    """
    per = nutrition_per_serving(recipe)
    if not per:
        return None
    
    parts = [f"{per['kcal']} ккал"]
    if per.get("protein_g") is not None:
        parts.append(f"Б {per['protein_g']}г")
    if per.get("fat_g") is not None:
        parts.append(f"Ж {per['fat_g']}г")
    if per.get("carbs_g") is not None:
        parts.append(f"У {per['carbs_g']}г")
    
    return " | ".join(parts)


def format_nutrition_details(recipe: dict) -> dict | None:
    """
    Format nutrition as a structured dict for API responses.
    
    Returns:
        dict matching the API response format, or None.
    """
    per = nutrition_per_serving(recipe)
    if not per:
        return None
    
    result = {
        "estimated": recipe.get("nutrition_is_estimated", True),
        "confidence": recipe.get("nutrition_confidence", "low"),
        "per_serving": per,
        "total": {
            "kcal": recipe.get("nutrition_kcal_total"),
            "protein_g": recipe.get("nutrition_protein_g_total"),
            "fat_g": recipe.get("nutrition_fat_g_total"),
            "carbs_g": recipe.get("nutrition_carbs_g_total"),
        },
    }
    
    notes = recipe.get("nutrition_notes")
    if notes:
        result["notes"] = notes
    
    return result


def format_nutrition_card(per_serving: dict) -> str:
    """
    Format a visual nutrition card for Telegram display.
    """
    kcal = per_serving.get("kcal", 0)
    protein = per_serving.get("protein_g", 0)
    fat = per_serving.get("fat_g", 0)
    carbs = per_serving.get("carbs_g", 0)
    
    return (
        f"<b>ПРИМЕРНО НА 1 ПОРЦИЮ</b>\n\n"
        f"<b>{kcal}</b>\n"
        f"<i>ккал</i>\n\n"
        f"Б {protein} г   Ж {fat} г   У {carbs} г\n\n"
        f"<i>Расчёт приблизительный. Фактические значения зависят "
        f"от продуктов, их марки и способа приготовления.</i>"
    )
