"""Размещение тренировки и коротких активностей в окнах."""

from __future__ import annotations

from app.schemas import fail, ok, sec_to_hhmm


def _activity_allowed(card: dict, activity_format: str) -> bool:
    if activity_format == "mixed":
        return True
    return card["format"] == activity_format


def generate_activity(profile: dict, workout: dict, catalog: dict, windows: list[dict]):
    duration = workout["duration_sec"]
    host = None
    for window in sorted(windows, key=lambda item: item["start"]):
        if window["end"] - window["start"] >= duration:
            host = window
            break
    if host is None:
        return fail("NO_MATCH", "Нет полного плана для этих ограничений.")

    workout_start = host["start"]
    workout_end = workout_start + duration
    fragments = []
    for window in windows:
        if window is host:
            if workout_end < window["end"]:
                fragments.append({"start": workout_end, "end": window["end"]})
        else:
            fragments.append(dict(window))
    fragments.sort(key=lambda item: item["start"])

    remaining = profile["activity_minutes_day"] * 60 - duration
    cards = [
        card
        for card in catalog["activities"]
        if _activity_allowed(card, profile["activity_format"])
    ]
    cards.sort(key=lambda item: (-item["duration_sec"], item["id"]))
    placed = []
    for card in cards:
        if len(placed) >= 5 or card["duration_sec"] > remaining:
            continue
        for fragment in fragments:
            free = fragment["end"] - fragment["start"]
            if free >= card["duration_sec"]:
                start = fragment["start"]
                end = start + card["duration_sec"]
                placed.append((start, end, card))
                fragment["start"] = end
                remaining -= card["duration_sec"]
                break

    items = [
        {
            "id": workout["id"],
            "kind": "workout",
            "title": workout["title"],
            "instruction": "См. последовательность упражнений.",
            "start": sec_to_hhmm(workout_start),
            "end": sec_to_hhmm(workout_end),
            "duration_sec": duration,
        }
    ]
    for start, end, card in placed:
        items.append(
            {
                "id": card["id"],
                "kind": "activity",
                "title": card["title"],
                "instruction": card["instruction"],
                "start": sec_to_hhmm(start),
                "end": sec_to_hhmm(end),
                "duration_sec": card["duration_sec"],
            }
        )
    items.sort(key=lambda item: item["start"])
    return ok({"items": items, "total_sec": sum(item["duration_sec"] for item in items)})
