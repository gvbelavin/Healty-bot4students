"""Сборка единого дневного плана и постпроверка."""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.formatting import rubles
from app.schemas import SCHEMA_VERSION, fail, ok, plan_signature
from app.services.activity import generate_activity
from app.services.nutrition import generate_nutrition
from app.services.workout import generate_workout
from app.validation import json_size, open_windows, postcheck_plan, recipe_cost

UNIT_TITLE = {"g": "г", "ml": "мл", "piece": "шт."}


def _ingredient_line(recipe: dict, ingredients: dict[str, dict]) -> list[str]:
    lines = []
    for item in recipe["ingredients"]:
        ingredient = ingredients[item["ingredient_id"]]
        lines.append(f"{ingredient['name']} — {item['quantity']} {UNIT_TITLE[ingredient['unit']]}")
    return lines


def _meal(recipe: dict, ingredients: dict[str, dict]) -> dict:
    return {
        "slot": recipe["slot"],
        "recipe_id": recipe["id"],
        "title": recipe["title"],
        "ingredients": _ingredient_line(recipe, ingredients),
        "steps": list(recipe["steps"]),
        "cost_kopecks": recipe_cost(recipe, ingredients),
        "cooking_min": recipe["cooking_min"],
        "tip": recipe["tip"],
    }


def _workout_payload(template: dict) -> dict:
    return {
        "template_id": template["id"],
        "title": template["title"],
        "duration_sec": template["duration_sec"],
        "exercises": [
            {
                "name": item["name"],
                "work_sec": item["work_sec"],
                "rest_sec": item["rest_sec"],
                "instruction": item["instruction"],
            }
            for item in template["exercises"]
        ],
    }


def _steps(meals: list[dict], activity: dict) -> list[str]:
    titles = {meal["slot"]: meal["title"] for meal in meals}
    lines = [
        f"{titles['breakfast']}, {titles['lunch']} и {titles['dinner']} — по рецептам ниже."
    ]
    for item in activity["items"]:
        lines.append(f"{item['start']}–{item['end']} — {item['title']}.")
    return lines


def build_daily_plan(request: dict, catalog: dict, now: datetime | None = None, deadline: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    if deadline is not None and datetime.now(timezone.utc) > deadline:
        return fail("TIMEOUT", "Не удалось завершить план. Повторите запрос.", retryable=True)

    profile = request["profile"]
    plan_date = date.fromisoformat(request["plan_date"])
    ingredients = catalog["ingredients_by_id"]
    nutrition = generate_nutrition(profile, catalog, plan_date)
    if not nutrition.ok:
        return nutrition
    windows = open_windows(profile, plan_date, now)
    if not windows:
        return fail("NO_MATCH", "На сегодня свободных окон уже нет. Выберите завтра.")
    workouts = generate_workout(profile, catalog, windows)
    if not workouts.ok:
        return workouts

    excluded = request.get("exclude_signature")
    saw_excluded = False
    for combo in nutrition.data["candidates"]:
        if deadline is not None and datetime.now(timezone.utc) > deadline:
            return fail("TIMEOUT", "Не удалось завершить план. Повторите запрос.", retryable=True)
        meals = [_meal(recipe, ingredients) for recipe in combo]
        for template in workouts.data["candidates"]:
            schedule = generate_activity(profile, template, catalog, windows)
            if not schedule.ok:
                continue
            workout = _workout_payload(template)
            activity = schedule.data
            plan = {
                "schema_version": SCHEMA_VERSION,
                "plan_date": request["plan_date"],
                "timezone_offset_min": profile["timezone_offset_min"],
                "catalog_version": catalog["manifest"]["version"],
                "profile_revision": request["profile_revision"],
                "nutrition": {
                    "meals": meals,
                    "total_cost_kopecks": sum(meal["cost_kopecks"] for meal in meals),
                    "total_cooking_min": sum(meal["cooking_min"] for meal in meals),
                },
                "workout": workout,
                "activity": activity,
                "steps": _steps(meals, activity),
                "warnings": [],
            }
            plan["warnings"] = [
                f"{rubles(plan['nutrition']['total_cost_kopecks'])}. — оценка стоимости порций, не целых упаковок.",
                "План не является медицинской рекомендацией.",
            ]
            signature = plan_signature(plan)
            if excluded and signature == excluded:
                saw_excluded = True
                continue
            checked = postcheck_plan(plan, profile, catalog)
            if not checked.ok:
                return fail("INVALID_RESULT", "Сейчас не удаётся составить корректный план.")
            if json_size(plan) > 32 * 1024:
                return fail("INVALID_RESULT", "Сейчас не удаётся составить корректный план.")
            return ok(plan)
    if saw_excluded:
        return fail("NO_ALTERNATIVE", "Другого подходящего варианта пока нет.")
    return fail("NO_MATCH", "Нет полного плана для этих ограничений.")
