#!/usr/bin/env bash
# Full dress rehearsal of the deployment with a fake model provider, in its own ROOT and with its own
# seeds: it spends no money and touches nothing of the real campaign. It runs the one-time deployment
# steps for real, then three tests of the researcher:
#   A  a scripted researcher does real work and probes what it can reach
#   B  context compression fires and the run survives the edited history
#   C  the dollar cap stops the agent and nothing reaches the provider
#
# Run as root on the VM, with /etc/sqltr-arena/operator.env in place:
#   bash case/researcher/rehearsal.sh results/researcher/rehearsal.json
set -uo pipefail
cd "$(dirname "$0")/../.."
REPORT=${1:?usage: rehearsal.sh report.json}
R=/srv/sqltr-rehearsal
E=/etc/sqltr-arena/rehearsal.env
A="bash case/deploy/arena.sh"
export SQLTR_OPERATOR_ENV=$E
export PATH="${SUDO_USER:+/home/$SUDO_USER/.local/bin:}$PATH"
step() { echo; echo "[$(date -u +%T)] === $*"; }
must() { "$@" || { echo "FAILED: $*"; exit 20; }; }

# the evaluator units have fixed names, so a rehearsal must not run next to the real campaign
if [ -e /srv/sqltr-arena/search/search.sqlite3 ]; then
  echo "the real campaign journal exists: stop here, the rehearsal would take over its evaluator units"; exit 21
fi

step "settings, from a clean directory"
[ -e "$E" ] && $A stop > /dev/null 2>&1
systemctl stop sqltr-fake-model 2>/dev/null
rm -rf "$R"
sed -e "s#^ROOT=.*#ROOT=$R#" -e 's/^CAMPAIGN_ID=.*/CAMPAIGN_ID=sqltr-rehearsal/' \
    -e 's/^MAX_SOLUTIONS=.*/MAX_SOLUTIONS=1/' -e 's/^FINALISTS=.*/FINALISTS=1/' -e 's/^OUTPUT_COUNT=.*/OUTPUT_COUNT=1/' \
    -e 's/^RELEASE_ID=.*/RELEASE_ID=sqltr-rehearsal/' \
    -e 's/^TRAIN_PORT=.*/TRAIN_PORT=18091/' -e 's/^PUBLIC_PORT=.*/PUBLIC_PORT=18092/' -e 's/^PRIVATE_PORT=.*/PRIVATE_PORT=18093/' \
    /etc/sqltr-arena/operator.env | grep -vE '^(GATEWAY_PORT|MODEL_MAX_USD|MODEL_CONFIG)=' > "$E"
cat >> "$E" <<EOF
GATEWAY_PORT=18094
MODEL_CONFIG=$R/gateway.fake.json
MODEL_MAX_USD=1
SEEDS_FILE=/etc/sqltr-arena/rehearsal-seeds.json
WORKSPACE_DIR=$R/workspace
PUBLIC_FAMILIES=3
PRIVATE_FAMILIES=3
EOF
chmod 600 "$E"
python3 -c "import json, secrets; print(json.dumps({'public': secrets.randbits(62), 'private': secrets.randbits(62), 'secret': secrets.token_hex(16)}))" > /etc/sqltr-arena/rehearsal-seeds.json
chmod 600 /etc/sqltr-arena/rehearsal-seeds.json
set -a; . "$E"; set +a

step "researcher image"
must $A researcher-image > /tmp/rehearsal-image.out 2>&1
docker images sqltr-researcher:1 --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}'

step "one-time steps: layout, freeze, data, journal, workspace"
for command in layout freeze data journal workspace; do
  $A "$command" > "/tmp/rehearsal-$command.out" 2>&1 || { echo "FAILED: $command"; tail -5 "/tmp/rehearsal-$command.out"; exit 20; }
  echo "$command ok"
done

step "evaluators and readiness"
must $A start > /tmp/rehearsal-start.out 2>&1
tail -3 /tmp/rehearsal-start.out | cut -c1-120
must $A readiness > /tmp/rehearsal-readiness.out 2>&1

step "fake provider and gateway"
python3 - "$R" <<'PY'
import json, sys
config = json.load(open("case/researcher/gateway.haiku-5-5.json"))
config["upstream"] = "http://127.0.0.1:18099"
json.dump(config, open(f"{sys.argv[1]}/gateway.fake.json", "w"))
PY
chown arena "$R/gateway.fake.json"
printf 'fake-key-for-rehearsal\n' > "$R/secrets/model-api-key"
must $A gateway > /tmp/rehearsal-gateway.out 2>&1

fake() {  # $1 log name, $2 script file, then extra arguments
  systemctl stop sqltr-fake-model 2>/dev/null
  local log="$R/logs/fake-$1.jsonl" script=$2; shift 2
  rm -f "$log"
  systemd-run -q --unit sqltr-fake-model --uid arena /usr/bin/python3 "$PWD/case/researcher/fake_model.py" \
    --script "$script" --log "$log" "$@"
  sleep 2
}
researcher() { $A researcher --prompt-file /workspace/case/campaign/campaign.md --stop-at-limit --toolsets terminal,file,todo; }

step "A: the scripted researcher does real work"
fake a "$PWD/case/researcher/rehearsal_script.json"
( while sleep 2; do docker stats --no-stream --format '{{.MemUsage}}' sqltr-researcher 2>/dev/null; done ) > "$R/logs/memory-a.log" &
sampler=$!
researcher > "$R/logs/researcher-a.out" 2>&1; rc_a=$?
kill "$sampler" 2>/dev/null
echo "exit $rc_a"; tail -2 "$R/logs/researcher-a.out" | cut -c1-200

step "B: compression stays under the 100k price line"
python3 -c "
import json
print(json.dumps([{'command': f'echo step {i}'} for i in range(32)] + [{'text': 'done'}]))" > "$R/logs/compress_script.json"
chown arena "$R/logs/compress_script.json"
fake b "$R/logs/compress_script.json" --inflate-at 28 --inflate-tokens 90000
researcher > "$R/logs/researcher-b.out" 2>&1; rc_b=$?
echo "exit $rc_b"; tail -2 "$R/logs/researcher-b.out" | cut -c1-200

step "C: the cap is reached"
systemctl stop sqltr-arena-gateway
cp "$E" "$E.saved"
sed -i 's/^MODEL_MAX_USD=.*/MODEL_MAX_USD=0.0000001/' "$E"
must $A gateway > /tmp/rehearsal-gateway.out 2>&1
fake c "$PWD/case/researcher/rehearsal_script.json"
started=$(date +%s)
timeout 300 $A researcher --prompt-file /workspace/case/campaign/campaign.md --stop-at-limit --toolsets terminal,file,todo > "$R/logs/researcher-c.out" 2>&1; rc_c=$?
seconds_c=$(( $(date +%s) - started ))
echo "exit $rc_c after $seconds_c s"
systemctl stop sqltr-arena-gateway
mv "$E.saved" "$E"

step "report"
"$ARENA_DIR/.venv/bin/python" -c "import sys; sys.path.insert(0, '$ARENA_DIR'); import runpy, sys as s; s.argv=['search','--journal','$R/search/search.sqlite3','export']; runpy.run_module('evaluator.search', run_name='__main__')" > "$R/logs/journal.json" 2>/dev/null
python3 - "$R" "$REPORT" "$rc_a" "$rc_b" "$rc_c" "$seconds_c" <<'PY'
import json, os, sys, time
from pathlib import Path

root, report, rc_a, rc_b, rc_c, seconds_c = Path(sys.argv[1]), Path(sys.argv[2]), *map(int, sys.argv[3:7])
logs = root / "logs"
def rows(name):
    path = logs / f"fake-{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
def tool_output(row):
    text = row.get("latest_tool_result") or ""
    try:
        return json.loads(text).get("output", text)
    except (ValueError, AttributeError):
        return text

checks = []
def check(name, passed, detail=None):
    checks.append({"check": name, "passed": bool(passed), "detail": detail})
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f": {detail}" if detail is not None else ""))

a = rows("a")
output = "\n".join(tool_output(r) for r in a)
journal = json.loads((logs / "journal.json").read_text())
measurement = (journal.get("measurements") or [{}])[0]
comparison = measurement.get("comparison") or {}
check("A01 the scripted researcher finished", rc_a == 0, rc_a)
check("A02 no request the real API would reject", a and not any(r["problems"] for r in a), f"{len(a)} requests")
check("A03 model, adaptive thinking and effort as configured",
      all(r["model"] == "claude-haiku-5-5" and r["thinking"]["type"] == "adaptive" and r["output_config"]["effort"] == "max" for r in a))
check("A04 thinking blocks go back on every step", all(r["thinking_replayed"] == r["messages"] // 2 for r in a))
check("A05 prompt cache markers on every request", all(r["cache_markers"] > 0 for r in a))
check("A06 the provider sees the operator key, not the client token", all(r["key"] == "fake-key-for-rehearsal" for r in a))
check("A07 generator, oracles, operator files and Docker socket are hidden",
      "VISIBLE" not in output and "hidden /workspace/sqltr" in output and "hidden /var/run/docker.sock" in output)
check("A08 the provider key is out of the researcher's reach", "nowhere in reach\n0\n" in output)
check("A09 no internet from the researcher", "https://pypi.org blocked" in output and "https://api.anthropic.com blocked" in output)
check("A10 engines and SSH closed to the researcher", all(f"host port {p} closed" in output for p in (5432, 8080, 22)))
check("A11 evaluator and gateway open to the researcher", "train reachable" in output and "gateway reachable" in output)
check("A12 the gateway refuses another model", "only claude-haiku-5-5 is allowed" in output)
check("A13 the gateway refuses a call without its token", "invalid client token" in output)
check("A14 the candidate and its measurement are in the journal, with the campaign's comparison key",
      len(journal.get("candidates") or []) == 1 and len(journal.get("measurements") or []) == 1
      and comparison.get("dataset_id") == "train-20261101-250" and comparison.get("conditions_id") == "sqltr-metric-v1-runner-1",
      comparison)
check("A15 search readiness works from inside the container", '"mode": "campaign"' in output and '"journal_ready": true' in output)

b = rows("b")
helper = next((i for i, r in enumerate(b) if not r["tools"]), None)
before = max((r["messages"] for r in b[:helper]), default=0) if helper is not None else 0
after = b[helper + 1]["messages"] if helper is not None and helper + 1 < len(b) else None
check("B01 the run survives compression", rc_b == 0, rc_b)
check("B02 compression fires and goes through the gateway on the same model",
      helper is not None and b[helper]["model"] == "claude-haiku-5-5")
check("B03 compression shrinks the conversation", after is not None and after < before / 2, {"before": before, "after": after})
rejected = [r for r in b if r["problems"]]
check("B04 at most one rejection, for the edited history, then recovery",
      len(rejected) <= 1 and all("Invalid `signature`" in p for r in rejected for p in r["problems"]) and b[-1]["messages"] > (after or 0),
      {"rejected": len(rejected)})

c = rows("c")
check("C01 the agent stops at the cap", rc_c == 2, rc_c)
check("C02 no call reaches the provider above the cap", len(c) == 0, len(c))
check("C03 it stops at once, without a retry storm", seconds_c < 60, f"{seconds_c} s")

memory = [line.split(" / ")[0] for line in (logs / "memory-a.log").read_text().splitlines() if " / " in line]
def mib(text):
    return float(text[:-3]) * (1024 if text.endswith("GiB") else 1)
result = {"date": time.strftime("%Y-%m-%d"), "provider": "fake (case/researcher/fake_model.py)",
          "researcher_memory_peak_mib": round(max(map(mib, memory))) if memory else None,
          "note": "scripted run; the memory of a real run is measured separately",
          "passed": sum(c["passed"] for c in checks), "total": len(checks), "checks": checks}
report.parent.mkdir(parents=True, exist_ok=True)
report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(f"{result['passed']}/{result['total']} checks passed; researcher memory peak {result['researcher_memory_peak_mib']} MiB")
sys.exit(0 if result["passed"] == result["total"] else 1)
PY
rc=$?

step "cleanup"
$A stop | tail -1
systemctl stop sqltr-fake-model 2>/dev/null
exit "$rc"
