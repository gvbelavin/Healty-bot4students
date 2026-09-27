"""Команды, кнопки и проверка личного чата."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.dialogue import (
    begin_delete,
    begin_diagnosis,
    begin_plan,
    error_view,
    main_menu,
    on_action,
    on_text,
    render,
    start_view,
)
from app.formatting import render_part, render_plan
from app.schemas import utc_now
from app.texts import text
from app.validation import json_size
from app.worker import cancel_active, enqueue, signature_of


def _markup(buttons) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in buttons]
    )


async def _show(update: Update, body: str, buttons) -> None:
    markup = _markup(buttons)
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(body, reply_markup=markup)
            return
        except Exception:
            await update.callback_query.message.reply_text(body, reply_markup=markup)
            return
    await update.message.reply_text(body, reply_markup=markup)


def _state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data["state"]


def _session(state, user_id):
    state["repo"].purge_expired_sessions(state["settings"]["session_ttl_min"])
    state["repo"].purge_old_plans(state["settings"]["plan_retention_days"])
    return state["repo"].get_session(user_id)


async def _apply(update: Update, context, user_id: int, view) -> None:
    state = _state(context)
    repo = state["repo"]
    effect = view.effect or {}
    keep_session = False
    if effect.get("type") == "save_profile":
        if effect.get("consent"):
            repo.set_consent(user_id, "1")
        repo.save_profile(user_id, effect["profile"])
    elif effect.get("type") == "delete":
        await cancel_active(state, user_id)
        result = repo.delete_user_data(user_id)
        if not result.ok:
            view = error_view("DB_ERROR")
    elif effect.get("type") == "broadcast":
        await _broadcast(update, effect.get("messages") or [])
        view = main_menu()
        keep_session = True
    elif effect.get("type") == "submit":
        request = {
            "profile": effect["profile"],
            "profile_revision": effect["revision"],
            "plan_date": effect["plan_date"],
            "exclude_signature": effect.get("exclude_signature"),
        }
        if json_size(request) > 16 * 1024:
            view = error_view("VALIDATION")
        else:
            update_id = update.update_id
            code, seconds = await enqueue(state, user_id, update_id, request)
            if code != "OK":
                view = error_view(code, seconds)
    if view.step is None and not keep_session:
        repo.delete_session(user_id)
    elif view.step is not None:
        repo.save_session(user_id, view.step, view.temporary or {})
    await _show(update, view.text, view.buttons)


async def _guard(update: Update) -> int | None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or chat.type != "private":
        if update.message:
            await update.message.reply_text(text("private_only"))
        elif update.callback_query:
            await update.callback_query.answer()
        return None
    return user.id


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _guard(update)
    if user_id is None:
        return
    state = _state(context)
    state["repo"].get_or_create_user(user_id)
    profile = state["repo"].get_profile(user_id)
    session = _session(state, user_id)
    await _show(update, * _pair(start_view(None if profile is None else profile["data"], session)))


def _pair(view):
    return view.text, view.buttons


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _guard(update)
    if user_id is None:
        return
    state = _state(context)
    state["repo"].get_or_create_user(user_id)
    session = _session(state, user_id)
    if session:
        session["temporary"]["paused"] = True
        state["repo"].save_session(user_id, session["step"], session["temporary"])
    await _show(update, *_pair(main_menu()))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _guard(update)
    if user_id is None:
        return
    _state(context)["repo"].get_or_create_user(user_id)
    await _show(update, text("help"), main_menu().buttons)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _guard(update)
    if user_id is None:
        return
    _state(context)["repo"].delete_session(user_id)
    await _show(update, text("cancel"), main_menu().buttons)


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _guard(update)
    if user_id is None:
        return
    state = _state(context)
    state["repo"].get_or_create_user(user_id)
    view = begin_delete()
    state["repo"].save_session(user_id, view.step, view.temporary)
    await _show(update, view.text, view.buttons)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _guard(update)
    if user_id is None:
        return
    state = _state(context)
    repo = state["repo"]
    repo.get_or_create_user(user_id)
    data = update.callback_query.data or ""
    catalog = state["catalog"]
    profile_row = repo.get_profile(user_id)
    profile = profile_row["data"] if profile_row else None
    revision = profile_row["revision"] if profile_row else None
    session = _session(state, user_id)

    if data == "consent:yes":
        repo.set_consent(user_id, "1")
    if data == "flow:consent" or data == "flow:restart":
        view = begin_diagnosis("create", None, catalog)
        await _apply(update, context, user_id, view)
        return
    if data == "flow:resume" and session:
        session["temporary"]["paused"] = False
        state["repo"].save_session(user_id, session["step"], session["temporary"])
        view = render(session["step"], session["temporary"], catalog)
        await _show(update, view.text, view.buttons)
        return
    if data.startswith("menu:"):
        view = _menu_action(data, profile, revision, repo.latest_ready(user_id), catalog)
        await _apply(update, context, user_id, view)
        return
    if session is None:
        await _show(update, *_pair(error_view("STALE_ACTION")))
        return
    if data == "consent:yes" and session["step"] == "CONSENT":
        session["temporary"]["consent"] = True
    view = on_action(session["step"], session["temporary"], data, catalog, profile, revision, utc_now())
    await _apply(update, context, user_id, view)


def _menu_action(data: str, profile, revision, plan_row, catalog):
    if data == "menu:help":
        return main_menu(text("help"))
    if data == "menu:del":
        return begin_delete()
    if data == "menu:edit":
        if profile is None:
            return begin_diagnosis("create", None, catalog)
        return begin_diagnosis("edit", profile, catalog)
    if data == "menu:new":
        if profile is None:
            return begin_diagnosis("create", None, catalog)
        return begin_plan(profile, catalog)
    if data == "menu:other":
        if profile is None:
            return begin_diagnosis("create", None, catalog)
        view = begin_plan(profile, catalog)
        if plan_row and plan_row["input"].get("profile_revision") == revision:
            view.temporary["exclude_signature"] = signature_of(plan_row)
        return view
    if data == "menu:plan":
        return _show_saved(plan_row, revision, full=True)
    part = {"menu:nut": "nutrition", "menu:act": "activity", "menu:wok": "workout"}.get(data)
    if part:
        return _show_saved(plan_row, revision, part=part)
    return error_view("STALE_ACTION")


def _show_saved(plan_row, revision, full: bool = False, part: str | None = None):
    if not plan_row or not plan_row.get("content"):
        return main_menu(text("empty_plan"))
    content = plan_row["content"]
    previous = revision != content.get("profile_revision")
    if part:
        body = render_part(content, part)
        if previous:
            body = text("previous_limits") + "\n\n" + body
        view = main_menu()
        view.effect = {"type": "broadcast", "messages": [body]}
        return view
    rendered = render_plan(content, previous_limits=previous)
    if not rendered.ok:
        return error_view("INVALID_RESULT")
    view = main_menu()
    view.effect = {"type": "broadcast", "messages": rendered.data["messages"]}
    return view


async def _broadcast(update: Update, messages: list[str]) -> None:
    target = update.callback_query.message if update.callback_query else update.message
    for body in messages:
        await target.reply_text(body)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _guard(update)
    if user_id is None:
        return
    state = _state(context)
    state["repo"].get_or_create_user(user_id)
    raw = update.message.text or ""
    if len(raw.strip()) > state["settings"]["input_max_chars"]:
        await _show(update, "Сократите сообщение до 500 символов.", main_menu().buttons)
        return
    session = _session(state, user_id)
    if session is None:
        await _show(update, *_pair(main_menu(text("unknown"))))
        return
    view = on_text(session["step"], session["temporary"], raw.strip(), state["catalog"])
    await _apply(update, context, user_id, view)


async def on_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _guard(update)
    if user_id is None:
        return
    await _show(update, text("unsupported"), main_menu().buttons)


def register(application) -> None:
    from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("menu", cmd_menu))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("delete", cmd_delete))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL | filters.AUDIO | filters.VOICE | filters.VIDEO, on_unsupported))
