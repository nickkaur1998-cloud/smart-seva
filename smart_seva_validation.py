"""Strict input validation helpers for Smart Seva.

These functions deliberately reject NaN/infinity, malformed dates, and values
outside explicit operational limits before the values reach the database.
"""
import math
from datetime import date, datetime


def finite_number(value, *, minimum=None, maximum=None, field="Value"):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a valid number.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number.")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field} must be no more than {maximum}.")
    return number


def bounded_integer(value, *, minimum=None, maximum=None, field="Value"):
    text = str(value).strip() if value is not None else ""
    if not text or not text.lstrip("+-").isdigit():
        raise ValueError(f"{field} must be a whole number.")
    number = int(text)
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field} must be no more than {maximum}.")
    return number


def iso_date(value, *, field="Date"):
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid YYYY-MM-DD date.") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{field} must be a valid YYYY-MM-DD date.")
    return parsed


def hhmm_time(value, *, field="Time"):
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%H:%M").time()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid 24-hour time.") from exc
    if parsed.strftime("%H:%M") != text:
        raise ValueError(f"{field} must be a valid 24-hour time.")
    return parsed
