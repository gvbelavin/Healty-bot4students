"""Текстовая выдача плана без разметки Telegram."""

from __future__ import annotations

from app.schemas import fail, format_offset, ok

SLOT_TITLE = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин"}
FORMAT_TITLE = {"indoors": "в помещении", "outdoors": "на улице", "mixed": "смешанный"}
PLACE_TITLE = {"home": "дом", "dorm": "общежитие", "outdoors": "улица"}
INTENSITY_TITLE = {"light": "лёгкая", "moderate": "умеренная"}
KITCHEN_TITLE = {
    "kettle": "чайник",
    "microwave": "микроволновка",
    "stove": "плита",
    "fridge": "холодильник",
}


def rubles(kopecks: int) -> str:
    rub = kopecks // 100
    frac = kopecks % 100
    if frac == 0:
        return f"{rub} руб"
    return f"{rub},{frac:02d} руб"


def profile_summary(profile: dict, names: dict[str, str]) -> str:
    kitchen = ", ".join(KITCHEN_TITLE[item] for item in profile["kitchen"]) or "без оборудования"
    products = ", ".join(names.get(item, item) for item in profile["available_ingredients"])
    excluded = ", ".join(names.get(item, item) for item in profile["excluded_ingredients"]) or "нет"
    windows = ", ".join(f"{item['start']}–{item['end']}" for item in profile["activity_windows"])
    equipment = ", ".join({"mat": "коврик", "chair": "стул"}[item] for item in profile["equipment"]) or "без инвентаря"
    convenience = "да" if profile["allow_convenience"] else "нет"
    return "\n".join(
        [
            f"Бюджет на день: {profile['budget_rub_day']} руб.",
            f"Готовка: {profile['cooking_minutes_day']} мин.",
            f"Кухня: {kitchen}.",
            f"Продукты: {products}.",
            f"Исключения: {excluded}.",
            f"Полуфабрикаты: {convenience}.",
            f"Активность: {profile['activity_minutes_day']} мин., {windows}.",
            f"Формат: {FORMAT_TITLE[profile['activity_format']]}, место: {PLACE_TITLE[profile['workout_place']]}.",
            f"Инвентарь: {equipment}. Интенсивность: {INTENSITY_TITLE[profile['intensity']]}.",
            f"Часовой пояс: {format_offset(profile['timezone_offset_min'])}.",
        ]
    )


def _meal_block(meal: dict) -> str:
    lines = [
        f"{SLOT_TITLE[meal['slot']]}: {meal['title']}",
        "Ингредиенты:",
        *[f"— {line}" for line in meal["ingredients"]],
        "Шаги:",
        *[f"{index}. {step}" for index, step in enumerate(meal["steps"], start=1)],
        f"Оценка порции: {rubles(meal['cost_kopecks'])}. Время: {meal['cooking_min']} мин.",
        f"Совет: {meal['tip']}",
    ]
    return "\n".join(lines)


def _workout_block(workout: dict) -> str:
    lines = [f"Тренировка: {workout['title']}", f"Длительность: {workout['duration_sec'] // 60} мин."]
    for index, exercise in enumerate(workout["exercises"], start=1):
        lines.append(
            f"{index}. {exercise['name']}: {exercise['work_sec']} с работа, {exercise['rest_sec']} с отдых. {exercise['instruction']}"
        )
    return "\n".join(lines)


def _activity_block(activity: dict) -> str:
    lines = ["Расписание:"]
    for item in activity["items"]:
        kind = "тренировка" if item["kind"] == "workout" else "активность"
        lines.append(f"{item['start']}–{item['end']} — {item['title']} ({kind}). {item['instruction']}")
    lines.append(f"Всего: {activity['total_sec'] // 60} мин.")
    return "\n".join(lines)


def render_plan(plan: dict, previous_limits: bool = False) -> Result:
    header = [
        f"План на {plan['plan_date']}.",
        f"Часовой пояс: {format_offset(plan['timezone_offset_min'])}.",
    ]
    if previous_limits:
        header.append("План построен по прежним ограничениям. Можно собрать новый.")
    header.append("Маршрут:")
    header.extend(f"— {step}" for step in plan["steps"])
    if plan["warnings"]:
        header.append("Важно:")
        header.extend(f"— {item}" for item in plan["warnings"])
    messages = [
        "\n".join(header),
        "Питание\n" + "\n\n".join(_meal_block(meal) for meal in plan["nutrition"]["meals"])
        + f"\n\nИтого: {rubles(plan['nutrition']['total_cost_kopecks'])}, готовка {plan['nutrition']['total_cooking_min']} мин.",
        _workout_block(plan["workout"]),
        _activity_block(plan["activity"]),
    ]
    if len(messages) > 4 or any(len(message) > 3500 for message in messages):
        return fail("INVALID_RESULT", "План не помещается в допустимые сообщения.")
    return ok({"messages": messages})


def render_part(plan: dict, part: str) -> str:
    rendered = render_plan(plan)
    if not rendered.ok:
        return "Сейчас не удаётся показать сохранённый план."
    index = {"nutrition": 1, "workout": 2, "activity": 3}[part]
    return rendered.data["messages"][index]
