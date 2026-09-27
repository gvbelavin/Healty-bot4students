"""Контракты Profile, BuildRequest, DailyPlan и Result."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

SCHEMA_VERSION = 1
CONSENT_VERSION = "1"
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HHMM_RE = re.compile(r"^(\d{2}):(\d{2})$")
WINDOW_RE = re.compile(r"^(\d{2}):(\d{2})\s*-\s*(\d{2}):(\d{2})$")

KITCHEN = ("kettle", "microwave", "stove", "fridge")
EQUIPMENT = ("mat", "chair")
FORMATS = ("indoors", "outdoors", "mixed")
PLACES = ("home", "dorm", "outdoors")
INTENSITIES = ("light", "moderate")
SLOTS = ("breakfast", "lunch", "dinner")
MEAL_ORDER = {"breakfast": 0, "lunch": 1, "dinner": 2}

PROFILE_FIELDS = (
    "budget_rub_day",
    "cooking_minutes_day",
    "kitchen",
    "available_ingredients",
    "excluded_ingredients",
    "allow_convenience",
    "activity_minutes_day",
    "activity_windows",
    "activity_format",
    "workout_place",
    "equipment",
    "intensity",
    "timezone_offset_min",
)


@dataclass
class AppError:
    code: str
    message: str
    field: str | None = None
    retryable: bool = False

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "retryable": self.retryable,
        }


@dataclass
class Result:
    ok: bool
    data: dict | None = None
    error: AppError | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "data": self.data,
            "error": None if self.error is None else self.error.as_dict(),
        }


def ok(data: dict) -> Result:
    return Result(True, data, None)


def fail(code: str, message: str, field: str | None = None, retryable: bool = False) -> Result:
    return Result(False, None, AppError(code, message, field, retryable))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_stamp(moment: datetime | None = None) -> str:
    moment = moment or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def hhmm_to_sec(value: str) -> int | None:
    match = HHMM_RE.fullmatch(value or "")
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 3600 + minute * 60


def sec_to_hhmm(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def local_date(moment: datetime, offset_min: int) -> date:
    return (moment.astimezone(timezone.utc) + timedelta(minutes=offset_min)).date()


def parse_iso_date(value: str) -> date | None:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def place_matches_format(place: str, activity_format: str) -> bool:
    if place in ("home", "dorm"):
        return activity_format in ("indoors", "mixed")
    if place == "outdoors":
        return activity_format in ("outdoors", "mixed")
    return False


def parse_minutes_text(text: str) -> int | None:
    cleaned = text.strip().lower().replace("ё", "е")
    cleaned = re.sub(r"(минут|мин|руб(лей|ля)?|₽)\s*$", "", cleaned).strip()
    if not re.fullmatch(r"-?\d+", cleaned):
        return None
    return int(cleaned)


def parse_windows_text(text: str) -> tuple[list[dict] | None, str | None]:
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not 1 <= len(parts) <= 3:
        return None, "Нужно от 1 до 3 интервалов через запятую, например 18:00-18:30."
    windows = []
    for part in parts:
        match = WINDOW_RE.fullmatch(part)
        if not match:
            return None, "Интервал записывается как ЧЧ:ММ-ЧЧ:ММ."
        start = hhmm_to_sec(f"{match.group(1)}:{match.group(2)}")
        end = hhmm_to_sec(f"{match.group(3)}:{match.group(4)}")
        if start is None or end is None or start >= end:
            return None, "Начало интервала должно быть раньше конца, без перехода через полночь."
        windows.append({"start": sec_to_hhmm(start), "end": sec_to_hhmm(end)})
    ordered = sorted(windows, key=lambda item: item["start"])
    for prev, nxt in zip(ordered, ordered[1:]):
        if hhmm_to_sec(prev["end"]) > hhmm_to_sec(nxt["start"]):
            return None, "Интервалы не должны пересекаться."
    return windows, None


def window_minutes(windows: list[dict]) -> int:
    total = 0
    for item in windows:
        total += (hhmm_to_sec(item["end"]) - hhmm_to_sec(item["start"])) // 60
    return total


def parse_timezone_text(text: str) -> int | None:
    cleaned = text.strip().upper().replace("UTC", "").replace(" ", "")
    match = re.fullmatch(r"([+-])(\d{1,2})(?::?(\d{2}))?", cleaned)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)
        if hours > 14 or minutes > 59:
            return None
        offset = sign * (hours * 60 + minutes)
    elif re.fullmatch(r"[+-]?\d+", cleaned):
        offset = int(cleaned)
    else:
        return None
    if offset < -720 or offset > 840 or offset % 15 != 0:
        return None
    return offset


def format_offset(offset_min: int) -> str:
    sign = "+" if offset_min >= 0 else "-"
    absolute = abs(offset_min)
    return f"UTC{sign}{absolute // 60:02d}:{absolute % 60:02d}"


def _unique_ids(values, field: str, minimum: int, maximum: int, known: set[str] | None) -> AppError | None:
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        return AppError("VALIDATION", "Нужен список идентификаторов.", field)
    if len(values) != len(set(values)):
        return AppError("VALIDATION", "Повторяющиеся значения не допускаются.", field)
    if not minimum <= len(values) <= maximum:
        return AppError("VALIDATION", f"Допустимо от {minimum} до {maximum} значений.", field)
    for item in values:
        if not ID_RE.fullmatch(item):
            return AppError("VALIDATION", "Неизвестный идентификатор.", field)
        if known is not None and item not in known:
            return AppError("VALIDATION", "Такого продукта нет в каталоге.", field)
    return None


def validate_profile(raw: dict, ingredient_ids: set[str] | None = None) -> Result:
    if not isinstance(raw, dict):
        return fail("VALIDATION", "Профиль должен быть объектом.", "profile")
    unknown = set(raw) - set(PROFILE_FIELDS)
    if unknown:
        return fail("VALIDATION", "В профиле есть неизвестное поле.", sorted(unknown)[0])

    for name in PROFILE_FIELDS:
        if name not in raw:
            return fail("VALIDATION", "Заполните обязательное поле.", name)

    budget = raw["budget_rub_day"]
    cooking = raw["cooking_minutes_day"]
    activity = raw["activity_minutes_day"]
    offset = raw["timezone_offset_min"]
    for name, value, low, high in (
        ("budget_rub_day", budget, 100, 3000),
        ("cooking_minutes_day", cooking, 0, 120),
        ("activity_minutes_day", activity, 5, 120),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            return fail("VALIDATION", "Нужно целое число.", name)
        if not low <= value <= high:
            return fail("VALIDATION", f"Допустимый диапазон: {low}–{high}.", name)
    if isinstance(offset, bool) or not isinstance(offset, int):
        return fail("VALIDATION", "Нужно целое смещение UTC в минутах.", "timezone_offset_min")
    if offset < -720 or offset > 840 or offset % 15 != 0:
        return fail("VALIDATION", "Смещение UTC должно быть кратно 15 минутам, от −12:00 до +14:00.", "timezone_offset_min")

    if not isinstance(raw["allow_convenience"], bool):
        return fail("VALIDATION", "Ответ про полуфабрикаты: да или нет.", "allow_convenience")
    if raw["activity_format"] not in FORMATS:
        return fail("VALIDATION", "Выберите формат активности кнопкой.", "activity_format")
    if raw["workout_place"] not in PLACES:
        return fail("VALIDATION", "Выберите место тренировки кнопкой.", "workout_place")
    if raw["intensity"] not in INTENSITIES:
        return fail("VALIDATION", "Выберите интенсивность кнопкой.", "intensity")
    if not place_matches_format(raw["workout_place"], raw["activity_format"]):
        return fail("VALIDATION", "Место тренировки не совпадает с форматом активности.", "workout_place")

    kitchen = raw["kitchen"]
    if not isinstance(kitchen, list) or any(item not in KITCHEN for item in kitchen) or len(kitchen) != len(set(kitchen)):
        return fail("VALIDATION", "Кухня выбирается только из предложенного списка.", "kitchen")
    equipment = raw["equipment"]
    if not isinstance(equipment, list) or any(item not in EQUIPMENT for item in equipment) or len(equipment) != len(set(equipment)):
        return fail("VALIDATION", "Инвентарь выбирается только из предложенного списка.", "equipment")

    available_error = _unique_ids(raw["available_ingredients"], "available_ingredients", 1, 30, ingredient_ids)
    if available_error:
        return Result(False, None, available_error)
    excluded_error = _unique_ids(raw["excluded_ingredients"], "excluded_ingredients", 0, 30, ingredient_ids)
    if excluded_error:
        return Result(False, None, excluded_error)
    if set(raw["available_ingredients"]) & set(raw["excluded_ingredients"]):
        return fail("VALIDATION", "Один продукт не может быть и доступным, и исключённым.", "excluded_ingredients")

    windows = raw["activity_windows"]
    if not isinstance(windows, list) or not 1 <= len(windows) <= 3:
        return fail("VALIDATION", "Нужно от 1 до 3 интервалов.", "activity_windows")
    normalized = []
    for item in windows:
        if not isinstance(item, dict) or set(item) != {"start", "end"}:
            return fail("VALIDATION", "Интервал задаётся полями start и end.", "activity_windows")
        start = hhmm_to_sec(item["start"])
        end = hhmm_to_sec(item["end"])
        if start is None or end is None or start >= end:
            return fail("VALIDATION", "Проверьте время интервала.", "activity_windows")
        normalized.append({"start": sec_to_hhmm(start), "end": sec_to_hhmm(end)})
    ordered = sorted(normalized, key=lambda item: item["start"])
    for prev, nxt in zip(ordered, ordered[1:]):
        if hhmm_to_sec(prev["end"]) > hhmm_to_sec(nxt["start"]):
            return fail("VALIDATION", "Интервалы не должны пересекаться.", "activity_windows")
    if window_minutes(ordered) < activity:
        return fail("VALIDATION", "Сумма интервалов меньше указанного лимита активности.", "activity_windows")

    profile = {
        "budget_rub_day": budget,
        "cooking_minutes_day": cooking,
        "kitchen": sorted(kitchen),
        "available_ingredients": sorted(raw["available_ingredients"]),
        "excluded_ingredients": sorted(raw["excluded_ingredients"]),
        "allow_convenience": raw["allow_convenience"],
        "activity_minutes_day": activity,
        "activity_windows": ordered,
        "activity_format": raw["activity_format"],
        "workout_place": raw["workout_place"],
        "equipment": sorted(equipment),
        "intensity": raw["intensity"],
        "timezone_offset_min": offset,
    }
    return ok(profile)


def validate_plan_date(value: str, offset_min: int, now: datetime) -> Result:
    parsed = parse_iso_date(value)
    if parsed is None:
        return fail("VALIDATION", "Дата плана записывается как ГГГГ-ММ-ДД.", "plan_date")
    today = local_date(now, offset_min)
    if parsed not in (today, today + timedelta(days=1)):
        return fail("VALIDATION", "План можно составить на сегодня или на завтра.", "plan_date")
    return ok({"plan_date": parsed.isoformat()})


def plan_signature(plan: dict) -> str:
    meals = [meal["recipe_id"] for meal in plan["nutrition"]["meals"]]
    activities = [item["id"] for item in plan["activity"]["items"] if item["kind"] == "activity"]
    parts = meals + [plan["workout"]["template_id"]] + activities
    return "|".join(parts)
