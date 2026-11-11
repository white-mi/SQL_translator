"""Reference builder and generator self-check (trusted operator code).

Reference = hashes and row counts of the output tables of the SOURCE script executed on the SOURCE
engine on every data state. It is computed once by `build_references`; the metric
(`sqltr_metric.evaluate.score_answer`) compares the tables of a translation against it.

Reference modes, recorded per example as `reference_engine` in meta.jsonl:
- "postgres": gp2trino; execute the Greenplum source on PostgreSQL (the Greenplum emulation);
- "trino": trino2gp; execute the Trino source on Trino (the campaign mode, on the VM);
- "postgres-paired": trino2gp when Trino is not available; execute the paired Greenplum rendering
  (the oracle script) on PostgreSQL. Valid only after the self-check has confirmed that both
  renderings give equal results for this family.

Both functions need the data states loaded on their engines (`python -m sqltr load-states`) and
raise `sqltr_metric.engines.InfraError` when an engine is unreachable or fails.
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
    """Summary of one `build_references` call; the CLI saves it as references_report.json.

    Attributes:
        built: number of examples produced.
        skipped: one `{"id", "family", "reason"}` per instance left out because its reference
            script failed on some state or did not create every output table.
        engines: examples per reference mode ("postgres", "trino", "postgres-paired"), despite
            the name.
    """

    built: int = 0
    skipped: list[dict] = field(default_factory=list)
    engines: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def _hashes(runs, outputs):
    """Collect the per-state hashes and row counts of every output table, or why they are unusable.

    Args:
        runs: `StateRun` list from `run_on_states`, one per state, in state order.
        outputs: output table names (`schema.table`) the script must create.

    Returns:
        `(tables, "")`, where tables maps each output name to `{"hashes": [...], "rows": [...]}`
        with one entry per run; or `(None, reason)` when a run failed (the first failing state,
        error kind and message) or an output table is missing.
    """
    if not all(r.ok for r in runs):
        bad = next(r for r in runs if not r.ok)
        return None, f"{bad.state}: {bad.error_kind} {bad.error}"
    missing = [(r.state, n) for r in runs for n in outputs if r.hashes.get(n) is None]
    if missing:
        return None, f"missing output {missing[0]}"
    return {n: {"hashes": [r.hashes[n] for r in runs], "rows": [r.rows[n] for r in runs]} for n in outputs}, ""


def build_references(instances: list[Instance], cfg: Config, trino: bool) -> tuple[list[dict], list[dict], BuildReport]:
    """Execute the reference script of every instance and return the arena dataset rows.

    The mode is chosen per instance (see the module docstring): Greenplum sources run on
    PostgreSQL; Trino sources run on Trino when `trino` is true, otherwise their paired Greenplum
    rendering runs on PostgreSQL. Runs are cached by (engine, script), so a paired trino2gp
    instance reuses the run of its gp2trino twin; the key leaves out the output list, which relies
    on paired renderings naming the same output tables. Opens one connection per engine and closes
    them before returning. Caller: `python -m sqltr build-references`, and the metric tests.

    Args:
        instances: from `sqltr.gen.build.read_instances` (instances.jsonl).
        cfg: metric config; the references cover `cfg.states`.
        trino: execute Trino sources on Trino (VM mode); Trino must be reachable with the states
            loaded.

    Returns:
        `(examples, meta, report)`. Each example is an arena row `{"id", "input", "reference"}`:
        `input` is a JSON string `{"direction", "script"}` and `reference` a JSON string
        `{"target", "states", "outputs"}` with per-state hashes and row counts per output table.
        `meta` has one row per example: family, template, direction, features, `reference_engine`
        (the mode) and row counts. An instance whose reference fails is left out of both lists and
        listed in `report.skipped`.

    Raises:
        InfraError: an engine is unreachable or fails.
    """
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
    """Execute the Greenplum rendering on PostgreSQL and the Trino rendering on Trino and compare.

    Runs once per (family, variant), over every state of `cfg.states`; the renderings are equal
    when every output table has the same hashes and row counts on every state. Needs both engines
    reachable with the states loaded. Caller: `python -m sqltr self-check`.

    The function only reports. A family whose outputs differ on any state must be dropped from the
    dataset: its paired renderings are not equivalent, so neither direction would have a
    trustworthy reference. A family that fails to run has no reference either.

    Returns:
        `{"checked", "equal", "mismatch", "errors"}`; the split passes when equal == checked.
        A mismatch entry has family, variant, the differing `tables` and their per-state row
        counts on each engine (`rows_gp`, `rows_trino`). An error entry has family, variant and
        the failure reason of each side under `gp` and `trino` ("" for a side that ran).

    Raises:
        InfraError: an engine is unreachable or fails.
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
