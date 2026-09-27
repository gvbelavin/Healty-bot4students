"""Проверка входа, каталога и готового плана."""

from __future__ import annotations

import json
import math
import re
from datetime import date
from pathlib import Path

from app.schemas import (
    EQUIPMENT,
    FORMATS,
    ID_RE,
    INTENSITIES,
    PLACES,
    SLOTS,
    fail,
    hhmm_to_sec,
    ok,
    parse_iso_date,
    sec_to_hhmm,
)

MAX_INPUT_BYTES = 16 * 1024
MAX_RESULT_BYTES = 32 * 1024
MAX_SESSION_BYTES = 16 * 1024


def json_size(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def portion_cost(quantity: int, pack_price: int, pack_quantity: int) -> int:
    return math.ceil(quantity * pack_price / pack_quantity)


def recipe_cost(recipe: dict, ingredients: dict[str, dict]) -> int:
    total = 0
    for item in recipe["ingredients"]:
        ingredient = ingredients[item["ingredient_id"]]
        total += portion_cost(item["quantity"], ingredient["pack_price_kopecks"], ingredient["pack_quantity"])
    return total


def _text_limit(value, limit: int, field: str) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return field
    return None


def load_catalog(manifest_path: Path, today: date) -> dict:
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent
    catalog = {"manifest": manifest}
    for key, filename_key in (
        ("ingredients", "ingredients_file"),
        ("recipes", "recipes_file"),
        ("activities", "activities_file"),
        ("workouts", "workouts_file"),
    ):
        catalog[key] = json.loads((root / manifest[filename_key]).read_text(encoding="utf-8"))
    errors = validate_catalog(catalog, today)
    if errors:
        raise ValueError("Каталог не прошёл проверку: " + "; ".join(errors))
    catalog["ingredients_by_id"] = {item["id"]: item for item in catalog["ingredients"]}
    catalog["recipes_by_id"] = {item["id"]: item for item in catalog["recipes"]}
    catalog["activities_by_id"] = {item["id"]: item for item in catalog["activities"]}
    catalog["workouts_by_id"] = {item["id"]: item for item in catalog["workouts"]}
    return catalog


def validate_catalog(catalog: dict, today: date) -> list[str]:
    errors: list[str] = []
    manifest = catalog.get("manifest") or {}
    version = manifest.get("version")
    if not isinstance(version, str) or not version or len(version) > 40:
        errors.append("manifest.version")

    def unique(items: list, label: str) -> dict[str, dict]:
        seen = {}
        for item in items:
            item_id = item.get("id")
            if not isinstance(item_id, str) or not ID_RE.fullmatch(item_id):
                errors.append(f"{label}.id")
                continue
            if item_id in seen:
                errors.append(f"duplicate:{item_id}")
            seen[item_id] = item
        return seen

    ingredients = unique(catalog.get("ingredients") or [], "ingredient")
    recipes = unique(catalog.get("recipes") or [], "recipe")
    activities = unique(catalog.get("activities") or [], "activity")
    workouts = unique(catalog.get("workouts") or [], "workout")
    if not 12 <= len(recipes) <= 30:
        errors.append("recipes.count")
    if not 6 <= len(activities) <= 12:
        errors.append("activities.count")
    if not 6 <= len(workouts) <= 12:
        errors.append("workouts.count")

    for ingredient in ingredients.values():
        if ingredient.get("unit") not in ("g", "ml", "piece"):
            errors.append(f"{ingredient['id']}.unit")
        for key in ("pack_price_kopecks", "pack_quantity"):
            if isinstance(ingredient.get(key), bool) or not isinstance(ingredient.get(key), int) or ingredient[key] <= 0:
                errors.append(f"{ingredient['id']}.{key}")
        price_date = parse_iso_date(ingredient.get("price_date", ""))
        if price_date is None or price_date > today:
            errors.append(f"{ingredient['id']}.price_date")
        if _text_limit(ingredient.get("name", ""), 80, "name"):
            errors.append(f"{ingredient['id']}.name")

    slot_counts = {slot: 0 for slot in SLOTS}
    for recipe in recipes.values():
        if recipe.get("slot") not in SLOTS:
            errors.append(f"{recipe['id']}.slot")
        else:
            slot_counts[recipe["slot"]] += 1
        if _text_limit(recipe.get("title", ""), 80, "title"):
            errors.append(f"{recipe['id']}.title")
        if not isinstance(recipe.get("cooking_min"), int) or recipe["cooking_min"] < 0:
            errors.append(f"{recipe['id']}.cooking_min")
        if not isinstance(recipe.get("convenience"), bool):
            errors.append(f"{recipe['id']}.convenience")
        kitchen = recipe.get("required_kitchen")
        if not isinstance(kitchen, list):
            errors.append(f"{recipe['id']}.kitchen")
        ingredients_used = recipe.get("ingredients")
        if not isinstance(ingredients_used, list) or not ingredients_used:
            errors.append(f"{recipe['id']}.ingredients")
        else:
            for piece in ingredients_used:
                if piece.get("ingredient_id") not in ingredients:
                    errors.append(f"{recipe['id']}.missing_ingredient")
                if isinstance(piece.get("quantity"), bool) or not isinstance(piece.get("quantity"), int) or piece["quantity"] <= 0:
                    errors.append(f"{recipe['id']}.quantity")
        steps = recipe.get("steps") or []
        if not 1 <= len(steps) <= 6 or any(_text_limit(step, 180, "step") for step in steps):
            errors.append(f"{recipe['id']}.steps")
        if _text_limit(recipe.get("tip", ""), 180, "tip"):
            errors.append(f"{recipe['id']}.tip")

    if any(slot_counts[slot] < 4 for slot in SLOTS):
        errors.append("recipes.per_slot")

    for activity in activities.values():
        if activity.get("format") not in ("indoors", "outdoors"):
            errors.append(f"{activity['id']}.format")
        duration = activity.get("duration_sec")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0 or duration % 60:
            errors.append(f"{activity['id']}.duration")
        if _text_limit(activity.get("title", ""), 80, "title") or _text_limit(activity.get("instruction", ""), 180, "instruction"):
            errors.append(f"{activity['id']}.text")

    for workout in workouts.values():
        if workout.get("intensity") not in INTENSITIES:
            errors.append(f"{workout['id']}.intensity")
        places = workout.get("places")
        if not isinstance(places, list) or not places or any(place not in PLACES for place in places):
            errors.append(f"{workout['id']}.places")
        equipment = workout.get("required_equipment")
        if not isinstance(equipment, list) or any(item not in EQUIPMENT for item in equipment):
            errors.append(f"{workout['id']}.equipment")
        exercises = workout.get("exercises") or []
        if not 1 <= len(exercises) <= 8:
            errors.append(f"{workout['id']}.exercises")
        total = 0
        for exercise in exercises:
            if _text_limit(exercise.get("name", ""), 80, "name") or _text_limit(exercise.get("instruction", ""), 180, "instruction"):
                errors.append(f"{workout['id']}.exercise_text")
            work = exercise.get("work_sec")
            rest = exercise.get("rest_sec")
            if isinstance(work, bool) or not isinstance(work, int) or work <= 0:
                errors.append(f"{workout['id']}.work")
            if isinstance(rest, bool) or not isinstance(rest, int) or rest < 0:
                errors.append(f"{workout['id']}.rest")
            if isinstance(work, int) and isinstance(rest, int):
                total += work + rest
        if total != workout.get("duration_sec") or not isinstance(total, int) or total <= 0 or total % 60:
            errors.append(f"{workout['id']}.duration")
        if _text_limit(workout.get("title", ""), 80, "title"):
            errors.append(f"{workout['id']}.title")
    return errors


def prices_fresh(recipe: dict, ingredients: dict[str, dict], plan_date: date) -> bool:
    for item in recipe["ingredients"]:
        price_date = date.fromisoformat(ingredients[item["ingredient_id"]]["price_date"])
        age = (plan_date - price_date).days
        if price_date > plan_date or age > 30:
            return False
    return True


def recipe_matches(recipe: dict, profile: dict, ingredients: dict[str, dict], plan_date: date) -> bool:
    used = {item["ingredient_id"] for item in recipe["ingredients"]}
    if not used <= set(profile["available_ingredients"]):
        return False
    if used & set(profile["excluded_ingredients"]):
        return False
    if not set(recipe["required_kitchen"]) <= set(profile["kitchen"]):
        return False
    if not profile["allow_convenience"] and recipe["convenience"]:
        return False
    return prices_fresh(recipe, ingredients, plan_date)


def open_windows(profile: dict, plan_date: date, now) -> list[dict]:
    from datetime import timedelta

    from app.schemas import local_date

    local = now + timedelta(minutes=profile["timezone_offset_min"])
    today = local_date(now, profile["timezone_offset_min"])
    now_sec = local.hour * 3600 + local.minute * 60
    if local.second or local.microsecond:
        now_sec += 60
    windows = []
    for item in profile["activity_windows"]:
        start = hhmm_to_sec(item["start"])
        end = hhmm_to_sec(item["end"])
        if plan_date == today:
            if end <= now_sec:
                continue
            start = max(start, now_sec)
        if end - start >= 60:
            windows.append({"start": start, "end": end})
    return windows


def postcheck_plan(plan: dict, profile: dict, catalog: dict) -> Result:
    meals = plan.get("nutrition", {}).get("meals")
    if not isinstance(meals, list) or len(meals) != 3:
        return fail("INVALID_RESULT", "В плане должно быть три приёма пищи.")
    if [meal.get("slot") for meal in meals] != list(SLOTS):
        return fail("INVALID_RESULT", "Приёмы пищи идут в порядке завтрак, обед, ужин.")
    ingredients = catalog["ingredients_by_id"]
    plan_date = date.fromisoformat(plan["plan_date"])
    cost = 0
    cooking = 0
    for meal in meals:
        recipe = catalog["recipes_by_id"].get(meal.get("recipe_id"))
        if recipe is None or not recipe_matches(recipe, profile, ingredients, plan_date):
            return fail("INVALID_RESULT", "Рецепт не соответствует ограничениям.")
        expected = recipe_cost(recipe, ingredients)
        if meal.get("cost_kopecks") != expected or meal.get("cooking_min") != recipe["cooking_min"]:
            return fail("INVALID_RESULT", "Стоимость или время рецепта рассчитаны неверно.")
        cost += expected
        cooking += recipe["cooking_min"]
        lines = meal.get("ingredients") or []
        if not 1 <= len(lines) <= 15 or any(not isinstance(line, str) or len(line) > 120 for line in lines):
            return fail("INVALID_RESULT", "Список ингредиентов не проходит ограничение.")
    if cost != plan["nutrition"].get("total_cost_kopecks") or cooking != plan["nutrition"].get("total_cooking_min"):
        return fail("INVALID_RESULT", "Итоги питания не сходятся.")
    if cost > profile["budget_rub_day"] * 100 or cooking > profile["cooking_minutes_day"]:
        return fail("INVALID_RESULT", "План превышает бюджет или время готовки.")

    workout = plan.get("workout") or {}
    template = catalog["workouts_by_id"].get(workout.get("template_id"))
    if template is None:
        return fail("INVALID_RESULT", "Тренировка отсутствует в каталоге.")
    exercise_total = sum(item["work_sec"] + item["rest_sec"] for item in workout.get("exercises") or [])
    if exercise_total != workout.get("duration_sec") or workout["duration_sec"] != template["duration_sec"]:
        return fail("INVALID_RESULT", "Длительность тренировки не сходится.")

    items = plan.get("activity", {}).get("items") or []
    if not 1 <= len(items) <= 6:
        return fail("INVALID_RESULT", "В расписании должно быть от 1 до 6 пунктов.")
    workout_items = [item for item in items if item.get("kind") == "workout"]
    if len(workout_items) != 1 or workout_items[0].get("id") != template["id"]:
        return fail("INVALID_RESULT", "В расписании должна быть ровно одна выбранная тренировка.")
    if workout_items[0].get("duration_sec") != template["duration_sec"]:
        return fail("INVALID_RESULT", "Длительность тренировки в расписании не совпадает.")
    total = 0
    parsed = []
    for item in items:
        start = hhmm_to_sec(item.get("start", ""))
        end = hhmm_to_sec(item.get("end", ""))
        if start is None or end is None or end - start != item.get("duration_sec"):
            return fail("INVALID_RESULT", "Границы интервала не равны длительности.")
        parsed.append((start, end, item))
        total += item["duration_sec"]
    if total != plan["activity"].get("total_sec"):
        return fail("INVALID_RESULT", "Общее время расписания не сходится.")
    if total > profile["activity_minutes_day"] * 60:
        return fail("INVALID_RESULT", "Расписание превышает лимит активности.")
    parsed.sort(key=lambda row: row[0])
    for prev, nxt in zip(parsed, parsed[1:]):
        if prev[1] > nxt[0]:
            return fail("INVALID_RESULT", "Пункты расписания пересекаются.")
    windows = [(hhmm_to_sec(item["start"]), hhmm_to_sec(item["end"])) for item in profile["activity_windows"]]
    for start, end, _item in parsed:
        if not any(start >= win_start and end <= win_end for win_start, win_end in windows):
            return fail("INVALID_RESULT", "Пункт расписания выходит за свободное окно.")
    steps = plan.get("steps") or []
    warnings = plan.get("warnings") or []
    if not 1 <= len(steps) <= 8 or any(not isinstance(step, str) or len(step) > 180 for step in steps):
        return fail("INVALID_RESULT", "Сводный маршрут не проходит ограничение.")
    if len(warnings) > 4 or any(not isinstance(item, str) or len(item) > 180 for item in warnings):
        return fail("INVALID_RESULT", "Пояснения не проходят ограничение.")
    if json_size(plan) > MAX_RESULT_BYTES:
        return fail("INVALID_RESULT", "Результат превышает допустимый размер.")
    return ok(plan)


def looks_medical(text: str) -> bool:
    lowered = text.lower().replace("ё", "е")
    return bool(re.search(r"диабет|аллерг|диагноз|лекар|врач|беремен|давлен|боле", lowered))
