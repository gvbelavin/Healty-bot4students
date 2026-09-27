import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.dialogue import on_text
from app.repositories import Repository
from app.schemas import fail, plan_signature, validate_profile
from app.services.orchestrator import build_daily_plan
from app.validation import load_catalog, recipe_matches
from app.worker import admission

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)
CATALOG = load_catalog(ROOT / "catalog" / "manifest.json", date(2026, 9, 27))


def profile(**overrides):
    base = {
        "budget_rub_day": 600,
        "cooking_minutes_day": 40,
        "kitchen": ["fridge", "kettle", "microwave", "stove"],
        "available_ingredients": [
            "apple", "bread", "buckwheat", "cottage", "cucumber", "egg", "milk",
            "oats", "oil", "onion", "potato", "tuna", "vegetables", "yogurt",
        ],
        "excluded_ingredients": [],
        "allow_convenience": False,
        "activity_minutes_day": 15,
        "activity_windows": [{"start": "18:00", "end": "18:30"}],
        "activity_format": "indoors",
        "workout_place": "dorm",
        "equipment": [],
        "intensity": "light",
        "timezone_offset_min": 420,
    }
    base.update(overrides)
    checked = validate_profile(base, set(CATALOG["ingredients_by_id"]))
    assert checked.ok, checked.error
    return checked.data


def test_budget_edges():
    ids = set(CATALOG["ingredients_by_id"])
    raw = profile()
    assert validate_profile({**raw, "budget_rub_day": 100}, ids).ok
    assert validate_profile({**raw, "budget_rub_day": 3000}, ids).ok
    assert validate_profile({**raw, "budget_rub_day": 99}, ids).error.field == "budget_rub_day"
    assert validate_profile({**raw, "budget_rub_day": 3001}, ids).error.field == "budget_rub_day"
    assert validate_profile({**raw, "cooking_minutes_day": -1}, ids).error.field == "cooking_minutes_day"
    assert validate_profile({**raw, "activity_windows": [{"start": "18:00", "end": "18:10"}]}, ids).error.field == "activity_windows"


def test_dialogue_keeps_step_on_bad_budget():
    view = on_text("BUDGET", {"mode": "create", "draft": {}, "page": 0, "select": []}, "99", CATALOG)
    assert view.step == "BUDGET"
    view = on_text("BUDGET", {"mode": "create", "draft": {}, "page": 0, "select": []}, "600", CATALOG)
    assert view.step == "COOKING"
    assert view.temporary["draft"]["budget_rub_day"] == 600
    weekly = on_text("BUDGET", {"mode": "create", "draft": {}, "page": 0, "select": []}, "До 3000 в неделю", CATALOG)
    assert weekly.step == "BUDGET"


def test_stale_price_and_exclusion_are_not_selected():
    sample = profile()
    old = dict(CATALOG["ingredients_by_id"]["oats"])
    ingredients = dict(CATALOG["ingredients_by_id"])
    ingredients["oats"] = {**old, "price_date": "2026-01-01"}
    recipe = CATALOG["recipes_by_id"]["b_yogurt"]
    assert not recipe_matches(recipe, sample, ingredients, date(2026, 9, 27))
    excluded = profile(excluded_ingredients=["yogurt"], available_ingredients=["oats", "apple", "bread", "buckwheat", "vegetables", "oil", "cottage"])
    assert not recipe_matches(recipe, excluded, CATALOG["ingredients_by_id"], date(2026, 9, 27))


def test_at05_schedule_and_full_plan():
    request = {"profile": profile(), "profile_revision": 1, "plan_date": "2026-09-27", "exclude_signature": None}
    result = build_daily_plan(request, CATALOG, NOW)
    assert result.ok, result.error
    plan = result.data
    workout_items = [item for item in plan["activity"]["items"] if item["kind"] == "workout"]
    assert len(workout_items) == 1
    assert workout_items[0]["duration_sec"] == 300
    assert plan["activity"]["total_sec"] == 900
    assert plan["nutrition"]["total_cost_kopecks"] <= 60000
    assert plan["nutrition"]["total_cooking_min"] <= 40
    assert [meal["slot"] for meal in plan["nutrition"]["meals"]] == ["breakfast", "lunch", "dinner"]
    starts = [item["start"] for item in plan["activity"]["items"]]
    assert starts == sorted(starts)


def test_other_plan_skips_same_signature_or_reports_no_alternative():
    request = {"profile": profile(), "profile_revision": 1, "plan_date": "2026-09-27", "exclude_signature": None}
    first = build_daily_plan(request, CATALOG, NOW)
    assert first.ok
    second = build_daily_plan({**request, "exclude_signature": plan_signature(first.data)}, CATALOG, NOW)
    assert second.ok or second.error.code == "NO_ALTERNATIVE"
    if second.ok:
        assert plan_signature(second.data) != plan_signature(first.data)


def test_no_match_when_products_cannot_make_three_meals():
    narrow = profile(
        available_ingredients=["tea"],
        kitchen=[],
        cooking_minutes_day=0,
        budget_rub_day=3000,
    )
    result = build_daily_plan(
        {"profile": narrow, "profile_revision": 1, "plan_date": "2026-09-27", "exclude_signature": None},
        CATALOG,
        NOW,
    )
    assert result.error.code == "NO_MATCH"


def test_single_option_returns_no_alternative(tmp_path):
    catalog = {
        "manifest": {"version": "mini"},
        "ingredients_by_id": CATALOG["ingredients_by_id"],
        "recipes_by_id": {},
        "recipes": [
            CATALOG["recipes_by_id"]["b_yogurt"],
            CATALOG["recipes_by_id"]["l_sandwich"],
            CATALOG["recipes_by_id"]["d_cottage"],
        ],
        "activities": [CATALOG["activities_by_id"]["a_walk_in"]],
        "activities_by_id": {"a_walk_in": CATALOG["activities_by_id"]["a_walk_in"]},
        "workouts": [CATALOG["workouts_by_id"]["w_indoor_light_5"]],
        "workouts_by_id": {"w_indoor_light_5": CATALOG["workouts_by_id"]["w_indoor_light_5"]},
    }
    for recipe in catalog["recipes"]:
        catalog["recipes_by_id"][recipe["id"]] = recipe
    raw = profile(
        available_ingredients=["yogurt", "apple", "oats", "bread", "tuna", "cucumber", "cottage"],
        kitchen=[],
        cooking_minutes_day=0,
        budget_rub_day=3000,
    )
    request = {"profile": raw, "profile_revision": 1, "plan_date": "2026-09-27", "exclude_signature": None}
    first = build_daily_plan(request, catalog, NOW)
    assert first.ok, first.error
    # 15 minutes cannot hold a 300s workout and a 600s walk, so shrink the activity card.
    catalog["activities"][0] = {**catalog["activities"][0], "duration_sec": 300}
    catalog["activities_by_id"]["a_walk_in"] = catalog["activities"][0]
    first = build_daily_plan(request, catalog, NOW)
    assert first.ok, first.error
    again = build_daily_plan({**request, "exclude_signature": plan_signature(first.data)}, catalog, NOW)
    assert again.error.code == "NO_ALTERNATIVE"


def test_repository_cascade_and_single_active_job(tmp_path):
    repo = Repository(tmp_path / "app.sqlite3", tmp_path / "backups")
    repo.get_or_create_user(10)
    saved = repo.save_profile(10, profile())
    assert saved["revision"] == 1
    request = {"profile": saved["data"], "profile_revision": 1, "plan_date": "2026-09-27", "exclude_signature": None}
    created = repo.create_plan(10, 1, request)
    assert "id" in created
    blocked = repo.create_plan(10, 2, request)
    assert blocked["error"] == "BUSY"
    repo.mark_running(created["id"])
    assert repo.fail_interrupted() == 1
    assert repo.get_plan(10, created["id"])["status"] == "failed"
    assert repo.get_plan(10, created["id"])["error_code"] == "RESTARTED"
    repo.save_session(10, "BUDGET", {"draft": {}})
    removed = asyncio.run(_delete(repo))
    assert removed.ok
    assert repo.get_profile(10) is None
    assert repo.get_session(10) is None


async def _delete(repo):
    return repo.delete_user_data(10)


def test_admission_limits():
    settings = {"user_jobs_per_24h": 10, "user_cooldown_sec": 30, "queue_capacity": 10}
    now = datetime(2026, 9, 27, tzinfo=timezone.utc)
    assert admission(True, 0, None, 0, settings, now)[0] == "BUSY"
    assert admission(False, 10, None, 0, settings, now)[0] == "DAILY_LIMIT"
    code, seconds = admission(False, 0, now - timedelta(seconds=10), 0, settings, now)
    assert code == "RATE_LIMIT" and seconds == 20
    assert admission(False, 0, None, 10, settings, now)[0] == "BUSY"
    assert admission(False, 0, None, 9, settings, now)[0] == "OK"


def test_unknown_ingredient_rejected():
    raw = profile()
    raw["available_ingredients"] = ["not_a_product"]
    assert validate_profile(raw, set(CATALOG["ingredients_by_id"])).error.code == "VALIDATION"
