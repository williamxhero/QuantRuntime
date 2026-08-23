from __future__ import annotations

from datetime import date
from typing import Any


def canonical_trading_days(rows: list[dict[str, Any]]) -> tuple[date, ...]:
    try:
        days = tuple(date.fromisoformat(str(row["trade_date"])) for row in rows)
    except (KeyError, ValueError) as exc:
        raise ValueError("calendar contains an invalid date") from exc
    if days != tuple(sorted(set(days))):
        raise ValueError("calendar is duplicated or out of order")
    if any(row.get("is_open") is not True for row in rows):
        raise ValueError("calendar contains a closed day")
    return days
