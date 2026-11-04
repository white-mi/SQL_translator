"""Synthetic acceptance of the arena deployment for the SQLTR case.

Starts throwaway train/public/private evaluators as host processes (the arena's "service without a
model" mode), drives a miniature campaign through the arena's own CLI and records what happened.
It never touches the real campaign: own work directory, own tokens, own journal, synthetic data
generated with dev seeds that are not the operator seeds.

    python case/acceptance/run.py --arena /opt/arena --work /srv/sqltr-accept --runner-image sqltr-runner:1

Needs: a pinned arena checkout with its venv (.venv) where the metric drivers are installed,
Docker with the runner image, PostgreSQL with loaded states (SQLTR_PG_* env), and for gp2trino
examples Trino (SQLTR_TRINO_* env). Without Trino use --directions trino2gp.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACCEPT_SEEDS = {"public": 910001, "private": 910002}  # dev seeds, never the operator seeds


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.arena = Path(args.arena).resolve()
        self.python = str(self.arena / ".venv" / "bin" / "python")
        self.work = Path(args.work).resolve()
        self.checks: list[dict] = []
        self.procs: dict[str, subprocess.Popen] = {}
        self.ports: dict[str, int] = {}

    # ------------------------------------------------------------------ helpers
    def arena_cli(self, *argv, ok=True) -> tuple[int, str]:
        proc = subprocess.run([self.python, "-m", *map(str, argv)], cwd=self.arena, capture_output=True,
                              text=True, timeout=self.args.cli_timeout)
        if ok and proc.returncode != 0:
            raise RuntimeError(f"{' '.join(map(str, argv[:3]))}: {proc.returncode} {proc.stderr.strip()[-400:]}")
        return proc.returncode, (proc.stdout if proc.returncode == 0 else proc.stderr).strip()

    def check(self, name: str, passed: bool, detail) -> None:
        self.checks.append({"check": name, "passed": bool(passed), "detail": detail})
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: {json.dumps(detail, ensure_ascii=False)[:300]}", flush=True)

    def token(self, name: str) -> Path:
        path = self.work / "secrets" / f"{name}-token"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_urlsafe(32) + "\n")
            path.chmod(0o600)
        return path

    # ------------------------------------------------------------------ preparation
    def prepare_data(self) -> None:
        data = self.work / "data"
        directions = set(self.args.directions.split(","))
        if not (ROOT / "data" / "train" / "dataset.jsonl").exists():
            raise SystemExit("build train first: python -m sqltr build-dataset --split train ... (see README)")
        sq = [sys.executable, "-m", "sqltr"] + (["--local"] if self.args.local else [])
        raw = self.work / "raw"
        excluded = [str(ROOT / "data" / "train" / "instances.jsonl")]
        for split in ("public", "private"):
            out = raw / split
            subprocess.run(sq + ["build-dataset", "--split", split, "--families", str(self.args.families),
                                 "--seed", str(ACCEPT_SEEDS[split]), "--exclude", *excluded, "--out", str(out)],
                           cwd=ROOT, check=True, capture_output=True)
            excluded.append(str(out / "instances.jsonl"))
            subprocess.run(sq + ["build-references", "--split-dir", str(out)] + (["--trino"] if "gp2trino" in directions else []),
                           cwd=ROOT, check=True, capture_output=True)
        sources = {"train": ROOT / "data" / "train" / "dataset.jsonl",
                   "public": raw / "public" / "dataset.jsonl", "private": raw / "private" / "dataset.jsonl"}
        for split, src in sources.items():
            rows = [json.loads(line) for line in src.read_text().splitlines() if line.strip()]
            rows = [r for r in rows if json.loads(r["input"])["direction"] in directions]
            if split == "train":
                rows = rows[: self.args.train_examples]
            (data / split).mkdir(parents=True, exist_ok=True)
            (data / split / "dataset.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        self.data = data

    def freeze_metric(self, name: str) -> Path:
        src = self.work / f"{name}-src"
        if src.exists():
            shutil.rmtree(src)
        src.mkdir(parents=True)
        metric = ROOT / "case" / "metric"
        for item in ("metric.json", "score.py", "metric_config.json"):
            shutil.copy2(metric / item, src / item)
        shutil.copytree(metric / "sqltr_metric", src / "sqltr_metric", ignore=shutil.ignore_patterns("__pycache__"))
        out = self.work / "srv" / name
        self.arena_cli("evaluator.prepare", "freeze", "--metric", src, "--output", out)
        self.arena_cli("evaluator.prepare", "verify", "--metric", out)
        return out

    def candidates(self) -> dict[str, Path]:
        ways = self.work / "ways"
        base = ways / "baseline"
        shutil.copytree(ROOT / "case" / "ways" / "baseline", base, ignore=shutil.ignore_patterns("__pycache__"))
        reg = ways / "registry"
        shutil.copytree(base, reg)
        (reg / "solution.json").write_text('{"harness": "sqltr-program", "parameters": {}}\n')
        (ways / "broken").mkdir()
        (ways / "broken" / "solve.py").write_text("import sys\nsys.stdin.read()\nraise SystemExit(3)\n")
        (ways / "empty").mkdir()
        (ways / "empty" / "solve.py").write_text('import json, sys\njson.load(sys.stdin)\nprint(json.dumps({"answer": ""}))\n')
        return {p.name: p for p in ways.iterdir()}

    # ------------------------------------------------------------------ services
    def start(self, name: str, kind: str, env: dict) -> str:
        port = self.args.base_port + len(self.ports)
        self.ports[name] = port
        srv = self.work / "srv"
        state, usage = srv / f"state-{name}", srv / f"usage-{name}"
        for d in (state, usage):
            d.mkdir(parents=True, exist_ok=True)
            d.chmod(0o700)
        (srv / "assets").mkdir(parents=True, exist_ok=True)
        (srv / "assets").chmod(0o755)
        full = {k: v for k, v in os.environ.items() if not k.startswith("ARENA_")}
        full.update({
            "ARENA_RUNNER_IMAGE": self.args.runner_image, "ARENA_USES_MODEL": "false",
            "ARENA_RUNNER_NETWORK": "none", "ARENA_MODEL": "", "ARENA_MODEL_BASE_URL": "",
            "ARENA_ASSETS_DIR": str(srv / "assets"), "ARENA_RUNNER_TIMEOUT": "600",
            "ARENA_HARNESSES": json.dumps({"sqltr-program": {"image": self.args.runner_image,
                                                              "uses_model": False, "network": "none"}}),
            "ARENA_STATE_DIR": str(state), "ARENA_USAGE_DIR": str(usage),
            "ARENA_TOKEN_FILE": str(self.token(name)), "ARENA_RELEASE_ID": f"sqltr-accept-{name}",
            "ARENA_SEARCH_MODE": "standalone",
        })
        full.update(env)
        if kind == "train":
            code = ("import uvicorn; from evaluator.train import TrainSettings, create_app; "
                    f"uvicorn.run(create_app(TrainSettings.from_env()), host='127.0.0.1', port={port}, access_log=False)")
        else:
            code = ("import uvicorn; from evaluator.service import Settings, create_app; "
                    f"uvicorn.run(create_app(Settings.from_env()), host='127.0.0.1', port={port}, access_log=False)")
        log = open(self.work / "logs" / f"{name}.log", "w")
        self.procs[name] = subprocess.Popen([self.python, "-c", code], cwd=self.arena, env=full,
                                            stdout=log, stderr=subprocess.STDOUT)
        url = f"http://127.0.0.1:{port}"
        for _ in range(120):
            try:
                urllib.request.urlopen(url + "/healthz", timeout=2)
                return url
            except OSError:
                time.sleep(0.5)
        raise RuntimeError(f"service {name} did not start, see {log.name}")

    def stop(self, name: str) -> None:
        proc = self.procs.pop(name, None)
        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=20)

    def stop_all(self) -> None:
        for name in list(self.procs):
            self.stop(name)

    # ------------------------------------------------------------------ flows
    def train(self, url, token, journal, cand, measurement, dataset_id, conditions, example_ids=None) -> dict:
        extra = [x for e in (example_ids or []) for x in ("--example-id", e)]
        _, out = self.arena_cli("evaluator.train", "submit", "--url", url, "--token-file", token, "--journal", journal,
                                "--candidate", cand, "--measurement", measurement, "--dataset", dataset_id,
                                "--conditions", conditions, "--train-data", self.data / "train" / "dataset.jsonl", *extra)
        evaluation = json.loads(out)["evaluation_id"]
        deadline = time.monotonic() + self.args.wait_timeout
        while time.monotonic() < deadline:
            _, out = self.arena_cli("evaluator.train", "status", "--url", url, "--token-file", token,
                                    "--evaluation-id", evaluation)
            if json.loads(out)["status"] != "RUNNING":
                break
            time.sleep(2)
        result_file = self.work / "results" / f"{measurement}.json"
        result_file.parent.mkdir(parents=True, exist_ok=True)
        _, out = self.arena_cli("evaluator.train", "result", "--url", url, "--token-file", token, "--journal", journal,
                                "--evaluation-id", evaluation, "--measurement", measurement, "--output", result_file)
        return json.loads(out)

    def wait(self, url, token, output, evaluation_id=None) -> tuple[int, dict]:
        extra = ["--evaluation-id", evaluation_id] if evaluation_id else []
        code, out = self.arena_cli("evaluator.client", "wait", "--url", url, "--token-file", token, *extra,
                                   "--poll-seconds", "2", "--output", output, ok=False)
        return code, (json.loads(Path(output).read_text()) if Path(output).exists() else {})

    def private_campaign(self, name, metric, finalists_ids, ways, max_solutions, extra_env=None) -> dict:
        """Separate private instance: own journal with `max_solutions` proposals, K = len(finalists_ids)."""
        srv = self.work / "srv" / name
        (srv / "search").mkdir(parents=True, exist_ok=True)
        (srv / "search-control").mkdir(parents=True, exist_ok=True)
        journal = srv / "search" / "search.sqlite3"
        self.arena_cli("evaluator.search", "--journal", journal, "init", "--max-solutions", max_solutions)
        url = self.start(name, "private", {
            "ARENA_DATASET_PATH": str(self.data / "private" / "dataset.jsonl"), "ARENA_METRIC_PATH": str(metric),
            "ARENA_SPLIT": "private", "ARENA_SEARCH_MODE": "campaign", "ARENA_SEARCH_JOURNAL": str(journal),
            "ARENA_SEARCH_MAX_SOLUTIONS": str(max_solutions),
            "ARENA_SEARCH_STOP_PATH": str(srv / "search-control" / "stop.json"),
            "ARENA_FINALISTS": str(len(finalists_ids)), "ARENA_OUTPUT_COUNT": "1", **(extra_env or {})})
        parent = None
        for cid in finalists_ids:
            self.arena_cli("evaluator.search", "--journal", journal, "propose", "--id", cid, "--directory", ways[cid],
                           *(["--parent", parent] if parent else []), "--hypothesis", "acceptance",
                           "--change", "acceptance", "--tokens", 0, "--seconds", 0)
            parent = parent or cid
        bundle = self.pack(name, journal, finalists_ids)
        self.arena_cli("evaluator.search", "--journal", journal, "finalize", "--bundle", bundle)
        token = self.token(name)
        self.arena_cli("evaluator.client", "submit", "--url", url, "--token-file", token, "--bundle", bundle)
        code, status = self.wait(url, token, self.work / "results" / f"{name}-status.json")
        record = next((self.work / "srv").glob(f"state-{name}/*/record.json"), None)
        rec = json.loads(record.read_text()) if record else {}
        return {"wait_exit": code, "api": status, "status": rec.get("status"), "winners": rec.get("winners"),
                "scores": [(s["candidate_id"], s["score"]) for s in rec.get("scores") or []],
                "failure": rec.get("failure")}

    def pack(self, tag, journal, ids) -> Path:
        frozen = self.work / "frozen" / tag
        args = []
        for cid in ids:
            target = frozen / cid
            if not target.exists():
                self.arena_cli("evaluator.search", "--journal", journal, "restore", "--candidate", cid,
                               "--directory", target)
            args += ["--candidate", f"{cid}={target}"]
        bundle = self.work / "bundles" / f"{tag}-{'-'.join(ids)}.json"
        bundle.parent.mkdir(parents=True, exist_ok=True)
        self.arena_cli("evaluator.client", "pack", *args, "--output", bundle)
        return bundle

    # ------------------------------------------------------------------ scenario
    def run(self) -> int:
        if self.work.exists() and any(self.work.iterdir()):
            raise SystemExit(f"{self.work} must be new or empty")
        (self.work / "logs").mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            self.prepare_data()
            metric = self.freeze_metric("metric")
            ways = self.candidates()
            srv = self.work / "srv"
            (srv / "search").mkdir(parents=True, exist_ok=True)
            (srv / "search-control").mkdir(parents=True, exist_ok=True)
            journal = srv / "search" / "search.sqlite3"
            ds = {"ARENA_METRIC_PATH": str(metric)}
            train_url = self.start("train", "train", {**ds, "ARENA_DATASET_PATH": str(self.data / "train" / "dataset.jsonl"),
                                                      "ARENA_CAMPAIGN_ID": "sqltr-accept", "ARENA_CASE_ID": "sqltr",
                                                      "ARENA_DATASET_ID": "train-accept", "ARENA_CONDITIONS_ID": "accept",
                                                      "ARENA_TRAIN_TRACKING": "false"})
            public_url = self.start("public", "public", {**ds, "ARENA_DATASET_PATH": str(self.data / "public" / "dataset.jsonl"),
                                                         "ARENA_SPLIT": "public"})
            private_env = {**ds, "ARENA_DATASET_PATH": str(self.data / "private" / "dataset.jsonl"),
                           "ARENA_SPLIT": "private", "ARENA_SEARCH_MODE": "campaign", "ARENA_SEARCH_JOURNAL": str(journal),
                           "ARENA_SEARCH_MAX_SOLUTIONS": "4", "ARENA_SEARCH_STOP_PATH": str(srv / "search-control" / "stop.json"),
                           "ARENA_FINALISTS": "2", "ARENA_OUTPUT_COUNT": "1"}
            private_url = self.start("private", "private", private_env)
            tok = {n: self.token(n) for n in ("train", "public", "private")}
            ready = ["evaluator.client", "search-readiness", "--url", private_url, "--token-file", tok["private"],
                     "--require-campaign", "--max-solutions", 4, "--finalists", 2, "--output-count", 1,
                     "--release-id", "sqltr-accept-private"]

            code, out = self.arena_cli(*ready, ok=False)
            self.check("A01 readiness refused before journal init", code != 0, out[-120:])
            self.arena_cli("evaluator.search", "--journal", journal, "init", "--max-solutions", 4)
            code, out = self.arena_cli(*ready, ok=False)
            self.check("A02 readiness after init: campaign gate on", code == 0, json.loads(out) if code == 0 else out)
            code, out = self.arena_cli("evaluator.client", "search-readiness", "--url", public_url,
                                       "--token-file", tok["public"], ok=False)
            self.check("A03 public does not claim the private gate", code == 0 and not json.loads(out)["gate_enabled"],
                       json.loads(out) if code == 0 else out)

            meta = {"--hypothesis": "acceptance", "--change": "acceptance", "--tokens": 0, "--seconds": 0}
            flat = [str(x) for kv in meta.items() for x in kv]
            self.arena_cli("evaluator.search", "--journal", journal, "propose", "--id", "baseline",
                           "--directory", ways["baseline"], *flat)
            for cid in ("registry", "broken", "empty"):
                self.arena_cli("evaluator.search", "--journal", journal, "propose", "--id", cid,
                               "--directory", ways[cid], "--parent", "baseline", *flat)
            code, out = self.arena_cli("evaluator.search", "--journal", journal, "propose", "--id", "extra",
                                       "--directory", ways["baseline"], "--parent", "baseline", *flat, ok=False)
            self.check("A04 proposal limit enforced", code != 0 and "limit" in out, out[-120:])

            results = {}
            for cid in ("baseline", "registry", "broken", "empty"):
                r = self.train(train_url, tok["train"], journal, cid, f"{cid}-train", "train-accept", "accept")
                kinds: dict[str, int] = {}
                for e in r.get("examples") or []:
                    f = (e.get("statistics") or {}).get("failure")
                    k = "ok" if not f else (f.get("kind") if isinstance(f, dict) else f)
                    kinds[k] = kinds.get(k, 0) + 1
                results[cid] = {"status": r["status"], "score": (r.get("metrics") or {}).get("score"),
                                "harness": r["params"].get("harness"), "examples": len(r.get("examples") or []),
                                "failures": kinds, "solver_seconds": (r.get("metrics") or {}).get("solver_seconds"),
                                "judge_seconds": (r.get("metrics") or {}).get("judge_seconds")}
            self.check("A05 train: baseline measured with per-example diagnostics",
                       results["baseline"]["status"] == "FINISHED" and results["baseline"]["examples"] > 0, results["baseline"])
            self.check("A06 train: registered harness selected by solution.json",
                       results["registry"]["harness"] == "sqltr-program"
                       and results["registry"]["score"] == results["baseline"]["score"], results["registry"])
            self.check("A07 train: crashing candidate scored 0 with candidate_fault (crash-tolerant harness)",
                       results["broken"]["status"] == "FINISHED" and results["broken"]["score"] == 0.0
                       and set(results["broken"]["failures"]) == {"candidate_fault"}, results["broken"])
            self.check("A08 train: empty answers scored 0, not an error",
                       results["empty"]["score"] == 0.0 and set(results["empty"]["failures"]) == {"empty_answer"},
                       results["empty"])

            bundle = self.pack("public", journal, ["baseline"])
            self.arena_cli("evaluator.search", "--journal", journal, "begin", "--id", "baseline-public",
                           "--candidate", "baseline", "--split", "public", "--dataset", "public-accept",
                           "--subset", "all", "--conditions", "accept", "--job", public_url)
            _, out = self.arena_cli("evaluator.client", "submit", "--url", public_url, "--token-file", tok["public"],
                                    "--bundle", bundle)
            evaluation = json.loads(out)["evaluation_id"]
            code, status = self.wait(public_url, tok["public"], self.work / "results" / "public-baseline.json", evaluation)
            score = (status.get("scores") or [{}])[0].get("score")
            self.arena_cli("evaluator.search", "--journal", journal, "finish", "--id", "baseline-public", "--score", score)
            self.check("A09 public returns only the aggregate score",
                       code == 0 and set(status) == {"status", "evaluation_id", "scores"}
                       and set(status["scores"][0]) == {"candidate_id", "score"}, status)

            k2 = self.pack("private", journal, ["baseline", "registry"])
            k1 = self.pack("private", journal, ["baseline"])
            code, out = self.arena_cli("evaluator.client", "submit", "--url", private_url, "--token-file", tok["private"],
                                       "--bundle", k2, ok=False)
            self.check("A10 private refused before finalize (no attempt spent)", code != 0, out[-80:])
            self.arena_cli("evaluator.search", "--journal", journal, "finalize", "--bundle", k1)
            code, out = self.arena_cli("evaluator.client", "submit", "--url", private_url, "--token-file", tok["private"],
                                       "--bundle", k1, ok=False)
            self.check("A11 private refuses a bundle with the wrong K", code != 0, out[-80:])
            self.arena_cli("evaluator.search", "--journal", journal, "finalize", "--bundle", k2, "--replace")
            code, out = self.arena_cli("evaluator.client", "submit", "--url", private_url, "--token-file", tok["private"],
                                       "--bundle", k2, ok=False)
            self.check("A12 private accepts the finalized K bundle", code == 0, out)
            code, out = self.arena_cli("evaluator.client", "submit", "--url", private_url, "--token-file", tok["private"],
                                       "--bundle", k1, ok=False)
            self.check("A13 a different bundle is refused after acceptance", code != 0, out[-80:])
            code, status = self.wait(private_url, tok["private"], self.work / "results" / "private-status.json")
            self.check("A14 private API exposes status only", code == 0 and status == {"status": "complete"}, status)
            record = json.loads(next(srv.glob("state-private/*/record.json")).read_text())
            self.check("A15 operator record holds scores, winners and the search admission",
                       record["status"] == "complete" and record["winners"]
                       and record["search_admission"]["finalization"]["batch_digest"],
                       {"winners": record["winners"], "scores": [(s["candidate_id"], s["score"]) for s in record["scores"]]})
            self.stop("private")
            private_url = self.start("private-restarted", "private",
                                     {**private_env, "ARENA_STATE_DIR": str(srv / "state-private"),
                                      "ARENA_USAGE_DIR": str(srv / "usage-private"),
                                      "ARENA_TOKEN_FILE": str(tok["private"]),
                                      "ARENA_RELEASE_ID": "sqltr-accept-private"})
            _, out = self.arena_cli("evaluator.client", "status", "--url", private_url, "--token-file", tok["private"])
            code2, out2 = self.arena_cli("evaluator.client", "submit", "--url", private_url, "--token-file",
                                         tok["private"], "--bundle", k2, ok=False)
            self.check("A16 private state survives restart; identical resubmit is idempotent",
                       json.loads(out) == {"status": "complete"} and code2 == 0 and json.loads(out2)["status"] == "complete",
                       [json.loads(out), out2])

            crash = self.private_campaign("private-crash", metric, ["baseline", "broken"], ways, 2)
            self.check("A17 crashing finalist scores 0, series completes",
                       crash["status"] == "complete" and dict(crash["scores"]).get("broken") == 0.0, crash)
            infra = self.private_campaign("private-infra", metric, ["baseline", "registry"], ways, 2,
                                          {"SQLTR_PG_PORT": "1", "SQLTR_TRINO_PORT": "1"})
            self.check("A18 engine outage fails the series loudly (no zero scores, no winners)",
                       infra["status"] == "failed" and not infra["winners"] and infra["api"] == {"status": "failed"},
                       infra)
            _, out = self.arena_cli("evaluator.search", "--journal", journal, "status")
            self.check("A19 journal consistent after the campaign", json.loads(out)["finalization"] is not None
                       and json.loads(out)["running"] == [], json.loads(out))
        finally:
            self.stop_all()
        report = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "seconds": round(time.monotonic() - started),
                  "runner_image": self.args.runner_image, "directions": self.args.directions,
                  "passed": sum(c["passed"] for c in self.checks), "total": len(self.checks), "checks": self.checks}
        (self.work / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(f"{report['passed']}/{report['total']} checks passed in {report['seconds']} s -> {self.work / 'report.json'}")
        return 0 if report["passed"] == report["total"] else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--arena", required=True, help="pinned arena checkout with .venv")
    p.add_argument("--work", required=True, help="new empty directory for this acceptance run")
    p.add_argument("--runner-image", default="sqltr-runner:1")
    p.add_argument("--directions", default="trino2gp,gp2trino")
    p.add_argument("--families", type=int, default=10, help="families per public/private acceptance split")
    p.add_argument("--train-examples", type=int, default=30)
    p.add_argument("--base-port", type=int, default=18181)
    p.add_argument("--local", action="store_true", help="engines on 127.0.0.1 (sqltr --local)")
    p.add_argument("--cli-timeout", type=float, default=900)
    p.add_argument("--wait-timeout", type=float, default=1800)
    return Acceptance(p.parse_args()).run()


if __name__ == "__main__":
    raise SystemExit(main())
