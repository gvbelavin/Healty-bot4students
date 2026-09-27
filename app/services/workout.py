"""Подбор мини-тренировки из каталога."""

from __future__ import annotations

from app.schemas import fail, ok


def _format_allows(template: dict, activity_format: str) -> bool:
    places = set(template["places"])
    if activity_format == "indoors":
        return bool(places & {"home", "dorm"})
    if activity_format == "outdoors":
        return "outdoors" in places
    return True


def generate_workout(profile: dict, catalog: dict, windows: list[dict]):
    limit = profile["activity_minutes_day"] * 60
    chosen = []
    for template in catalog["workouts"]:
        if profile["workout_place"] not in template["places"]:
            continue
        if not _format_allows(template, profile["activity_format"]):
            continue
        if not set(template["required_equipment"]) <= set(profile["equipment"]):
            continue
        if template["intensity"] != profile["intensity"]:
            continue
        duration = template["duration_sec"]
        if duration > limit:
            continue
        if not any(item["end"] - item["start"] >= duration for item in windows):
            continue
        chosen.append(template)
    if not chosen:
        return fail("NO_MATCH", "Нет полного плана для этих ограничений.")
    chosen.sort(key=lambda item: (-item["duration_sec"], item["id"]))
    return ok({"candidates": chosen})
