"""Тексты интерфейса из config/messages.ru.json."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MESSAGES = json.loads((ROOT / "config" / "messages.ru.json").read_text(encoding="utf-8"))


def text(key: str, **kwargs) -> str:
    value = MESSAGES[key]
    return value.format(**kwargs) if kwargs else value
