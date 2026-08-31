"""
Форматирование текущего локального времени (settings.TIMEZONE) для
записи в Google Sheets (timestamp_created, timestamp_offered и т.д.).

Вынесено в отдельный модуль намеренно: и handlers/order.py, и
services/dispatch.py (Day 5) нуждаются в одной и той же функции — если
оставить её приватной в order.py, dispatch.py пришлось бы импортировать
из handlers, а order.py, в свою очередь, импортирует dispatch.py (чтобы
запустить диспетчеризацию после создания заказа) — получился бы
циклический импорт. utils/ — «листовой» модуль, от него ничего не
зависит и он ни от чего в handlers/services не зависит, поэтому оба
могут спокойно импортировать отсюда.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from config import Settings


def now_local_str(settings: Settings) -> str:
    return datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
