"""Copy of case/metric/sqltr_metric/sqltext.py: the candidate package must be self-contained."""

from __future__ import annotations

import re
from dataclasses import dataclass

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


@dataclass(frozen=True)
class Segment:
    kind: str  # "code", "string", "comment"
    text: str


def segments(sql: str) -> list[Segment]:
    out: list[Segment] = []
    buf: list[str] = []
    i, n = 0, len(sql)

    def flush() -> None:
        if buf:
            out.append(Segment("code", "".join(buf)))
            buf.clear()

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            flush()
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append(Segment("comment", sql[i:j]))
            i = j
        elif ch == "/" and nxt == "*":
            flush()
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            out.append(Segment("comment", sql[i:j]))
            i = j
        elif ch in "eE" and nxt == "'" and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            flush()
            j = i + 2
            while j < n:
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(Segment("string", sql[i:j]))
            i = j
        elif ch == "'":
            flush()
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(Segment("string", sql[i:j]))
            i = j
        elif ch == "$":
            m = _DOLLAR_TAG.match(sql, i)
            if m and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
                flush()
                tag = m.group(0)
                end = sql.find(tag, m.end())
                j = n if end == -1 else end + len(tag)
                out.append(Segment("string", sql[i:j]))
                i = j
            else:
                buf.append(ch)
                i += 1
        elif ch == '"':
            # double-quoted identifiers stay in code: they are names and may need rewriting
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            i = j
        else:
            buf.append(ch)
            i += 1
    flush()
    return out


def split_statements(sql: str) -> list[str]:
    """Split a script into statements on semicolons outside literals and comments.

    Comment-only fragments are dropped. Comments inside a statement are kept.
    """
    statements: list[str] = []
    current: list[str] = []
    has_code = False
    for seg in segments(sql):
        if seg.kind != "code":
            current.append(seg.text)
            if seg.kind == "string":
                has_code = True
            continue
        parts = seg.text.split(";")
        for idx, part in enumerate(parts):
            current.append(part)
            if part.strip():
                has_code = True
            if idx < len(parts) - 1:
                if has_code:
                    statements.append("".join(current).strip())
                current, has_code = [], False
    if has_code:
        statements.append("".join(current).strip())
    return [s for s in statements if s]


def map_code(sql: str, fn) -> str:
    """Apply fn to every code segment and return the reassembled text."""
    return "".join(fn(s.text) if s.kind == "code" else s.text for s in segments(sql))


def code_only(sql: str) -> str:
    """Return the text with literals and comments blanked out (same length is not preserved)."""
    return " ".join(s.text if s.kind == "code" else " " for s in segments(sql))
