"""Очередь генерации: два исполнителя, дедлайн и лимиты."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

from app.dialogue import error_view
from app.formatting import render_plan
from app.schemas import plan_signature, utc_now
from app.services.orchestrator import build_daily_plan

logger = logging.getLogger("app")


def admission(active: bool, jobs_24h: int, last_created: datetime | None, queue_size: int, settings: dict, now: datetime) -> tuple[str, int]:
    if active:
        return "BUSY", 0
    if jobs_24h >= settings["user_jobs_per_24h"]:
        return "DAILY_LIMIT", 0
    if last_created is not None:
        elapsed = (now - last_created).total_seconds()
        if elapsed < settings["user_cooldown_sec"]:
            return "RATE_LIMIT", max(1, math.ceil(settings["user_cooldown_sec"] - elapsed))
    if queue_size >= settings["queue_capacity"]:
        return "BUSY", 0
    return "OK", 0


async def enqueue(state: dict, telegram_id: int, request_key: int, request: dict) -> tuple[str, int]:
    repo = state["repo"]
    settings = state["settings"]
    now = utc_now()
    existing = repo.plan_by_request(request_key)
    if existing is not None:
        return "OK", 0
    code, seconds = admission(
        active=repo.active_plan(telegram_id) is not None,
        jobs_24h=repo.jobs_since(telegram_id, now - timedelta(hours=24)),
        last_created=repo.last_plan_time(telegram_id),
        queue_size=state["queue"].qsize(),
        settings=settings,
        now=now,
    )
    if code != "OK":
        return code, seconds
    created = repo.create_plan(telegram_id, request_key, request)
    if "error" in created:
        return "BUSY" if created["error"] == "BUSY" else "DB_ERROR", 0
    job = {
        "plan_id": created["id"],
        "telegram_id": telegram_id,
        "request": request,
        "accepted_at": now,
        "cancel": False,
        "done": asyncio.Event(),
    }
    state["jobs"][telegram_id] = job
    try:
        state["queue"].put_nowait(job)
    except asyncio.QueueFull:
        repo.mark_failed(created["id"], "BUSY")
        state["jobs"].pop(telegram_id, None)
        return "BUSY", 0
    return "OK", 0


async def cancel_active(state: dict, telegram_id: int) -> None:
    job = state["jobs"].get(telegram_id)
    if job is None:
        return
    job["cancel"] = True
    try:
        await asyncio.wait_for(job["done"].wait(), timeout=state["settings"]["generation_deadline_sec"] + 2)
    except asyncio.TimeoutError:
        logger.info("event=cancel_timeout code=TIMEOUT catalog=%s", state["catalog"]["manifest"]["version"])


async def worker_loop(state: dict) -> None:
    while True:
        job = await state["queue"].get()
        try:
            if job is None:
                return
            await _run_job(state, job)
        finally:
            if job is not None:
                job["done"].set()
                state["jobs"].pop(job["telegram_id"], None)
            state["queue"].task_done()


async def _run_job(state: dict, job: dict) -> None:
    repo = state["repo"]
    settings = state["settings"]
    catalog = state["catalog"]
    started = utc_now()
    deadline = job["accepted_at"] + timedelta(seconds=settings["generation_deadline_sec"])
    plan_id = job["plan_id"]
    if job["cancel"] or not repo.user_exists(job["telegram_id"]):
        repo.mark_failed(plan_id, "RESTARTED")
        return
    if utc_now() > deadline:
        repo.mark_failed(plan_id, "TIMEOUT")
        await _notify(state, job, error_view("TIMEOUT").text)
        return
    if not repo.mark_running(plan_id):
        return
    result = await asyncio.to_thread(build_daily_plan, job["request"], catalog, started, deadline)
    if job["cancel"] or not repo.user_exists(job["telegram_id"]):
        repo.mark_failed(plan_id, "RESTARTED")
        return
    elapsed = int((utc_now() - started).total_seconds() * 1000)
    if not result.ok:
        code = result.error.code
        repo.mark_failed(plan_id, code)
        logger.info("event=%s code=%s ms=%s catalog=%s", plan_id, code, elapsed, catalog["manifest"]["version"])
        await _notify(state, job, error_view(code).text)
        return
    profile = repo.get_profile(job["telegram_id"])
    previous = profile is None or profile["revision"] != result.data["profile_revision"]
    rendered = render_plan(result.data, previous_limits=previous)
    if not rendered.ok or not repo.mark_ready(plan_id, result.data):
        repo.mark_failed(plan_id, "INVALID_RESULT")
        await _notify(state, job, error_view("INVALID_RESULT").text)
        return
    logger.info("event=%s code=READY ms=%s catalog=%s", plan_id, elapsed, catalog["manifest"]["version"])
    delivered = await _notify(state, job, rendered.data["messages"])
    repo.mark_delivery(plan_id, "sent" if delivered else "failed")
    repo.maybe_daily_backup(settings["backup_retention_days"])


async def _notify(state: dict, job: dict, payload) -> bool:
    messages = payload if isinstance(payload, list) else [payload]
    bot = state["bot"]
    delays = [0, *state["settings"]["send_retry_delays_sec"]]
    for message in messages:
        sent = False
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                await bot.send_message(chat_id=job["telegram_id"], text=message)
                sent = True
                break
            except Exception as exc:
                name = type(exc).__name__
                if name == "RetryAfter":
                    await asyncio.sleep(getattr(exc, "retry_after", 1))
                    continue
                if name in ("TimedOut", "NetworkError"):
                    continue
                logger.info("event=%s code=DELIVERY_FAILED ms=0 catalog=%s", job["plan_id"], state["catalog"]["manifest"]["version"])
                return False
        if not sent:
            logger.info("event=%s code=DELIVERY_FAILED ms=0 catalog=%s", job["plan_id"], state["catalog"]["manifest"]["version"])
            return False
    return True


def signature_of(plan_row: dict) -> str | None:
    if not plan_row or not plan_row.get("content"):
        return None
    return plan_signature(plan_row["content"])
