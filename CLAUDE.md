# SQLTR — instructions for Claude Code

Course project (ITMO × DeepPavlov "Agentic LLM Systems", assignment B: a skill grown by evolution on the DeepPavlov arena). Own topic, approved 2026-10-31: translate multi-statement SQL scripts between PostgreSQL/Greenplum and Trino on Iceberg, in both directions. Course rules: `projects/README.md` and `projects/B-arena-skill.md` in the public repo `deeppavlov/agentic-course-itmo`.

## How we work

- Claude does most of the implementation; the user directs and must be able to defend every decision at the course defense. Explain the reasoning behind changes, not only the diff.
- Conversation in English. Repository docs (README, `docs/`, ADRs, `case/**/README.md`, `campaign.md`) stay in Russian: the course and the defense are in Russian. Code, comments and commit messages in English.
- Phrase advice as what to do, not as what to avoid.
- Small commits; run `python -m pytest -q` before every commit.

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
| `case/acceptance/run.py` | Synthetic end-to-end acceptance of the arena deployment (19 checks) |
| `case/deploy/` | Single-VM deployment: `operator.env.example`, `arena.sh` (layout, image, accept, freeze, data, journal, workspace, start, readiness) |
| `case/campaign/campaign.md` | Campaign conditions (draft until checkpoint 1) |
| `sqltr/gen/` | Generator: 14 templates with paired GP/Trino renderers (operator only) |
| `sqltr/holdout.py` | 12 hand-written pairs outside the generator (operator only) |
| `sqltr/warehouse.py`, `sqltr/references.py` | Synthetic warehouse with states s1–s4; references and self-check |
| `stand/` | Docker Compose stand: PostgreSQL 16.4 + Trino 483 on Iceberg; `vm-setup.sh`, `bootstrap.sh` |
| `docs/adr/` | ADR 0001–0009 |
| `results/` | Committed summaries; raw logs stay out of git |

## Dev setup (Linux or WSL2)

The scripts are bash and the stand is Docker; on Windows work inside WSL2.

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
python -m pytest -q                              # 48 tests
```

Full stand with Trino (VM, 8 vCPU / 32 GB): `bash stand/bootstrap.sh`, see `stand/README.md`. Arena deployment: `case/deploy/README.md`.

Arena acceptance (needs Docker, `data/train`, the runner image, and the pinned arena checkout with its `.venv` plus the metric drivers: `../arena/.venv/bin/python -m pip install "psycopg[binary]==3.3.6" "trino==0.340.0"`):

```bash
docker build -f case/harness/runner.Dockerfile -t sqltr-runner:1 .
python case/acceptance/run.py --arena ../arena --work /tmp/sqltr-accept-1 --runner-image sqltr-runner:1 --local --directions trino2gp
```

## Status (2026-11-04)

- Metric, generator, baseline, stand, docs: done and tested locally on PostgreSQL. Trino side not yet run (needs the VM).
- Arena acceptance 19/19 in a sandbox (`results/acceptance/local-2026-11-04.json`).
- Baseline: train trino2gp 0.116; gp2trino ≤ 0.024 (offline upper bound); holdout trino2gp 0/12.
- Next: VM → `stand/bootstrap.sh` → `case/deploy/arena.sh` steps → checkpoint 1 numbers → campaign (limit ≈ 50, K = 5, N = 3).
