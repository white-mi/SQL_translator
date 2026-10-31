"""Baseline candidate: SQLGlot transpile, statement by statement.

Runner contract (non-model harness): stdin {"input": "<json string>", "assets_dir": "/assets", ...},
stdout {"answer": "<translated script>"}. input = {"direction": "gp2trino"|"trino2gp", "script": "..."}.

The candidate never crashes: any failure falls back to the source statement, so one bad example
cannot stop an evaluation series (a crash stops the whole closed series in the arena).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DIALECTS = {"gp2trino": ("postgres", "trino"), "trino2gp": ("trino", "postgres")}


def translate_statement(statement: str, read: str, write: str) -> str:
    import sqlglot
    from sqlglot.errors import ErrorLevel

    out = sqlglot.transpile(statement, read=read, write=write, unsupported_level=ErrorLevel.IGNORE)
    text = ";\n".join(s for s in out if s.strip())
    return text or statement


def translate(script: str, direction: str) -> str:
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
