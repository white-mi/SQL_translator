"""Reference builder and generator self-check (trusted operator code).

Reference = hashes of the output tables of the SOURCE script executed on the SOURCE engine on
every data state. It is computed once; translations are compared against it by the metric.

Modes for the trino2gp direction (source dialect is Trino):
- "trino": execute the Trino source on Trino (the campaign mode, on the VM);
- "paired": Trino is not available; execute the paired Greenplum rendering on PostgreSQL. Valid only
  after the self-check has confirmed that both renderings give equal results for this family.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from sqltr_metric.config import Config
from sqltr_metric.engines import make_engine
from sqltr_metric.evaluate import run_on_states

from .gen.build import Instance


@dataclass
class BuildReport:
    built: int = 0
    skipped: list[dict] = field(default_factory=list)
    engines: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def _hashes(runs, outputs):
    if not all(r.ok for r in runs):
        bad = next(r for r in runs if not r.ok)
        return None, f"{bad.state}: {bad.error_kind} {bad.error}"
    missing = [(r.state, n) for r in runs for n in outputs if r.hashes.get(n) is None]
    if missing:
        return None, f"missing output {missing[0]}"
    return {n: {"hashes": [r.hashes[n] for r in runs], "rows": [r.rows[n] for r in runs]} for n in outputs}, ""


def build_references(instances: list[Instance], cfg: Config, trino: bool) -> tuple[list[dict], list[dict], BuildReport]:
    """Return (arena examples, meta rows, report)."""
    report = BuildReport()
    engines = {"postgres": make_engine("postgres", cfg)}
    if trino:
        engines["trino"] = make_engine("trino", cfg)
    cache: dict[tuple[str, str], tuple[dict | None, str]] = {}
    examples, meta = [], []
    try:
        for inst in instances:
            if inst.source_dialect == "gp":
                mode, script, dialect = "postgres", inst.source_script, "gp"
            elif trino:
                mode, script, dialect = "trino", inst.source_script, "trino"
            else:
                mode, script, dialect = "postgres-paired", inst.oracle_script, "gp"
            key = (mode.split("-")[0], script)
            if key not in cache:
                engine = engines["trino" if dialect == "trino" else "postgres"]
                runs = run_on_states(script, dialect, inst.outputs, cfg, engine=engine)
                cache[key] = _hashes(runs, inst.outputs)
            outputs, error = cache[key]
            if outputs is None:
                report.skipped.append({"id": inst.id, "family": inst.family, "reason": error})
                continue
            reference = {"target": inst.target_dialect, "states": cfg.states, "outputs": outputs}
            examples.append({"id": inst.id,
                             "input": json.dumps({"direction": inst.direction, "script": inst.source_script},
                                                 ensure_ascii=False),
                             "reference": json.dumps(reference, ensure_ascii=False)})
            meta.append({"id": inst.id, "family": inst.family, "template": inst.template,
                         "direction": inst.direction, "features": inst.features, "reference_engine": mode,
                         "rows": {n: v["rows"] for n, v in outputs.items()}})
            report.built += 1
            report.engines[mode] += 1
    finally:
        for engine in engines.values():
            engine.close()
    return examples, meta, report


def self_check(instances: list[Instance], cfg: Config) -> dict:
    """Execute the Greenplum rendering on PostgreSQL and the Trino rendering on Trino per family.

    Families whose outputs differ on any state must be dropped from the dataset: their paired
    renderings are not equivalent, so neither direction would have a trustworthy reference.
    """
    pg, tr = make_engine("postgres", cfg), make_engine("trino", cfg)
    by_family: dict[tuple[str, int], Instance] = {}
    for inst in instances:
        by_family.setdefault((inst.family, inst.variant), inst)
    result = {"checked": 0, "equal": 0, "mismatch": [], "errors": []}
    try:
        for (family, variant), inst in sorted(by_family.items()):
            gp_script = inst.source_script if inst.source_dialect == "gp" else inst.oracle_script
            tr_script = inst.oracle_script if inst.source_dialect == "gp" else inst.source_script
            gp_runs = run_on_states(gp_script, "gp", inst.outputs, cfg, engine=pg)
            tr_runs = run_on_states(tr_script, "trino", inst.outputs, cfg, engine=tr)
            result["checked"] += 1
            gp_out, gp_err = _hashes(gp_runs, inst.outputs)
            tr_out, tr_err = _hashes(tr_runs, inst.outputs)
            if gp_out is None or tr_out is None:
                result["errors"].append({"family": family, "variant": variant, "gp": gp_err, "trino": tr_err})
            elif gp_out == tr_out:
                result["equal"] += 1
            else:
                diff = [n for n in inst.outputs if gp_out[n] != tr_out[n]]
                result["mismatch"].append({"family": family, "variant": variant, "tables": diff,
                                           "rows_gp": {n: gp_out[n]["rows"] for n in diff},
                                           "rows_trino": {n: tr_out[n]["rows"] for n in diff}})
    finally:
        pg.close()
        tr.close()
    return result
