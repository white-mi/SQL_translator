"""Command line: python -m sqltr [--local] <command> ...

  setup-pg          create the unprivileged runner role in PostgreSQL
  load-states       (re)load the data states s1-s4 into PostgreSQL and/or Trino
  build-dataset     generate instances of one split (train: public seed, public/private: operator seeds)
  build-holdout     write the hand-written holdout pairs as instances (operator only)
  build-references  execute source scripts and write the arena dataset (dataset.jsonl) + meta.jsonl
  self-check        compare Greenplum and Trino renderings on their engines (VM, needs Trino)
  run-local         run a candidate over a split and score it with the metric

Usual order: setup-pg, load-states, build-dataset, build-references, then run-local. Each command
is one `cmd_*` function. Engine addresses come from `case/metric/metric_config.json` (compose
hostnames `postgres` and `trino`) and the `SQLTR_*` variables; the global `--local` flag,
given before the command, points both engines at 127.0.0.1.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import sqltr  # noqa: F401  (adds the metric package to sys.path)
from sqltr_metric import config


def _cfg(args) -> config.Config:
    """Load the metric config and apply the global `--local` flag and run-local's `--no-cache`.

    `--local` sets both the PostgreSQL and the Trino host to 127.0.0.1 and wins over
    `SQLTR_PG_HOST` and `SQLTR_TRINO_HOST`; ports and credentials still come from
    `metric_config.json` and `SQLTR_*`. A namespace without one of the attributes keeps the default.
    """
    cfg = config.load()
    if getattr(args, "local", False):
        cfg.postgres.host = "127.0.0.1"
        cfg.trino.host = "127.0.0.1"
    if getattr(args, "no_cache", False):
        cfg.cache = False
    return cfg


def _read_jsonl(path: Path) -> list[dict]:
    """Return the objects of a UTF-8 JSON Lines file, skipping blank lines."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    """Write rows as UTF-8 JSON Lines, replacing the file and creating its parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _seed(args, split: str) -> tuple[int, str]:
    """Return `(seed, secret)` for build-dataset from `--seed` and `--seed-file`.

    The secret salts the hashed example ids of non-train splits, so an id does not reveal its
    family.

    - train with `--seed`: that seed and an empty secret (train ids are readable anyway);
    - otherwise `--seed-file` wins: `seeds[split]` and `seeds["secret"]` (or "") from the
      operator's JSON file `{"public": int, "private": int, "secret": str}`; a split without a
      key raises KeyError;
    - otherwise `--seed` with the secret "dev": a development run whose ids are not secret.

    Raises:
        SystemExit: neither option was given.
    """
    if split == "train" and args.seed is not None:
        return args.seed, ""
    if args.seed_file:
        seeds = json.loads(Path(args.seed_file).read_text())
        return int(seeds[split]), seeds.get("secret", "")
    if args.seed is not None:
        return args.seed, "dev"
    raise SystemExit("public/private need --seed-file with operator seeds (or --seed for a dev-only run)")


def cmd_setup_pg(args) -> None:
    """Create the unprivileged runner role in PostgreSQL (`setup-pg`).

    Needs PostgreSQL reachable as the configured admin user, a superuser. Idempotent: an existing
    role keeps its password; its temp_file_limit is set every time. Also revokes CREATE on schema public from PUBLIC and grants the runner CONNECT and
    TEMPORARY on the database (`warehouse.setup_postgres_roles`). Run it before load-states, whose
    grants name this role. Prints one confirmation line.
    """
    import psycopg

    from sqltr import warehouse

    cfg = _cfg(args)
    pg = cfg.postgres
    with psycopg.connect(host=pg.host, port=pg.port, dbname=pg.dbname, user=pg.admin_user,
                         password=pg.admin_password or None, autocommit=True) as conn:
        warehouse.setup_postgres_roles(conn, pg.runner_user, pg.runner_password or None)
    print("runner role ready")


def cmd_load_states(args) -> None:
    """Load the synthetic data states into the engines chosen by `--engine` (`load-states`).

    For each state of `cfg.states` (s1-s4, or `SQLTR_STATES`) drops and recreates the schema
    `retail_<state>` and fills it with `warehouse.generate_state`. `--engine` is postgres (the
    default), trino or both. PostgreSQL needs the admin connection and the runner role from
    setup-pg, which gets read access; Trino needs the admin user and writes to the configured
    catalog. Prints row counts per state and table, and the elapsed seconds for Trino.
    """
    from sqltr import warehouse

    cfg = _cfg(args)
    if args.engine in ("postgres", "both"):
        import psycopg

        pg = cfg.postgres
        with psycopg.connect(host=pg.host, port=pg.port, dbname=pg.dbname, user=pg.admin_user,
                             password=pg.admin_password or None, autocommit=True) as conn:
            counts = warehouse.load_postgres(conn, cfg.states, pg.runner_user)
        print("postgres:", json.dumps(counts))
    if args.engine in ("trino", "both"):
        from trino import dbapi

        tr = cfg.trino
        conn = dbapi.connect(host=tr.host, port=tr.port, user=tr.admin_user, catalog=tr.catalog,
                             http_scheme="http")
        started = time.monotonic()
        counts = warehouse.load_trino(conn, cfg.states, tr.catalog)
        print(f"trino ({time.monotonic() - started:.0f}s):", json.dumps(counts))


def cmd_build_dataset(args) -> None:
    """Generate one split into `<out>/instances.jsonl` (`build-dataset`).

    Needs no database. `--families` families are sampled from the seed that `_seed` picks; each
    gets `--variants` style variants in both directions, so the file holds
    2 x families x variants instances. Families found in the `--exclude` instance files are never
    sampled, which keeps public and private disjoint from train. Overwrites the file and prints the
    instance and family counts and the instances per template. The file holds the oracle
    translations, so it is operator data and stays out of the researcher workspace.
    """
    from sqltr.gen.build import build_split, read_instances, write_instances

    seed, secret = _seed(args, args.split)
    exclude: set[str] = set()
    for path in args.exclude or []:
        exclude |= {i.family for i in read_instances(Path(path))}
    instances = build_split(args.split, args.families, seed, variants=args.variants, exclude=exclude,
                            secret=secret)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_instances(instances, out / "instances.jsonl")
    templates: dict[str, int] = {}
    for inst in instances:
        templates[inst.template] = templates.get(inst.template, 0) + 1
    print(f"{len(instances)} instances, {len({i.family for i in instances})} families -> {out}")
    print(json.dumps(templates, sort_keys=True))


def cmd_build_holdout(args) -> None:
    """Write the hand-written holdout pairs to `<out>/instances.jsonl` (`build-holdout`).

    Needs no database. Each script pair of `sqltr/holdout.py` becomes two instances, one per
    direction. The holdout validates outside the arena search and never reaches the researcher
    (ADR 0009). Continue with build-references and self-check on the same directory.
    """
    from sqltr.gen.build import write_instances
    from sqltr.holdout import holdout_instances

    instances = holdout_instances()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_instances(instances, out / "instances.jsonl")
    print(f"{len(instances)} holdout instances ({len(instances) // 2} script pairs) -> {out}")


def cmd_build_references(args) -> None:
    """Build the arena dataset of a split from its instances (`build-references`).

    Reads `<split-dir>/instances.jsonl` and runs every reference script on every data state
    (`references.build_references`). Needs PostgreSQL with the runner role and the states loaded
    (setup-pg, load-states). With `--trino` Trino sources run on Trino, which must be reachable
    with its states loaded; without it the trino2gp references come from the paired Greenplum
    rendering on PostgreSQL, valid only for families that self-check found equal.

    Writes to the split directory: `dataset.jsonl` (arena examples), `meta.jsonl` (family,
    template, direction, features, reference engine and row counts per example; it stays out of the
    researcher workspace) and `references_report.json` (built count, skipped instances with the
    reason, count per reference mode, states, seconds). An instance whose reference script fails is
    skipped, not fatal; an engine fault raises `InfraError`.
    """
    from sqltr.gen.build import read_instances
    from sqltr.references import build_references

    cfg = _cfg(args)
    split_dir = Path(args.split_dir)
    instances = read_instances(split_dir / "instances.jsonl")
    started = time.monotonic()
    examples, meta, report = build_references(instances, cfg, trino=args.trino)
    _write_jsonl(split_dir / "dataset.jsonl", examples)
    _write_jsonl(split_dir / "meta.jsonl", meta)
    (split_dir / "references_report.json").write_text(json.dumps(
        {"built": report.built, "skipped": report.skipped, "engines": dict(report.engines),
         "states": cfg.states, "seconds": round(time.monotonic() - started, 1)}, ensure_ascii=False, indent=2))
    print(f"built {report.built}, skipped {len(report.skipped)}, engines {dict(report.engines)}")


def cmd_self_check(args) -> None:
    """Check that the paired Greenplum and Trino renderings give equal outputs (`self-check`).

    Reads `<split-dir>/instances.jsonl` and, once per family and variant, runs the Greenplum
    rendering on PostgreSQL and the Trino rendering on Trino over every state
    (`references.self_check`). Needs both engines reachable with the states loaded. Writes
    `<split-dir>/self_check.json` and prints the counts; the split passes when `equal` equals
    `checked`. Mismatching or failing families are only reported, not removed.
    """
    from sqltr.gen.build import read_instances
    from sqltr.references import self_check

    cfg = _cfg(args)
    split_dir = Path(args.split_dir)
    result = self_check(read_instances(split_dir / "instances.jsonl"), cfg)
    (split_dir / "self_check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"checked {result['checked']}, equal {result['equal']}, mismatch {len(result['mismatch'])}, "
          f"errors {len(result['errors'])}")


def cmd_run_local(args) -> None:
    """Run a candidate over a split and score every answer with the metric (`run-local`).

    The arena evaluation loop without the arena (`runlocal.evaluate_split`). Reads
    `dataset.jsonl` and `meta.jsonl` from `--split-dir` (build-references). Options:
    `--solver` is the candidate's `solve.py` (by default the baseline, relative to the current
    directory, so run from the repository root); `--directions` is a comma-separated filter such
    as `trino2gp`; `--limit` keeps the first N examples after that filter; `--workers` is the
    number of examples run in parallel threads.

    Needs the target engine of each chosen direction (PostgreSQL for trino2gp, Trino for gp2trino)
    with the states loaded, and Linux or WSL: the candidate runs with cwd `/tmp`, so on native
    Windows every example fails as `solver_failure`. Scores are cached in PostgreSQL
    (`sqltr_meta.score_cache`) unless `--no-cache` or `SQLTR_CACHE=0`.

    Writes `<out>/raw/results.jsonl` (one row per example, with the answer) and
    `<out>/summary.json` (`runlocal.summarize` plus `wall_seconds`) and prints the headline
    numbers. An engine fault raises `InfraError` and nothing is written.
    """
    from sqltr.runlocal import evaluate_split, summarize
    from sqltr_metric.evaluate import PgCache

    cfg = _cfg(args)
    split_dir = Path(args.split_dir)
    dataset = _read_jsonl(split_dir / "dataset.jsonl")
    meta = {m["id"]: m for m in _read_jsonl(split_dir / "meta.jsonl")}
    cache = PgCache(cfg) if cfg.cache else None
    started = time.monotonic()
    results = evaluate_split(dataset, meta, Path(args.solver), cfg, workers=args.workers,
                             directions=set(args.directions.split(",")) if args.directions else None,
                             limit=args.limit, cache=cache)
    summary = summarize(results)
    summary["wall_seconds"] = round(time.monotonic() - started, 1)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out / "raw" / "results.jsonl", results)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({k: summary[k] for k in ("examples", "score", "by_direction", "failures", "wall_seconds")},
                     ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    """Parse the command line and run the chosen command.

    Global options go before the command (`python -m sqltr --local run-local ...`); `--local`
    points both engines at 127.0.0.1 (see `_cfg`). Each subparser stores its `cmd_*` function
    as `args.fn`, which receives the whole namespace. argparse exits with status 2 on bad
    arguments; errors inside a command propagate as exceptions.

    Args:
        argv: arguments without the program name; None reads `sys.argv`.
            `case/dataset/generate.py` passes its own list.
    """
    parser = argparse.ArgumentParser(prog="sqltr")
    parser.add_argument("--local", action="store_true", help="engines on 127.0.0.1 instead of compose hosts")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup-pg").set_defaults(fn=cmd_setup_pg)

    p = sub.add_parser("load-states")
    p.add_argument("--engine", choices=["postgres", "trino", "both"], default="postgres")
    p.set_defaults(fn=cmd_load_states)

    p = sub.add_parser("build-dataset")
    p.add_argument("--split", choices=["train", "public", "private", "dev"], required=True)
    p.add_argument("--families", type=int, required=True)
    p.add_argument("--variants", type=int, default=1)
    p.add_argument("--seed", type=int)
    p.add_argument("--seed-file")
    p.add_argument("--exclude", nargs="*", help="instances.jsonl of other splits whose families are excluded")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_build_dataset)

    p = sub.add_parser("build-holdout", help="out-of-search validation pairs (sqltr/holdout.py)")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_build_holdout)

    p = sub.add_parser("build-references")
    p.add_argument("--split-dir", required=True)
    p.add_argument("--trino", action="store_true", help="execute Trino sources on Trino (VM mode)")
    p.set_defaults(fn=cmd_build_references)

    p = sub.add_parser("self-check")
    p.add_argument("--split-dir", required=True)
    p.set_defaults(fn=cmd_self_check)

    p = sub.add_parser("run-local")
    p.add_argument("--split-dir", required=True)
    p.add_argument("--solver", default="case/ways/baseline/solve.py")
    p.add_argument("--directions")
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_run_local)

    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
