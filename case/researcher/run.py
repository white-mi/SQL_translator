"""Researcher loop inside the container: Hermes works until the journal says the search is over.

An agent may end its turn while the search is unfinished, so after every turn the loop asks the journal
how far the search is and, if it is not over, sends the agent back to work. Two turns in a row without
any change in the journal end the run with an error: that would spend money and move nothing.

The container holds only a token for the model gateway. The provider key stays with the gateway.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARENA_PYTHON = "/usr/local/bin/python"  # the image's system Python holds the arena CLI's packages
TOOLSETS = ["terminal", "file", "todo", "delegation"]
SYSTEM_MESSAGE = (
    "Follow the arena instructions and the campaign conditions you were given. "
    "Work only inside /workspace and through the arena CLI. All model calls go through the configured gateway."
)


def write_home(home: Path, values: dict[str, str]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    text = (HERE / "hermes-config.yaml").read_text()
    for name, value in values.items():
        text = text.replace(f"@{name}@", value)
    (home / "config.yaml").write_text(text)
    (home / "config.yaml").chmod(0o600)


def journal_status(journal: str) -> dict:
    out = subprocess.run([ARENA_PYTHON, "-m", "evaluator.search", "--journal", journal, "status"],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def search_finished(status: dict, stop_at_limit: bool) -> bool:
    if status.get("running"):
        return False
    if status.get("finalization"):
        return True
    return stop_at_limit and not status.get("remaining_proposals")


def continuation(status: dict) -> str:
    if status.get("running"):
        return (f"Your turn ended, but these measurements are still open: {json.dumps(status['running'])}. "
                "Collect their results and record them in the journal, waiting for the calculated remaining "
                "time with a blocking command, not with repeated model calls.")
    if status.get("remaining_proposals") and not status.get("stopped"):
        return (f"Your turn ended, but {status['remaining_proposals']} of {status['max_solutions']} proposal "
                "places are still free. Continue the search under the same conditions. Time, tokens and a "
                "good enough score are not reasons to stop.")
    return ("The proposal limit is used up or the search was stopped. Do not propose new solutions. Finish the "
            "planned measurements, select the finalists and finalize the batch.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--journal", default=os.environ.get("ARENA_SEARCH_JOURNAL", "/search/search.sqlite3"))
    parser.add_argument("--model", default=os.environ.get("ARENA_MODEL_NAME", "claude-haiku-5-5"))
    parser.add_argument("--gateway-url", default=os.environ.get("SQLTR_MODEL_URL"))
    parser.add_argument("--token-file", type=Path, default=Path("/run/secrets/model-token"))
    parser.add_argument("--api-mode", default="anthropic_messages")
    parser.add_argument("--effort", default="max")
    parser.add_argument("--cwd", default="/workspace")
    parser.add_argument("--toolsets", default=",".join(TOOLSETS))
    parser.add_argument("--stop-at-limit", action="store_true",
                        help="end when the proposal limit is used up, without finalization (rehearsal)")
    parser.add_argument("--transcript", type=Path, help="write the conversation here after every turn")
    args = parser.parse_args()
    if not args.gateway_url:
        raise SystemExit("--gateway-url or SQLTR_MODEL_URL is required")

    token = args.token_file.read_text().strip()
    # the agent's terminal needs the token only to call the gateway; it is worthless without that network
    os.environ["ARENA_MODEL_TOKEN"] = token
    home = Path(os.environ.get("HERMES_HOME", "/tmp/hermes-home"))
    os.environ["HERMES_HOME"] = str(home)
    Path(os.environ.get("HOME", "/tmp/researcher-home")).mkdir(parents=True, exist_ok=True)  # /tmp starts empty
    write_home(home, {"MODEL": args.model, "BASE_URL": args.gateway_url, "API_KEY": token,
                      "API_MODE": args.api_mode, "EFFORT": args.effort})

    from run_agent import AIAgent  # after HERMES_HOME: Hermes reads its settings on import

    agent = AIAgent(model=args.model, base_url=args.gateway_url, api_key=token, provider="custom", api_mode=args.api_mode,
                    reasoning_config={"enabled": True, "effort": args.effort},
                    enabled_toolsets=[t for t in args.toolsets.split(",") if t], max_iterations=2000, cwd=args.cwd,
                    skip_memory=True, skip_background_review=True, skip_context_files=True, save_trajectories=False,
                    quiet_mode=True)
    prompt = args.prompt_file.read_text()
    history = None
    previous = journal_status(args.journal)
    idle_turns = 0
    turn = 0
    try:
        while True:
            turn += 1
            result = agent.run_conversation(prompt, system_message=SYSTEM_MESSAGE, conversation_history=history)
            history = result.get("messages")
            if args.transcript and history:
                args.transcript.write_text("".join(json.dumps(m, ensure_ascii=False, default=str) + "\n" for m in history))
            status = journal_status(args.journal)
            print(json.dumps({"turn": turn, "completed": result.get("completed"), "failed": result.get("failed"),
                              "proposed": status.get("proposed"), "remaining": status.get("remaining_proposals"),
                              "running": len(status.get("running") or []), "finalized": bool(status.get("finalization")),
                              "error": str(result.get("error") or "")[:300]}), flush=True)
            if result.get("failed") or not result.get("completed"):
                print("the agent failed; the search is not finished", file=sys.stderr, flush=True)
                return 2
            if search_finished(status, args.stop_at_limit):
                print((result.get("final_response") or "")[:2000], flush=True)
                return 0
            idle_turns = idle_turns + 1 if status == previous else 0
            if idle_turns >= 2:
                print("two turns without any change in the journal; stopping", file=sys.stderr, flush=True)
                return 3
            if not history:
                print("the agent returned no conversation history", file=sys.stderr, flush=True)
                return 4
            previous = status
            prompt = continuation(status)
    finally:
        agent.close()


if __name__ == "__main__":
    raise SystemExit(main())
