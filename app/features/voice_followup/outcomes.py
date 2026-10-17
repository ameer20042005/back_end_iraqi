# -*- coding: utf-8 -*-
"""مخزن نتائج المكالمات — ملف JSON لكل غرفة تحت `settings.call_outcomes_dir`.

تطبيق سطح المكتب ما يستقبل webhooks، فيسحب النتيجة بـ
GET /voice_followup/calls/{room}/outcome. الـ agent worker عملية منفصلة عن
uvicorn (start.sh)، فالملفات هي المخزن المشترك بين العمليتين بدل dict بالذاكرة.

- الكتابة ذرّية (ملف مؤقت ثم replace) حتى ما يقرأ الـ GET ملفاً نصف مكتوب.
- مدة البقاء `call_outcomes_ttl_hours` (افتراضياً ٢٤ ساعة)، والعدد محدود بـ
  `call_outcomes_max_files`؛ التنظيف يصير عند كل كتابة.
- اسم الغرفة يُفحص بتعبير صارم قبل أي وصول للقرص (لا path traversal).
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import settings
from app.features.voice_followup.schema import CallOutcome

logger = logging.getLogger(__name__)

ROOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class InvalidRoom(ValueError):
    pass


def _dir() -> Path:
    return Path(settings.call_outcomes_dir)


def _path(room: str) -> Path:
    if not isinstance(room, str) or not ROOM_RE.fullmatch(room):
        raise InvalidRoom(f"اسم غرفة غير صالح: {room!r}")
    return _dir() / f"{room}.json"


def _ttl_seconds() -> float:
    return settings.call_outcomes_ttl_hours * 3600


def _prune() -> None:
    """يحذف النتائج المنتهية، ثم الأقدم لو تجاوز العدد السقف."""
    try:
        files = [(p, p.stat().st_mtime) for p in _dir().glob("*.json")]
    except OSError:
        return
    cutoff = time.time() - _ttl_seconds()
    alive = []
    for path, mtime in files:
        if mtime < cutoff:
            path.unlink(missing_ok=True)
        else:
            alive.append((path, mtime))
    overflow = len(alive) - settings.call_outcomes_max_files
    if overflow > 0:
        for path, _ in sorted(alive, key=lambda x: x[1])[:overflow]:
            path.unlink(missing_ok=True)


def save_outcome(outcome: CallOutcome) -> None:
    path = _path(outcome.room)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(outcome.model_dump_json(), encoding="utf-8")
    os.replace(tmp, path)
    _prune()


def load_outcome(room: str) -> Optional[CallOutcome]:
    """النتيجة المحفوظة، أو None لو ما انحفظت بعد أو انتهت مدتها."""
    path = _path(room)
    try:
        if time.time() - path.stat().st_mtime > _ttl_seconds():
            return None
        return CallOutcome.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.exception("ملف نتيجة تالف للغرفة %s", room)
        return None


def get_outcome(room: str) -> CallOutcome:
    """جواب نقطة السحب: النتيجة لو موجودة، وإلا pending."""
    return load_outcome(room) or CallOutcome(room=room, status="pending")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
