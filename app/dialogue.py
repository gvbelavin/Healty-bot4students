"""Переходы диагностики и главного меню."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from app.formatting import profile_summary
from app.schemas import (
    KITCHEN,
    fail,
    local_date,
    parse_minutes_text,
    parse_timezone_text,
    parse_windows_text,
    place_matches_format,
    validate_profile,
    window_minutes,
)
from app.texts import text
from app.validation import looks_medical

KITCHEN_LABELS = {
    "kettle": "Чайник",
    "microwave": "Микроволновка",
    "stove": "Плита",
    "fridge": "Холодильник",
}
FORMAT_LABELS = {"indoors": "В помещении", "outdoors": "На улице", "mixed": "Смешанный"}
PLACE_LABELS = {"home": "Дом", "dorm": "Общежитие", "outdoors": "Улица"}
EQUIPMENT_LABELS = {"mat": "Коврик", "chair": "Стул"}
INTENSITY_LABELS = {"light": "Лёгкая", "moderate": "Умеренная"}
TIMEZONES = (180, 300, 420, 480)
ORDER = [
    "CONSENT",
    "BUDGET",
    "COOKING",
    "KITCHEN",
    "INGREDIENTS",
    "EXCLUSIONS",
    "CONVENIENCE",
    "ACTIVITY_TIME",
    "WINDOWS",
    "FORMAT",
    "PLACE",
    "EQUIPMENT",
    "INTENSITY",
    "TIMEZONE",
    "CONFIRM",
]


@dataclass
class View:
    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)
    step: str | None = None
    temporary: dict | None = None
    effect: dict | None = None


def menu_buttons(include_other: bool = True) -> list[list[tuple[str, str]]]:
    rows = [
        [(text("new_plan"), "menu:new"), (text("my_plan"), "menu:plan")],
        [(text("nutrition"), "menu:nut"), (text("activity"), "menu:act"), (text("workout"), "menu:wok")],
        [(text("edit"), "menu:edit")],
        [(text("how"), "menu:help"), (text("delete"), "menu:del")],
    ]
    if include_other:
        rows.insert(2, [(text("other"), "menu:other")])
    return rows


def main_menu(note: str = "") -> View:
    body = text("menu") if not note else f"{note}\n\n{text('menu')}"
    return View(body, menu_buttons(), step=None, temporary=None)


def start_view(saved_profile: dict | None, session: dict | None) -> View:
    if session is not None:
        return View(
            text("resume"),
            [[(text("continue"), "flow:resume"), (text("restart"), "flow:restart")]],
            step=session["step"],
            temporary=session["temporary"],
        )
    if saved_profile is None:
        return View(text("welcome"), [[(text("start_diag"), "flow:consent")]])
    return main_menu()


def blank(mode: str = "create") -> dict:
    return {"mode": mode, "draft": {}, "page": 0, "select": []}


def _nav() -> list[tuple[str, str]]:
    return [(text("back"), "nav:back"), (text("to_menu"), "nav:menu")]


def _marked(labels: dict[str, str], selected: list[str], prefix: str) -> list[list[tuple[str, str]]]:
    rows = []
    row = []
    for key, label in labels.items():
        title = f"✓ {label}" if key in selected else label
        row.append((title, f"{prefix}:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _page_buttons(items: list[dict], selected: list[str], page: int, prefix: str) -> list[list[tuple[str, str]]]:
    pages = max(1, (len(items) + 4) // 5)
    page = max(0, min(page, pages - 1))
    chunk = items[page * 5:(page + 1) * 5]
    rows = []
    for item in chunk:
        mark = "✓ " if item["id"] in selected else ""
        rows.append([(f"{mark}{item['name']}", f"{prefix}:id:{item['id']}")])
    nav = []
    if page > 0:
        nav.append(("←", f"{prefix}:page:{page - 1}"))
    if page + 1 < pages:
        nav.append(("→", f"{prefix}:page:{page + 1}"))
    if nav:
        rows.append(nav)
    return rows


def render(step: str, temporary: dict, catalog: dict) -> View:
    draft = temporary.get("draft", {})
    select = temporary.get("select", [])
    names = {item["id"]: item["name"] for item in catalog["ingredients"]}

    def current(value: str) -> str:
        return f"\n{text('current', value=value)}" if temporary.get("mode") == "edit" and value else ""

    if step == "CONSENT":
        return View(text("consent"), [[(text("agree"), "consent:yes"), (text("decline"), "consent:no")]], step, temporary)
    if step == "BUDGET":
        shown = str(draft["budget_rub_day"]) if "budget_rub_day" in draft else ""
        return View(text("budget_prompt") + current(shown), [_nav() + ([(text("keep"), "keep")] if shown else [])], step, temporary)
    if step == "COOKING":
        shown = str(draft["cooking_minutes_day"]) if "cooking_minutes_day" in draft else ""
        return View(text("cooking_prompt") + current(shown), [_nav() + ([(text("keep"), "keep")] if shown else [])], step, temporary)
    if step == "KITCHEN":
        rows = _marked(KITCHEN_LABELS, select, "kitchen")
        rows.append([(text("done"), "kitchen:done")])
        rows.append(_nav())
        return View(text("kitchen_prompt"), rows, step, temporary)
    if step == "INGREDIENTS":
        rows = _page_buttons(catalog["ingredients"], select, temporary.get("page", 0), "ing")
        rows.append([(text("done"), "ing:ok")])
        rows.append(_nav())
        return View(text("ingredients_prompt"), rows, step, temporary)
    if step == "EXCLUSIONS":
        rows = _page_buttons(catalog["ingredients"], select, temporary.get("page", 0), "exc")
        rows.append([(text("none"), "exc:none"), (text("done"), "exc:ok")])
        rows.append(_nav())
        return View(text("exclusions_prompt"), rows, step, temporary)
    if step == "CONVENIENCE":
        return View(
            text("convenience_prompt"),
            [[(text("yes"), "conv:yes"), (text("no"), "conv:no")], _nav()],
            step,
            temporary,
        )
    if step == "ACTIVITY_TIME":
        shown = str(draft["activity_minutes_day"]) if "activity_minutes_day" in draft else ""
        return View(text("activity_prompt") + current(shown), [_nav() + ([(text("keep"), "keep")] if shown else [])], step, temporary)
    if step == "WINDOWS":
        shown = ", ".join(f"{item['start']}-{item['end']}" for item in draft.get("activity_windows", []))
        return View(text("windows_prompt") + current(shown), [_nav() + ([(text("keep"), "keep")] if shown else [])], step, temporary)
    if step == "FORMAT":
        return View(text("format_prompt"), [[(label, f"fmt:{key}") for key, label in FORMAT_LABELS.items()], _nav()], step, temporary)
    if step == "PLACE":
        return View(text("place_prompt"), [[(label, f"place:{key}") for key, label in PLACE_LABELS.items()], _nav()], step, temporary)
    if step == "EQUIPMENT":
        rows = _marked(EQUIPMENT_LABELS, select, "eq")
        rows.append([(text("no_equipment"), "eq:none"), (text("done"), "eq:done")])
        rows.append(_nav())
        return View(text("equipment_prompt"), rows, step, temporary)
    if step == "INTENSITY":
        return View(text("intensity_prompt"), [[(label, f"int:{key}") for key, label in INTENSITY_LABELS.items()], _nav()], step, temporary)
    if step == "TIMEZONE":
        buttons = [(f"UTC+{value // 60}" if value % 60 == 0 else f"UTC+{value // 60}:{value % 60:02d}", f"tz:{value}") for value in TIMEZONES]
        return View(text("timezone_prompt"), [buttons, _nav()], step, temporary)
    if step == "CONFIRM":
        return View(text("confirm_prompt") + "\n\n" + profile_summary(draft, names), [[(text("save"), "confirm:save")], _nav()], step, temporary)
    if step == "DATE":
        return View(text("date_prompt"), [[(text("today"), "date:today"), (text("tomorrow"), "date:tomorrow")], _nav()], step, temporary)
    if step == "SUBMIT":
        return View(text("submit_prompt") + "\n\n" + profile_summary(draft, names), [[(text("create"), "submit:yes")], _nav()], step, temporary)
    if step == "DELETE_CONFIRM":
        return View(text("delete_ask"), [[(text("confirm_delete"), "delete:yes"), (text("cancel_button"), "delete:no")]], step, temporary)
    return main_menu()


def _advance(temporary: dict, step: str, catalog: dict, select: list | None = None) -> View:
    nxt = ORDER[ORDER.index(step) + 1]
    temporary = {**temporary, "page": 0, "select": list(select if select is not None else temporary["draft"].get(_select_key(nxt), []))}
    return render(nxt, temporary, catalog)


def _select_key(step: str) -> str:
    return {
        "KITCHEN": "kitchen",
        "INGREDIENTS": "available_ingredients",
        "EXCLUSIONS": "excluded_ingredients",
        "EQUIPMENT": "equipment",
    }.get(step, "")


def _back(temporary: dict, step: str, catalog: dict) -> View:
    if step in ("CONSENT", "DATE", "DELETE_CONFIRM"):
        return main_menu()
    if step == "SUBMIT":
        return render("DATE", temporary, catalog)
    if step not in ORDER:
        return main_menu()
    prev = ORDER[ORDER.index(step) - 1]
    temporary = {**temporary, "page": 0, "select": list(temporary["draft"].get(_select_key(prev), []))}
    return render(prev, temporary, catalog)


def _reject(step: str, temporary: dict, catalog: dict, message: str) -> View:
    view = render(step, temporary, catalog)
    view.text = f"{message}\n\n{view.text}"
    return view


def begin_diagnosis(mode: str, profile: dict | None, catalog: dict) -> View:
    temporary = blank(mode)
    if profile and mode == "edit":
        temporary["draft"] = dict(profile)
        temporary["select"] = list(profile.get("budget_rub_day") and [])
        return render("BUDGET", temporary, catalog)
    return render("CONSENT", temporary, catalog)


def begin_plan(profile: dict, catalog: dict) -> View:
    temporary = blank("plan")
    temporary["draft"] = dict(profile)
    return render("DATE", temporary, catalog)


def begin_delete() -> View:
    return render("DELETE_CONFIRM", blank("delete"), None if False else {"ingredients": []})


def on_action(step: str, temporary: dict, action: str, catalog: dict, saved_profile: dict | None, revision: int | None, now) -> View:
    temporary = {
        "mode": temporary.get("mode", "create"),
        "draft": dict(temporary.get("draft", {})),
        "page": temporary.get("page", 0),
        "select": list(temporary.get("select", [])),
        "consent": temporary.get("consent", False),
        "exclude_signature": temporary.get("exclude_signature"),
        "paused": False,
    }
    draft = temporary["draft"]
    if action == "nav:menu":
        temporary["paused"] = True
        return View(text("menu"), menu_buttons(), step=step, temporary=temporary)
    if action == "nav:back":
        return _back(temporary, step, catalog)
    if action == "keep":
        return _advance(temporary, step, catalog)

    if step == "CONSENT":
        if action == "consent:yes":
            temporary["consent"] = True
            return _advance(temporary, step, catalog, [])
        if action == "consent:no":
            return main_menu(text("consent_declined"))
    if step == "KITCHEN" and action.startswith("kitchen:"):
        key = action.split(":", 1)[1]
        if key == "done":
            draft["kitchen"] = [item for item in KITCHEN if item in temporary["select"]]
            return _advance(temporary, step, catalog)
        if key in KITCHEN:
            _toggle(temporary, key)
            return render(step, temporary, catalog)
    if step == "INGREDIENTS" and action.startswith("ing:"):
        return _paged(step, temporary, action, "ing", "available_ingredients", catalog, empty_ok=False)
    if step == "EXCLUSIONS" and action.startswith("exc:"):
        if action == "exc:none":
            draft["excluded_ingredients"] = []
            return _advance(temporary, step, catalog, [])
        return _paged(step, temporary, action, "exc", "excluded_ingredients", catalog, empty_ok=True)
    if step == "CONVENIENCE" and action in ("conv:yes", "conv:no"):
        draft["allow_convenience"] = action == "conv:yes"
        return _advance(temporary, step, catalog, [])
    if step == "FORMAT" and action.startswith("fmt:"):
        draft["activity_format"] = action.split(":", 1)[1]
        return _advance(temporary, step, catalog, [])
    if step == "PLACE" and action.startswith("place:"):
        place = action.split(":", 1)[1]
        if not place_matches_format(place, draft.get("activity_format", "")):
            return _reject(step, temporary, catalog, "Это место не совпадает с выбранным форматом.")
        draft["workout_place"] = place
        return _advance(temporary, step, catalog, draft.get("equipment", []))
    if step == "EQUIPMENT" and action.startswith("eq:"):
        key = action.split(":", 1)[1]
        if key == "none":
            draft["equipment"] = []
            return _advance(temporary, step, catalog, [])
        if key == "done":
            draft["equipment"] = [item for item in EQUIPMENT_LABELS if item in temporary["select"]]
            return _advance(temporary, step, catalog, [])
        if key in EQUIPMENT_LABELS:
            _toggle(temporary, key)
            return render(step, temporary, catalog)
    if step == "INTENSITY" and action.startswith("int:"):
        draft["intensity"] = action.split(":", 1)[1]
        return _advance(temporary, step, catalog)
    if step == "TIMEZONE" and action.startswith("tz:"):
        draft["timezone_offset_min"] = int(action.split(":", 1)[1])
        return _advance(temporary, step, catalog)
    if step == "CONFIRM" and action == "confirm:save":
        checked = validate_profile(draft, {item["id"] for item in catalog["ingredients"]})
        if not checked.ok:
            return _reject(step, temporary, catalog, checked.error.message)
        return View(text("saved"), menu_buttons(), step=None, temporary=None, effect={"type": "save_profile", "profile": checked.data, "consent": temporary.get("consent", False)})
    if step == "DATE" and action in ("date:today", "date:tomorrow"):
        offset = draft["timezone_offset_min"]
        today = local_date(now, offset)
        chosen = today if action == "date:today" else today + timedelta(days=1)
        draft["plan_date"] = chosen.isoformat()
        return render("SUBMIT", temporary, catalog)
    if step == "SUBMIT" and action == "submit:yes":
        if saved_profile is None or revision is None:
            return _reject(step, temporary, catalog, "Сначала сохраните профиль.")
        return View(
            text("waiting"),
            [],
            step=None,
            temporary=None,
            effect={
                "type": "submit",
                "profile": saved_profile,
                "revision": revision,
                "plan_date": draft["plan_date"],
                "exclude_signature": temporary.get("exclude_signature"),
            },
        )
    if step == "DELETE_CONFIRM" and action == "delete:yes":
        return View(text("deleted"), [], step=None, temporary=None, effect={"type": "delete"})
    if step == "DELETE_CONFIRM" and action == "delete:no":
        return main_menu()
    return _reject(step, temporary, catalog, text("stale"))


def on_text(step: str, temporary: dict, message: str, catalog: dict) -> View:
    temporary = {
        "mode": temporary.get("mode", "create"),
        "draft": dict(temporary.get("draft", {})),
        "page": temporary.get("page", 0),
        "select": list(temporary.get("select", [])),
        "consent": temporary.get("consent", False),
        "exclude_signature": temporary.get("exclude_signature"),
        "paused": temporary.get("paused", False),
    }
    if temporary.get("paused"):
        return View(text("unknown"), menu_buttons(), step=step, temporary=temporary)
    if looks_medical(message):
        return _reject(step, temporary, catalog, text("medical"))
    draft = temporary["draft"]
    if step == "BUDGET":
        return _number(step, temporary, catalog, message, "budget_rub_day", 100, 3000, weekly=True)
    if step == "COOKING":
        return _number(step, temporary, catalog, message, "cooking_minutes_day", 0, 120, weekly=False)
    if step == "ACTIVITY_TIME":
        return _number(step, temporary, catalog, message, "activity_minutes_day", 5, 120, weekly=False)
    if step == "WINDOWS":
        windows, error = parse_windows_text(message)
        if error:
            return _reject(step, temporary, catalog, error)
        limit = draft.get("activity_minutes_day")
        if limit is not None and window_minutes(windows) < limit:
            return _reject(step, temporary, catalog, "Сумма интервалов меньше указанного лимита активности.")
        draft["activity_windows"] = windows
        return _advance(temporary, step, catalog, [])
    if step == "TIMEZONE":
        offset = parse_timezone_text(message)
        if offset is None:
            return _reject(step, temporary, catalog, "Нужен часовой пояс, например +07:00.")
        draft["timezone_offset_min"] = offset
        return _advance(temporary, step, catalog)
    return _reject(step, temporary, catalog, "На этом шаге выберите кнопку.")


def _number(step, temporary, catalog, message, field, low, high, weekly: bool) -> View:
    if weekly and "недел" in message.lower().replace("ё", "е"):
        return _reject(step, temporary, catalog, "Нужен бюджет на один день, не на неделю.")
    value = parse_minutes_text(message)
    if value is None or not low <= value <= high:
        return _reject(step, temporary, catalog, f"Введите целое число от {low} до {high}.")
    temporary["draft"][field] = value
    return _advance(temporary, step, catalog)


def _toggle(temporary: dict, key: str) -> None:
    selected = temporary["select"]
    if key in selected:
        selected.remove(key)
    else:
        selected.append(key)


def _paged(step, temporary, action, prefix, field, catalog, empty_ok: bool) -> View:
    if action.startswith(f"{prefix}:page:"):
        temporary["page"] = int(action.rsplit(":", 1)[1])
        return render(step, temporary, catalog)
    if action.startswith(f"{prefix}:id:"):
        _toggle(temporary, action.split(":", 2)[2])
        if len(temporary["select"]) > 30:
            temporary["select"].pop()
            return _reject(step, temporary, catalog, "Можно выбрать не больше 30 продуктов.")
        return render(step, temporary, catalog)
    if action == f"{prefix}:ok":
        chosen = list(temporary["select"])
        if not empty_ok and not chosen:
            return _reject(step, temporary, catalog, "Выберите хотя бы один продукт.")
        other = temporary["draft"].get("available_ingredients", [])
        if field == "excluded_ingredients" and set(chosen) & set(other):
            return _reject(step, temporary, catalog, "Уберите продукт, который уже отмечен как доступный.")
        temporary["draft"][field] = chosen
        return _advance(temporary, step, catalog)
    return _reject(step, temporary, catalog, text("stale"))


def error_view(code: str, seconds: int = 0) -> View:
    if code == "RATE_LIMIT":
        body = text("rate_limit", seconds=max(seconds, 1))
    elif code == "DAILY_LIMIT":
        body = text("daily_limit")
    elif code == "BUSY":
        body = text("busy")
    elif code == "NO_MATCH":
        body = text("no_match")
    elif code == "NO_ALTERNATIVE":
        body = text("no_alternative")
    elif code == "TIMEOUT" or code == "RESTARTED":
        body = text("timeout")
    elif code in ("CATALOG_ERROR", "INVALID_RESULT"):
        body = text("catalog_error")
    elif code == "DB_ERROR":
        body = text("db_error")
    else:
        body = text("stale")
    return main_menu(body)
