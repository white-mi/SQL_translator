"""Baseline candidate: SQLGlot transpile, statement by statement.

Runner contract (non-model harness): stdin {"input": "<json string>", "assets_dir": "/assets", ...},
stdout {"answer": "<translated script>"}. input = {"direction": "gp2trino"|"trino2gp", "script": "..."}.

The candidate never crashes: a statement SQLGlot fails on is kept as written, and any other
failure returns the source script unchanged (or an empty answer if the input cannot be read).
The runner image turns a crash or timeout of solve.py into an answer that scores 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# `from sqlsplit import ...` resolves to the file next to solve.py, whatever the cwd or flags
sys.path.insert(0, str(Path(__file__).resolve().parent))

# direction -> (SQLGlot read dialect, SQLGlot write dialect); Greenplum is handled as "postgres"
DIALECTS = {"gp2trino": ("postgres", "trino"), "trino2gp": ("trino", "postgres")}


def translate_statement(statement: str, read: str, write: str) -> str:
    """Return one statement transpiled by SQLGlot from dialect `read` to dialect `write`.

    Unsupported-feature checks are off (ErrorLevel.IGNORE), so constructs the target dialect
    lacks are emitted anyway.
    Several output statements are joined with semicolons; no output returns `statement`
    unchanged. SQLGlot errors, such as a parse error, propagate to the caller.
    """
    import sqlglot
    from sqlglot.errors import ErrorLevel

    out = sqlglot.transpile(statement, read=read, write=write, unsupported_level=ErrorLevel.IGNORE)
    text = ";\n".join(s for s in out if s.strip())
    return text or statement


def translate(script: str, direction: str) -> str:
    """Return `script` translated for `direction`, one statement at a time.

    The script is split with sqlsplit.split_statements; a statement on which translate_statement
    raises is kept as written. The parts are joined with ";" and a blank line and end with ";";
    a script without statements gives "". Raises KeyError for a direction not in DIALECTS.
    """
    from sqlsplit import split_statements

    read, write = DIALECTS[direction]
    parts = []
    for statement in split_statements(script):
        try:
            parts.append(translate_statement(statement, read, write))
        except Exception:  # noqa: BLE001 - keep the source statement on any transpiler error
            parts.append(statement)
    return ";\n\n".join(parts) + (";\n" if parts else "")


def main() -> None:
    """Read the runner payload from stdin and print {"answer": ...} to stdout.

    Any error while reading or translating is swallowed: the answer is then the source script,
    or "" if the input could not be read.
    """
    answer = ""
    try:
        payload = json.load(sys.stdin)
        task = json.loads(payload["input"])
        answer = task.get("script", "")
        answer = translate(answer, task["direction"])
    except Exception:  # noqa: BLE001 - always produce a valid answer object
        pass
    sys.stdout.write(json.dumps({"answer": answer}, ensure_ascii=False))


if __name__ == "__main__":
    main()
