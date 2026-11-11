"""Canonical form of result tables, shared by references and scoring.

Two tables are equal when they have the same column names (case-insensitive, order ignored) and
the same multiset of rows after value normalization. Column types are not compared: every value
becomes a canonical string (value()), so the integer 1, the float 1.0 and the text '1' are equal.
Numbers are rounded half-up to 6 decimal places, not compared with a tolerance.
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
    """Return a Decimal rounded half-up to 6 places as text without trailing zeros.

    Zero, including -0, gives "0"; a value too large to round is printed unrounded.
    """
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
    """Return the canonical text of one cell value, as returned by a database driver.

    None gives NULL (the text \\N), booleans "t" or "f", numbers go through _number (NaN and
    infinities spelled out). Datetimes with a time zone are converted to UTC and printed without
    it; times drop their zone without conversion; intervals become seconds; bytes become hex;
    arrays "[a,b]" and dicts sorted JSON, both recursively. Anything else, text included, is
    str(v) unchanged.
    """
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
    """Return sorted lowercase column names and sorted normalized row strings.

    A row string is the row's values (value()) in sorted-column order, joined by the 0x1F
    separator character.
    """
    cols = [c.lower() for c in columns]
    order = sorted(range(len(cols)), key=lambda i: cols[i])
    lines = ["\x1f".join(value(row[i]) for i in order) for row in rows]
    lines.sort()
    return [cols[i] for i in order], lines


def table_hash(columns: list[str], rows: list[tuple]) -> str:
    """Return the SHA-256 hex digest of the table's canonical form.

    Tables equal under this module's rules get the same digest. evaluate.run_on_states hashes
    reference and answer tables with it, so references store only digests and row counts.
    """
    cols, lines = canonical(columns, rows)
    digest = hashlib.sha256()
    digest.update("\x1f".join(cols).encode())
    digest.update(b"\n")
    for line in lines:
        digest.update(line.encode())
        digest.update(b"\n")
    return digest.hexdigest()
