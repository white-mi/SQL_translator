"""Text rewrites and static checks the trusted measurer applies to a script before running it.

Rewrites: schema isolation (isolate) and the Greenplum storage shim (gp_shim). Static checks:
writes outside the writable schemas (outside_writes) and constructs Greenplum 7 rejects
(gp_incompatible). All of them are regex-based and look at code segments only
(sqltext.segments); string literals and comments are never changed or matched.
"""

from __future__ import annotations

import re

from .sqltext import segments

# ---------------------------------------------------------------- placeholders for literals


def _protect(sql: str) -> tuple[str, list[str]]:
    """Replace string literals and comments by placeholders so regexes can span code safely.

    Returns (text with NUL-delimited numbered placeholders, the replaced originals); pass both to
    _restore() to put the originals back.
    """
    saved: list[str] = []
    parts: list[str] = []
    for seg in segments(sql):
        if seg.kind == "code":
            parts.append(seg.text)
        else:
            saved.append(seg.text)
            parts.append(f"\x00{len(saved) - 1}\x00")
    return "".join(parts), saved


def _restore(text: str, saved: list[str]) -> str:
    """Return `text` with the placeholders made by _protect() replaced by the saved originals."""
    return re.sub(r"\x00(\d+)\x00", lambda m: saved[int(m.group(1))], text)


# ---------------------------------------------------------------- schema isolation


def isolate(sql: str, mapping: dict[str, str]) -> str:
    """Rewrite qualified references `schema.` (optionally `iceberg.schema.`) via mapping.

    mapping keys are logical schema names (lowercase), values are physical schema names.
    Quoted identifiers ("wrk") are matched too; the replacement is emitted unquoted.

    Matching ignores case and keeps an `iceberg.` catalog prefix. A name is replaced only when a
    dot follows it and no other name part precedes it, so unqualified table names stay as they
    are; being a text rewrite, it also replaces an alias or column qualifier spelled like a
    logical schema. Called by evaluate.run_on_states for every state.
    """
    if not mapping:
        return sql
    names = "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True))
    pattern = re.compile(
        r'(?<![\w$."])'  # not part of a longer identifier or a deeper path
        r'(?:(?P<cq>"?)(?P<cat>iceberg)(?P=cq)\.)?'
        r'(?P<q>"?)(?P<schema>' + names + r')(?P=q)\.',
        re.IGNORECASE,
    )

    def repl(m: re.Match) -> str:
        """Return the physical replacement for one match, keeping a catalog prefix."""
        cat = f"{m.group('cat')}." if m.group("cat") else ""
        return f"{cat}{mapping[m.group('schema').lower()]}."

    protected, saved = _protect(sql)
    return _restore(pattern.sub(repl, protected), saved)


# ---------------------------------------------------------------- Greenplum storage shim

_GP_STORAGE_KEYS = (
    "appendonly",
    "appendoptimized",
    "orientation",
    "compresstype",
    "compresslevel",
    "blocksize",
    "checksum",
)
_RE_DISTRIBUTED_BY = re.compile(r"\bDISTRIBUTED\s+BY\s*\([^)]*\)", re.IGNORECASE)
_RE_DISTRIBUTED_OTHER = re.compile(r"\bDISTRIBUTED\s+(?:RANDOMLY|REPLICATED)\b", re.IGNORECASE)
_RE_STORAGE_WITH = re.compile(
    r"\bWITH\s*\(\s*(?:" + "|".join(_GP_STORAGE_KEYS) + r")\b[^)]*\)", re.IGNORECASE
)
_RE_GP_PARTITION = re.compile(r"\bPARTITION\s+BY\s+(?:RANGE|LIST)\s*\([^)]*\)\s*\(", re.IGNORECASE)


def _drop_balanced(text: str, start: int, open_idx: int) -> str:
    """Return `text` without the span from `start` to the ")" matching the "(" at `open_idx`.

    If that parenthesis is never closed, `text` is returned unchanged.
    """
    depth, j = 0, open_idx
    while j < len(text):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[:start] + text[j + 1 :]
        j += 1
    return text  # unbalanced: leave as is, the engine will report the error


def gp_shim(sql: str) -> str:
    """Remove Greenplum physical-storage clauses that do not change query results.

    DISTRIBUTED BY/RANDOMLY/REPLICATED, append-optimized storage options in WITH (...) and the
    classic inline partition specification are dropped so that Greenplum-dialect scripts run on
    PostgreSQL with identical results.

    WITH (...) is dropped only when its first option is a Greenplum storage key, and PARTITION
    BY only when an inline partition list follows it, so PostgreSQL's own declarative
    partitioning stays. Called by evaluate.prepare_script for every Greenplum-dialect script,
    references and answers alike.
    """
    text, saved = _protect(sql)
    text = _RE_DISTRIBUTED_BY.sub("", text)
    text = _RE_DISTRIBUTED_OTHER.sub("", text)
    text = _RE_STORAGE_WITH.sub("", text)
    while True:
        m = _RE_GP_PARTITION.search(text)
        if not m:
            break
        new = _drop_balanced(text, m.start(), m.end() - 1)
        if new == text:
            break
        text = new
    return _restore(text, saved)


# ---------------------------------------------------------------- static inspection

_RE_WRITE = re.compile(
    r"\b(?P<verb>CREATE\s+(?:(?:GLOBAL|LOCAL)\s+)?(?:TEMP|TEMPORARY|UNLOGGED)?\s*TABLE"
    r"(?:\s+IF\s+NOT\s+EXISTS)?|INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO"
    r"|DROP\s+TABLE(?:\s+IF\s+EXISTS)?|TRUNCATE(?:\s+TABLE)?|ALTER\s+TABLE(?:\s+IF\s+EXISTS)?"
    r"|CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW|CREATE\s+SCHEMA|DROP\s+SCHEMA)"
    r"\s+(?P<name>[\w$\".]+)",
    re.IGNORECASE,
)


def write_targets(sql: str) -> list[tuple[str, str]]:
    """Return (verb, name) for every write statement found in code segments.

    verb is upper case with single spaces ("INSERT INTO", "CREATE TEMP TABLE"); name is the
    target as written, lowercased, without double quotes, possibly qualified. A regex scan, not
    a parser: verbs are matched anywhere in the code, and statements outside its list (such as
    COPY or CREATE INDEX) are not reported. Database grants back it up.
    """
    text, _ = _protect(sql)
    found = []
    for m in _RE_WRITE.finditer(text):
        verb = " ".join(m.group("verb").upper().split())
        found.append((verb, m.group("name").replace('"', "").lower()))
    return found


def outside_writes(sql: str, allowed: set[str]) -> list[str]:
    """Return "VERB name" for each write to a schema outside `allowed`; an empty list means none.

    `allowed` holds logical schema names in lower case. CREATE SCHEMA and DROP SCHEMA are always
    reported; a catalog prefix (`iceberg.wrk.t`) is ignored; unqualified names pass, because
    they resolve to the default writable schema. score_answer scores a non-empty result 0
    before executing anything.
    """
    bad = []
    for verb, name in write_targets(sql):
        if verb.startswith(("CREATE SCHEMA", "DROP SCHEMA")):
            bad.append(f"{verb} {name}")
            continue
        parts = name.split(".")
        if len(parts) == 3:
            parts = parts[1:]
        if len(parts) == 2 and parts[0] not in allowed:
            bad.append(f"{verb} {name}")
    return bad


_RE_MERGE = re.compile(r"\bMERGE\s+INTO\b", re.IGNORECASE)


def gp_incompatible(sql: str) -> list[str]:
    """Return why `sql` would fail on Greenplum 7 although PostgreSQL 16 runs it; [] means no issue.

    Only MERGE INTO is checked. score_answer applies it to answers with a Greenplum target and
    scores a non-empty result 0 (failure "target_dialect_gate").
    """
    text, _ = _protect(sql)
    issues = []
    if _RE_MERGE.search(text):
        issues.append("MERGE is not supported by Greenplum 7")
    return issues
