"""Dialect-agnostic SQL text utilities: lexing into code/literal segments and statement splitting.

The lexer understands single-quoted strings ('' escapes), PostgreSQL escape strings (E'..'),
dollar-quoted strings ($tag$..$tag$), double-quoted identifiers, line comments and nested block
comments. Rewrites are applied only to code segments, so string literals are never touched.

engines.py executes the statements of split_statements() one by one; rewrite.py works on
segments(). case/ways/baseline/sqlsplit.py is a copy for the baseline candidate, so a change
here does not reach it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


@dataclass(frozen=True)
class Segment:
    """One piece of SQL text from segments(): code, a string literal or a comment.

    Attributes:
        kind: "code", "string" or "comment"; double-quoted identifiers are part of code.
        text: the exact source text of the piece.
    """

    kind: str  # "code", "string", "comment"
    text: str


def segments(sql: str) -> list[Segment]:
    """Return `sql` cut into code, string and comment segments, in source order.

    Joining the texts gives back `sql` exactly. An unterminated string, dollar quote or block
    comment runs to the end of the text.
    """
    out: list[Segment] = []
    buf: list[str] = []
    i, n = 0, len(sql)

    def flush() -> None:
        """Turn the pending code characters into one code segment."""
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
    """Split a script into statements on semicolons outside string literals and comments.

    Statements come back stripped and without their semicolons. Comment-only fragments are
    dropped; a comment inside a statement or before it is kept with that statement.
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
    """Apply fn (str -> str) to every code segment and return the reassembled text."""
    return "".join(fn(s.text) if s.kind == "code" else s.text for s in segments(sql))


def code_only(sql: str) -> str:
    """Return the text with literals and comments blanked out (same length is not preserved).

    Each literal or comment becomes a space and the segments are joined with spaces, so offsets
    do not match `sql`: use it for searching code, not for rewriting it.
    """
    return " ".join(s.text if s.kind == "code" else " " for s in segments(sql))
