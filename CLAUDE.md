# SQLTR — instructions for Claude Code

Course project (ITMO × DeepPavlov "Agentic LLM Systems", assignment B: a skill grown by evolution on the DeepPavlov arena). Own topic, approved 2026-10-31: translate multi-statement SQL scripts between PostgreSQL/Greenplum and Trino on Iceberg, in both directions. Course rules: `projects/README.md` and `projects/B-arena-skill.md` in the public repo `deeppavlov/agentic-course-itmo`.

## How we work

- Claude does most of the implementation; the user directs and must be able to defend every decision at the course defense. Explain the reasoning behind changes, not only the diff.
- Conversation in English. Repository docs (README, `docs/`, ADRs, `case/**/README.md`, `campaign.md`) stay in Russian: the course and the defense are in Russian. Code, comments and commit messages in English.
- Phrase advice as what to do, not as what to avoid.
- Small commits; run `python -m pytest -q` before every commit.
- `docs/repo-map.md` describes every tracked file and who may see it: update it in the same commit that adds, removes or repurposes a file.

## Hard rules

- Synthetic retail domain only. No employer names, internal system names, real work scripts, schemas or data anywhere: code, docs, commits, test fixtures. Writable schemas are `wrk` and `sandbox`.
- The arena has no license file: never copy arena code into this repo. Reference the pinned archive by SHA-256 (ADR 0001). Lab material from the arena's `archive/` (run reports, server names, paths, user names) never goes into the repo.
- Operator secrets never enter git: public/private seeds (`operator/`, `/etc/sqltr-arena/`), `.env` files, passwords, tokens, API keys. The repo is public from day one (course rule 0-6).
- The solver improves only through the arena search. Do not hand-tune `case/ways/` against train/public/private; the baseline stays as committed. Otherwise the experiment is invalid.
- The researcher workspace never contains the generator (`sqltr/gen`), oracle translations (`instances.jsonl`) or the holdout (`sqltr/holdout.py`) — ADR 0009, `case/deploy/arena.sh workspace`.
- The frozen metric (`case/metric`) changes only before `arena.sh freeze`; after the campaign starts it is immutable.

## Map

| Path | What |
| --- | --- |
| `case/metric/` | Metric package: `metric.json`, `score.py`, `sqltr_metric/` (execution on the target engine over 4 data states, canonical table hashes), tests |
| `case/harness/` | Arena runner image for candidates (`sqltr-runner:1`, `main.py`): a candidate crash becomes a 0 for that example instead of failing the private series (ADR 0008). Not an agent harness in the course sense (B-8) |
| `case/ways/baseline/` | Starting candidate: SQLGlot 30.27.0 per statement, never crashes |
| `case/acceptance/` | `run.py`: synthetic end-to-end acceptance of the arena deployment (19 checks). `full_train.py`: what a candidate costs on the full train split (operator only) |
| `case/deploy/` | Single-VM deployment: `operator.env.example`, `arena.sh` (layout, image, accept, freeze, data, journal, workspace, start, readiness, firewall, researcher-image, gateway, researcher) |
| `case/researcher/` | Researcher image (Hermes plus the arena CLI), its loop `run.py`, the model gateway that alone holds the provider key (ADR 0007), a fake provider, `rehearsal.sh`, `real_check.py` |
| `case/campaign/campaign.md` | Campaign conditions (draft until preparation) |
| `sqltr/gen/` | Generator: 14 templates with paired GP/Trino renderers (operator only) |
| `sqltr/holdout.py` | 12 hand-written pairs outside the generator (operator only) |
| `sqltr/warehouse.py`, `sqltr/references.py` | Synthetic warehouse with states s1–s4; references and self-check |
| `stand/` | Docker Compose stand: PostgreSQL 16.4 + Trino 483 on Iceberg; `vm-setup.sh`, `bootstrap.sh` |
| `docs/adr/` | ADR 0001–0009 |
| `results/` | Committed summaries, indexed in `results/README.md`; raw logs stay out of git; `archive/` holds the superseded local measurements of 2026-11-04 |

## How the pieces connect

1. `build-dataset` (`sqltr/gen`): families → `<out>/instances.jsonl`, each instance with `source_script` and `oracle_script`. All variants and both directions of a family land in one split.
2. `build-references` (`sqltr/references.py`): runs every source script on its source engine over states s1–s4 and writes `dataset.jsonl` (arena format: `id`, `input`, `reference` with output-table hashes) plus `meta.jsonl` (family, template, features; `run-local` uses it for breakdowns, and it stays out of the researcher workspace).
3. Evaluation: the arena evaluator runs the candidate's `solve.py` in the runner image, then `case/metric/score.py`. `python -m sqltr run-local` is the same loop without the arena.

Inside the metric (`case/metric/sqltr_metric/`):

- One code path builds references and scores answers: `evaluate.run_on_states` runs a script on every state and hashes each output table (`normalize.table_hash`, independent of row and column order). An answer scores 1 only if every output table matches the reference on every state, otherwise 0.
- Greenplum is emulated by PostgreSQL 16: `rewrite.gp_shim` strips Greenplum storage and distribution clauses before execution, and `rewrite.gp_incompatible` rejects what PostgreSQL accepts but Greenplum 7 does not (currently `MERGE`).
- Isolation: `rewrite.isolate` maps the logical schemas `retail`, `wrk`, `sandbox` (optionally behind `iceberg.`) to per-run physical schemas such as `retail_s1` and `wrk_<run>_<state>`; answers execute as `sqltr_runner` with statement timeouts. The static `rewrite.outside_writes` check runs first, database grants back it up, and `case/metric/tests/test_score.py` covers both.
- A defect of the translation is a 0 with the reason in `statistics.failure`; an engine or configuration fault raises `InfraError` and `score.py` exits non-zero (ADR 0002, ADR 0008).
- Without Trino, trino2gp references use paired mode: the oracle Greenplum rendering runs on PostgreSQL. This is valid only for families that `self-check` on the VM has confirmed equivalent.

## Easy to break

- `sqltr_metric` lives in `case/metric/`, not at the top level. In a plain checkout it imports only because `sqltr/__init__.py` and `conftest.py` put `case/metric` on `sys.path`; a script that uses it imports `sqltr` first.
- `case/ways/baseline/sqlsplit.py` is a copy of `case/metric/sqltr_metric/sqltext.py`, because a candidate package must be self-contained; a fix in one does not reach the other. The crash marker `-- sqltr-harness-error:` is defined twice, as `MARKER` in `case/harness/main.py` and `HARNESS_FAULT` in `sqltr_metric/evaluate.py`; change both together. `sqltr/runlocal.py` mirrors the runner protocol but records a crash as `solver_failure` rather than a marker.
- The metric cache (`sqltr_meta.score_cache` in PostgreSQL) is keyed by `METRIC_VERSION`, example id and answer. While changing the metric before freeze, run with `--no-cache` or `SQLTR_CACHE=0` so each answer is executed again instead of served from the cache.
- A Trino memory-limit error counts as a translation fault (`engines.py`, `_LIMITS`), so too small a limit gives silent zeros, not a failure. Trino's heap is set in `stand/trino/jvm.config` (60% of `mem_limit`, 2.4 GB; it also needs about 0.8 GB outside the heap). Rerun `self-check` after changing either; `equal` must equal `checked`.
- Researcher container, each found the hard way by `case/researcher/rehearsal.sh`: the gateway must be named `api.anthropic.com` inside the container (`--add-host`), or Hermes treats it as a relay and drops thinking blocks; the gateway token must start with `sk-ant-api`, or Hermes may take it for OAuth; the arena CLI's packages live in the system Python because the agent's terminal is a login shell that resets `PATH`; everything Hermes needs must be in the image, because the researcher has no internet. Details in `case/researcher/README.md`.
- `case/researcher/gateway.py` forwards the request body byte for byte: editing it would break prompt caching. Haiku 5.5 charges five times more above 100,000 prompt tokens, which is why `hermes-config.yaml` caps the window there; check `long_prompt` in `model-usage.jsonl`.
- `arena.sh` evaluator units have fixed names, so a rehearsal and the real campaign cannot run side by side; `rehearsal.sh` refuses once the campaign journal exists.
- `WORKSPACE_ALLOW` in `arena.sh` copies `README.md`, `case/README.md`, `case/campaign`, `case/dataset/README.md`, `docs/domain.md`, `docs/arena.md` and `docs/adr` to the researcher. Whatever is written there reaches the researcher as a hint, so results of experiments outside the campaign stay in `docs/poc.md` and `results/poc/`, with at most a bare link from those files.
- `case/metric/metric_config.json` points at the compose hostnames `postgres` and `trino`. Locally, override them with the `SQLTR_*` variables read in `sqltr_metric/config.py`, or pass the global `--local` flag before the subcommand: `python -m sqltr --local run-local ...`.

## Dev setup (Linux or WSL2)

The scripts are bash and the stand is Docker; on Windows work inside WSL2. Native Windows Python fails quietly: `run-local` starts each candidate with `cwd=/tmp`, which raises on Windows, so every example scores 0 as `solver_failure`.

```bash
cp stand/.env.example stand/.env                 # set both passwords
docker compose -f stand/compose.yaml --env-file stand/.env up -d postgres
set -a; . stand/.env; set +a
export SQLTR_PG_HOST=127.0.0.1
python -m venv .venv && . .venv/bin/activate && pip install -e . pytest
python -m sqltr --local setup-pg
python -m sqltr --local load-states --engine postgres
python -m sqltr build-dataset --split train --families 250 --seed 20261101 --out data/train
python -m sqltr --local build-references --split-dir data/train
python -m pytest -q                              # 73 tests
```

13 of the 73 tests need that PostgreSQL (the `pg_cfg` fixture in `conftest.py`): all of `case/metric/tests/test_score.py` and one each in `tests/test_generator.py` and `tests/test_holdout.py`. Without a reachable database they are skipped, not failed, so check the skip count. One test: `python -m pytest -q case/metric/tests/test_units.py::test_gp_shim_keeps_cte_with_clause`.

Full stand with Trino (VM, measured on 2 vCPU / 8 GB): `bash stand/bootstrap.sh`, see `stand/README.md`. Arena deployment: `case/deploy/README.md`.

Arena acceptance (needs Docker, `data/train`, the runner image, and the pinned arena checkout with its `.venv` plus the metric drivers: `../arena/.venv/bin/python -m pip install "psycopg[binary]==3.3.6" "trino==0.340.0"`):

```bash
docker build -f case/harness/runner.Dockerfile -t sqltr-runner:1 .
python case/acceptance/run.py --arena ../arena --work /tmp/sqltr-accept-1 --runner-image sqltr-runner:1 --local --directions trino2gp
```

## Status (2026-11-08)

- Stand and arena run on the VM (2 vCPU / 8 GB) with real Trino: self-check 250/250 train and 12/12 holdout; arena acceptance 19/19 (`results/acceptance/vm-2026-11-08.json`).
- Baseline: train gp2trino 0.012, trino2gp 0.116; holdout gp2trino 1/12, trino2gp 0/12.
- Candidate cost, full train through the arena on that VM (`results/stand/full-train-2026-11-08.json`): 17 minutes for a perfect candidate (score exactly 1.0), 11 for the baseline, 9 for a descendant with unchanged translations. `case/acceptance/full_train.py` repeats the measurement.
- Researcher route rehearsed end to end with a fake provider (`results/researcher/rehearsal-2026-11-08.json`). No real model call has gone through the gateway yet.
- Proof of concept outside the campaign, with a Haiku subagent as researcher (`docs/poc.md`, `results/poc/`): 0.064 to 0.402 on train in 3 candidates, about $0.015 per candidate. Its candidates stay out of this repository and out of the campaign.
- Real campaign not started: `freeze`, `data`, `journal` have run only in the rehearsal directory. Next: a model key or quota → `real_check.py` → a capped test of a real researcher → real `freeze` → `data` → `journal` → `workspace` → `start` → campaign (limit ≈ 50, K = 5, N = 3).
