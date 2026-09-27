"""Long polling, запуск worker и остановка."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date
from pathlib import Path

from telegram.ext import Application
from telegram.request import HTTPXRequest

from app.handlers import register
from app.repositories import Repository
from app.validation import load_catalog
from app.worker import worker_loop

ROOT = Path(__file__).resolve().parents[1]


def load_settings() -> dict:
    settings = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
    settings["db_path"] = str(ROOT / settings["db_path"])
    settings["catalog_path"] = str(ROOT / settings["catalog_path"])
    return settings


def load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def _post_init(application: Application) -> None:
    state = application.bot_data["state"]
    state["bot"] = application.bot
    state["repo"].fail_interrupted()
    state["repo"].maybe_daily_backup(state["settings"]["backup_retention_days"])
    for _ in range(state["settings"]["workers"]):
        state["tasks"].append(asyncio.create_task(worker_loop(state)))
    await application.bot.set_my_commands(
        [
            ("start", "Начать"),
            ("menu", "Меню"),
            ("help", "Как это работает"),
            ("cancel", "Остановить опрос"),
            ("delete", "Удалить данные"),
        ]
    )


async def _post_shutdown(application: Application) -> None:
    state = application.bot_data["state"]
    for _ in state["tasks"]:
        await state["queue"].put(None)
    for task in state["tasks"]:
        await task


def build_application(token: str | None = None) -> Application:
    load_env()
    settings = load_settings()
    token = token or os.environ.get("BOT_TOKEN", "")
    if not token:
        raise SystemExit("Задайте BOT_TOKEN в окружении или в файле .env.")
    catalog = load_catalog(Path(settings["catalog_path"]), date.today())
    if settings["generator"] != "catalog":
        raise SystemExit("В этой версии генератор только catalog.")
    repo = Repository(
        Path(settings["db_path"]),
        ROOT / "backups",
        settings["sqlite_busy_timeout_ms"],
    )
    request = HTTPXRequest(
        connect_timeout=settings["send_timeout_sec"],
        read_timeout=settings["send_timeout_sec"],
        write_timeout=settings["send_timeout_sec"],
    )
    application = (
        Application.builder()
        .token(token)
        .request(request)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["state"] = {
        "settings": settings,
        "catalog": catalog,
        "repo": repo,
        "queue": asyncio.Queue(maxsize=settings["queue_capacity"]),
        "jobs": {},
        "tasks": [],
        "bot": None,
    }
    register(application)
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build_application().run_polling(timeout=load_settings()["poll_timeout_sec"])


if __name__ == "__main__":
    main()
