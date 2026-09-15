"""
cron.py - Lightweight cron & grid expression evaluator for schedule orchestration.
Supports 5-field and 6-field (with seconds) cron expressions, aliases, and sub-second steps.
Computes next occurrence and missed occurrences for replay/catchup.
"""
from datetime import datetime, timedelta
import math
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from harness.core.codec import parse_duration


def parse_cron_field(field_str: str, min_val: int, max_val: int) -> Set[int]:
    """Parses a single cron field into a set of allowed integers."""
    allowed: Set[int] = set()
    s = field_str.strip()
    if s == "*":
        return set(range(min_val, max_val + 1))

    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("*/"):
            step = int(part[2:])
            allowed.update(range(min_val, max_val + 1, step))
        elif "/" in part:
            range_part, step_str = part.split("/", 1)
            step = int(step_str)
            if "-" in range_part:
                start, end = map(int, range_part.split("-", 1))
            else:
                start, end = int(range_part), max_val
            allowed.update(range(start, end + 1, step))
        elif "-" in part:
            start, end = map(int, part.split("-", 1))
            allowed.update(range(start, end + 1))
        else:
            allowed.add(int(part))

    return allowed


def parse_cron_expression(expr: str) -> Tuple[Dict[str, Any], Optional[Tuple[Set[int], ...]]]:
    """
    Normalizes a cron or step expression into parsed metadata dictionary
    and allowed sets for (second, minute, hour, day, month, dow).
    """
    s = expr.strip().lower()

    # Step shorthand (e.g. '*/100ms', '*/250ms', '*/5s')
    if s.startswith("*/") and ("ms" in s or "s" in s or "m" in s or "h" in s) and " " not in s:
        duration = parse_duration(s[2:])
        parsed = {"type": "step", "step_seconds": duration, "raw": expr}
        return parsed, None

    # Common aliases
    if s in ("@second", "second"):
        fields = ["0", "*", "*", "*", "*", "*", "*"]
    elif s in ("@minute", "minute"):
        fields = ["0", "0", "*", "*", "*", "*", "*"]
    elif s in ("@hourly", "hourly"):
        fields = ["0", "0", "0", "*", "*", "*", "*"]
    elif s in ("@daily", "daily", "@midnight", "midnight"):
        fields = ["0", "0", "0", "0", "*", "*", "*"]
    elif s in ("@weekly", "weekly"):
        fields = ["0", "0", "0", "0", "*", "*", "0"]
    else:
        parts = expr.strip().split()
        if len(parts) == 5:
            # Standard 5-field cron: prepend ms=0, second=0
            fields = ["0", "0"] + parts
        elif len(parts) == 6:
            # 6-field cron: prepend ms=0
            fields = ["0"] + parts
        elif len(parts) == 7:
            # Full 7-field sub-second cron: ms, sec, min, hour, day, month, dow
            fields = parts
        else:
            # Fallback treat as duration step
            try:
                dur = parse_duration(expr)
                return {"type": "step", "step_seconds": dur, "raw": expr}, None
            except Exception:
                fields = ["0", "0", "*", "*", "*", "*", "*"]

    # Parse sets
    milliseconds = parse_cron_field(fields[0], 0, 999)
    seconds = parse_cron_field(fields[1], 0, 59)
    minutes = parse_cron_field(fields[2], 0, 59)
    hours = parse_cron_field(fields[3], 0, 23)
    days = parse_cron_field(fields[4], 1, 31)
    months = parse_cron_field(fields[5], 1, 12)
    dows = parse_cron_field(fields[6], 0, 6)

    parsed = {
        "type": "cron",
        "raw": expr,
        "millisecond": fields[0] if fields[0] != "*" else "*",
        "second": fields[1] if fields[1] != "*" else "*",
        "minute": fields[2] if fields[2] != "*" else "*",
        "hour": fields[3] if fields[3] != "*" else "*",
        "day": fields[4] if fields[4] != "*" else "*",
        "month": fields[5] if fields[5] != "*" else "*",
        "dow": fields[6] if fields[6] != "*" else "*",
    }
    return parsed, (milliseconds, seconds, minutes, hours, days, months, dows)


def compute_next_occurrence(expr: str, after_epoch: Optional[float] = None) -> Tuple[float, Dict[str, Any]]:
    """
    Computes the next occurrence timestamp (epoch float) strictly > after_epoch.
    Returns (next_epoch, parsed_metadata).
    Supports 5, 6, and 7-field (sub-second millisecond) cron masks and step durations.
    """
    if after_epoch is None:
        after_epoch = time.time()

    parsed, field_sets = parse_cron_expression(expr)

    # Sub-second or duration step
    if field_sets is None or parsed.get("type") == "step":
        step = parsed.get("step_seconds", 1.0)
        if step <= 0.0:
            step = 1.0
        next_slot = math.ceil(after_epoch / step) * step
        if next_slot - after_epoch < 0.00001:
            next_slot += step
        return next_slot, parsed

    # Standard / 6-field / 7-field cron search
    milliseconds, seconds, minutes, hours, days, months, dows = field_sets
    sorted_ms = sorted(milliseconds)

    # Start search from the current second floor
    curr_sec = int(math.floor(after_epoch))
    curr_dt = datetime.fromtimestamp(curr_sec)

    # Search window up to 366 days
    for _ in range(86400 * 366):
        if curr_dt.month in months and curr_dt.day in days:
            # Python dow: Monday is 0, Sunday is 6. Cron: Sunday is 0, Saturday is 6.
            cron_dow = (curr_dt.weekday() + 1) % 7
            if cron_dow in dows and curr_dt.hour in hours and curr_dt.minute in minutes and curr_dt.second in seconds:
                sec_epoch = curr_dt.timestamp()
                for m in sorted_ms:
                    candidate = sec_epoch + (m / 1000.0)
                    if candidate > after_epoch + 1e-6:
                        return candidate, parsed

        # Fast forward optimization
        if curr_dt.month not in months:
            if curr_dt.month == 12:
                curr_dt = curr_dt.replace(year=curr_dt.year + 1, month=1, day=1, hour=0, minute=0, second=0)
            else:
                curr_dt = curr_dt.replace(month=curr_dt.month + 1, day=1, hour=0, minute=0, second=0)
            continue
        elif curr_dt.day not in days or ((curr_dt.weekday() + 1) % 7) not in dows:
            curr_dt = (curr_dt + timedelta(days=1)).replace(hour=0, minute=0, second=0)
            continue
        elif curr_dt.hour not in hours:
            curr_dt = (curr_dt + timedelta(hours=1)).replace(minute=0, second=0)
            continue
        elif curr_dt.minute not in minutes:
            curr_dt = (curr_dt + timedelta(minutes=1)).replace(second=0)
            continue

        curr_dt += timedelta(seconds=1)

    return after_epoch + 1.0, parsed


def compute_missed_occurrences(
    expr: str,
    start_epoch: float,
    end_epoch: float,
    max_count: int = 100,
) -> List[float]:
    """
    Computes all scheduled occurrences strictly between start_epoch and end_epoch.
    Useful for catchup replay upon worker resume.
    """
    if start_epoch >= end_epoch:
        return []

    occurrences: List[float] = []
    curr = start_epoch

    while len(occurrences) < max_count:
        nxt, _ = compute_next_occurrence(expr, after_epoch=curr)
        if nxt > end_epoch:
            break
        occurrences.append(nxt)
        curr = nxt

    return occurrences
