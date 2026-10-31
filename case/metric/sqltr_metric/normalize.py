"""Canonical form of result tables, shared by references and scoring.

Two tables are equal when they have the same column names (case-insensitive, order ignored) and
the same multiset of rows after value normalization. Numbers are compared at 1e-6 precision.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

NULL = "\\N"
_QUANT = Decimal("0.000001")


def _number(value: Decimal) -> str:
    try:
        q = value.quantize(_QUANT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return format(value, "f")
    if q == 0:
        return "0"
    text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def value(v: Any) -> str:
    if v is None:
        return NULL
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        return _number(Decimal(repr(v)))
    if isinstance(v, Decimal):
        if v.is_nan():
            return "nan"
        if v.is_infinite():
            return "inf" if v > 0 else "-inf"
        return _number(v)
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(dt.UTC).replace(tzinfo=None)
        text = v.isoformat(sep=" ")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, dt.time):
        text = v.replace(tzinfo=None).isoformat()
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
    if isinstance(v, dt.timedelta):
        return _number(Decimal(str(v.total_seconds())))
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(value(x) for x in v) + "]"
    if isinstance(v, dict):
        return json.dumps({str(k): value(x) for k, x in v.items()}, sort_keys=True, ensure_ascii=False)
    return str(v)


def canonical(columns: list[str], rows: list[tuple]) -> tuple[list[str], list[str]]:
    """Return sorted lowercase column names and sorted normalized row strings."""
    cols = [c.lower() for c in columns]
    order = sorted(range(len(cols)), key=lambda i: cols[i])
    lines = ["\x1f".join(value(row[i]) for i in order) for row in rows]
    lines.sort()
    return [cols[i] for i in order], lines


def table_hash(columns: list[str], rows: list[tuple]) -> str:
    cols, lines = canonical(columns, rows)
    digest = hashlib.sha256()
    digest.update("\x1f".join(cols).encode())
    digest.update(b"\n")
    for line in lines:
        digest.update(line.encode())
        digest.update(b"\n")
    return digest.hexdigest()
