"""Small, dependency-free helpers shared by providers and the UI."""

from __future__ import annotations

import base64
import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

UTC = timezone.utc

_ISO_FRACTION = re.compile(r"(\.\d+)")


def utcnow() -> datetime:
    return datetime.now(UTC)


def dig(obj: Any, *path: str | int, default: Any = None) -> Any:
    """Safely walk nested dicts/lists: ``dig(data, "a", 0, "b")``."""
    cur = obj
    for key in path:
        if isinstance(key, int):
            if not isinstance(cur, (list, tuple)) or not -len(cur) <= key < len(cur):
                return default
            cur = cur[key]
        else:
            if not isinstance(cur, Mapping) or key not in cur:
                return default
            cur = cur[key]
        if cur is None:
            return default
    return cur


def first(mapping: Mapping[str, Any] | None, *keys: str, default: Any = None) -> Any:
    """Return the first present, non-null value among ``keys``."""
    if not isinstance(mapping, Mapping):
        return default
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return default


def to_float(value: Any) -> float | None:
    """Parse numbers that may arrive as int, float or numeric strings."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
        if text.endswith("%"):
            text = text[:-1]
        if text.startswith("$"):
            text = text[1:]
        try:
            result = float(text)
        except ValueError:
            return None
    else:
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def to_int(value: Any) -> int | None:
    number = to_float(value)
    return None if number is None else int(number)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def percent(used: float | None, limit: float | None) -> float | None:
    """``used`` of ``limit`` as a 0..100+ percentage, or None when unknown."""
    if used is None or limit is None or limit <= 0:
        return None
    return used / limit * 100.0


def fraction_to_percent(value: Any) -> float | None:
    """Normalise utilisation that may be a 0..1 fraction or a 0..100 percent.

    Only use this where the upstream format is genuinely ambiguous; values
    of exactly 1.0 are treated as a fraction (100%).
    """
    number = to_float(value)
    if number is None:
        return None
    if 0.0 <= number <= 1.0:
        return number * 100.0
    return number


def parse_time(value: Any) -> datetime | None:
    """Parse ISO-8601 strings and epoch seconds/milliseconds into aware UTC."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return _from_epoch(float(text))
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Python < 3.11 only accepts 3 or 6 fractional digits.
    match = _ISO_FRACTION.search(text)
    if match:
        digits = match.group(1)[1:]
        text = text.replace(match.group(1), "." + (digits + "000000")[:6], 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _from_epoch(number: float) -> datetime | None:
    if number <= 0:
        return None
    # Heuristic: values above ~year 33658 in seconds are milliseconds.
    if number > 1e14:  # microseconds
        number /= 1e6
    elif number > 1e11:  # milliseconds
        number /= 1e3
    try:
        return datetime.fromtimestamp(number, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def in_seconds(now: datetime, seconds: Any) -> datetime | None:
    number = to_float(seconds)
    if number is None:
        return None
    return now + timedelta(seconds=max(0.0, number))


def decode_jwt_claims(token: str | None) -> dict[str, Any]:
    """Decode the (unverified) payload of a JWT. Returns {} on failure."""
    if not token or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_json(path) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def title_case_plan(value: str | None) -> str | None:
    """Turn ``"max_5x"`` / ``"PRO"`` / ``"pro-plus"`` into ``"Max 5x"`` / ``"Pro"``."""
    if not value or not isinstance(value, str):
        return None
    words = re.split(r"[\s_\-]+", value.strip())
    pretty = []
    for word in words:
        if not word:
            continue
        lower = word.lower()
        if re.fullmatch(r"\d+x", lower):
            pretty.append(lower)
        elif lower in {"ai", "api", "cli", "ide", "glm", "byok"}:
            pretty.append(lower.upper())
        else:
            pretty.append(lower.capitalize())
    return " ".join(pretty) or None


def iter_lines_reverse(path, chunk_size: int = 64 * 1024) -> Iterable[str]:
    """Yield the lines of a text file from last to first without reading it all."""
    with open(path, "rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        remainder = b""
        while position > 0:
            step = min(chunk_size, position)
            position -= step
            handle.seek(position)
            block = handle.read(step) + remainder
            lines = block.split(b"\n")
            remainder = lines.pop(0)
            for raw in reversed(lines):
                if raw.strip():
                    yield raw.decode("utf-8", errors="replace")
        if remainder.strip():
            yield remainder.decode("utf-8", errors="replace")
