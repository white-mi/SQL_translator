"""Command line: python -m sqltr <command> ...

  setup-pg          create the unprivileged runner role in PostgreSQL
  load-states       load the K data states into PostgreSQL and/or Trino
  build-dataset     generate instances of one split (train: public seed, public/private: operator seeds)
  build-references  execute source scripts and write the arena dataset (dataset.jsonl) + meta.jsonl
  self-check        compare Greenplum and Trino renderings on their engines (VM, needs Trino)
  run-local         run a candidate over a split and score it with the metric
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
    cfg = config.load()
    if getattr(args, "local", False):
        cfg.postgres.host = "127.0.0.1"
        cfg.trino.host = "127.0.0.1"
    if getattr(args, "no_cache", False):
        cfg.cache = False
    return cfg


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _seed(args, split: str) -> tuple[int, str]:
    if split == "train" and args.seed is not None:
        return args.seed, ""
    if args.seed_file:
        seeds = json.loads(Path(args.seed_file).read_text())
        return int(seeds[split]), seeds.get("secret", "")
    if args.seed is not None:
        return args.seed, "dev"
    raise SystemExit("public/private need --seed-file with operator seeds (or --seed for a dev-only run)")


def cmd_setup_pg(args) -> None:
    import psycopg

    from sqltr import warehouse

    cfg = _cfg(args)
    pg = cfg.postgres
    with psycopg.connect(host=pg.host, port=pg.port, dbname=pg.dbname, user=pg.admin_user,
                         password=pg.admin_password or None, autocommit=True) as conn:
        warehouse.setup_postgres_roles(conn, pg.runner_user, pg.runner_password or None)
    print("runner role ready")


def cmd_load_states(args) -> None:
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


def cmd_build_references(args) -> None:
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
    from sqltr.gen.build import read_instances
    from sqltr.references import self_check

    cfg = _cfg(args)
    split_dir = Path(args.split_dir)
    result = self_check(read_instances(split_dir / "instances.jsonl"), cfg)
    (split_dir / "self_check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"checked {result['checked']}, equal {result['equal']}, mismatch {len(result['mismatch'])}, "
          f"errors {len(result['errors'])}")


def cmd_run_local(args) -> None:
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
