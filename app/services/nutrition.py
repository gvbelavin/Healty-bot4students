"""Подбор трёх рецептов на день."""

from __future__ import annotations

from datetime import date

from app.schemas import SLOTS, fail, ok
from app.validation import recipe_cost, recipe_matches


def generate_nutrition(profile: dict, catalog: dict, plan_date: date):
    ingredients = catalog["ingredients_by_id"]
    grouped = {slot: [] for slot in SLOTS}
    for recipe in catalog["recipes"]:
        if recipe_matches(recipe, profile, ingredients, plan_date):
            grouped[recipe["slot"]].append(recipe)
    if any(not grouped[slot] for slot in SLOTS):
        return fail("NO_MATCH", "Нет полного плана для этих ограничений.")

    combos = []
    for breakfast in grouped["breakfast"]:
        for lunch in grouped["lunch"]:
            for dinner in grouped["dinner"]:
                chosen = (breakfast, lunch, dinner)
                cost = sum(recipe_cost(recipe, ingredients) for recipe in chosen)
                cooking = sum(recipe["cooking_min"] for recipe in chosen)
                if cooking > profile["cooking_minutes_day"] or cost > profile["budget_rub_day"] * 100:
                    continue
                combos.append((cost, cooking, tuple(recipe["id"] for recipe in chosen), chosen))
    if not combos:
        return fail("NO_MATCH", "Нет полного плана для этих ограничений.")
    combos.sort(key=lambda item: (item[0], item[1], item[2]))
    return ok({"candidates": [item[3] for item in combos]})
