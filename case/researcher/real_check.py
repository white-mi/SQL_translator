"""First calls to a real provider through the gateway: settles what only a real call can show. Costs cents.

The rehearsal with the fake provider checks the plumbing. This answers the questions the fake can only guess:
  1. is the model reachable, and does the account accept the headers Hermes sends?
  2. does the API accept thinking blocks replayed in a tool loop?
  3. does it accept a tool loop without them? (Hermes falls back to this after compression)
  4. does this account enforce the check on an edited history?
  5. does prompt caching really work through the gateway?

The script never sees the provider key: it calls the gateway with the client token, like the researcher.

Needs a gateway running with the real key (`arena.sh gateway`). The token file belongs to the arena
user with mode 0600, hence sudo. The port is the settings' GATEWAY_PORT: 8094 in the campaign,
18094 in the rehearsal root.

    sudo python3 case/researcher/real_check.py --url http://172.30.0.1:18094 --token-file <root>/secrets/model-token

Makes eight calls (five if the model does not call the tool), prints one JSON summary per call and
the gateway's /healthz status at the end; --report also writes them to a JSON file. Exit code 0 if
the plain call with Hermes's betas returned 200, else 1; the other answers are for the operator to read.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

HERMES_BETAS = "interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14"
TOOL = {"name": "get_row_count", "description": "Return the number of rows in a table of the warehouse.",
        "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}}
QUESTION = "How many rows does the table retail_orders have? Use the tool, then answer with the number only."
# long enough to pass the minimum cacheable prompt; the text itself does not matter
LONG_SYSTEM = "You answer questions about a retail warehouse. " + " ".join(
    f"Rule {i}: table number {i} stores one kind of retail fact and has a primary key." for i in range(450))


class Client:
    """Minimal Messages API client that calls the gateway the way Hermes does.

    It sends the client token as x-api-key, an anthropic-beta header, adaptive thinking and an
    effort level, and never streams.
    """

    def __init__(self, url: str, token: str, model: str, betas: str | None):
        """Remember the gateway base URL, the client token, the model and the default anthropic-beta.

        `betas` None or "" sends no anthropic-beta header by default.
        """
        self.url, self.token, self.model, self.betas = url.rstrip("/"), token, model, betas

    def call(self, messages: list, *, tools: list | None = None, system=None, effort: str = "low", betas: str | None = None) -> tuple[int, dict]:
        """Send one /v1/messages request and return (HTTP status, parsed JSON body).

        Thinking is always adaptive and max_tokens 2000. An HTTP error comes back the same way, with
        {} when its body is not JSON; network failures raise.

        Args:
            system: the system prompt, a string or a list of blocks; omitted when empty.
            effort: output_config.effort.
            betas: anthropic-beta for this call; None uses the client's default, "" sends none.
        """
        body = {"model": self.model, "max_tokens": 2000, "messages": messages,
                "thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
        if tools:
            body["tools"] = tools
        if system:
            body["system"] = system
        headers = {"content-type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": self.token}
        chosen = betas if betas is not None else self.betas
        if chosen:
            headers["anthropic-beta"] = chosen
        request = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            except ValueError:
                return error.code, {}

    def status(self) -> dict:
        """Return the gateway's GET /healthz summary: model, spend, cap and call counts."""
        with urllib.request.urlopen(self.url + "/healthz", timeout=20) as response:
            return json.loads(response.read())


def describe(status: int, payload: dict) -> dict:
    """Return the summary of one call that is printed and reported.

    Status, stop reason, content block types, token counts (input, output, cache read and write)
    and the first 300 characters of any error message.
    """
    usage = payload.get("usage") or {}
    return {"status": status, "stop_reason": payload.get("stop_reason"),
            "blocks": [b.get("type") for b in payload.get("content") or []],
            "input": usage.get("input_tokens"), "output": usage.get("output_tokens"),
            "cache_read": usage.get("cache_read_input_tokens"), "cache_write": usage.get("cache_creation_input_tokens"),
            "error": (payload.get("error") or {}).get("message", "")[:300]}


def main() -> int:
    """Run the checks in order, print each summary and the gateway status, write --report if given.

    The tool loop checks reuse the first tool call's reply and are skipped when the model does not
    call the tool. Returns 0 if the plain call with Hermes's betas succeeded, else 1.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    client = Client(args.url, args.token_file.read_text().strip(), args.model, HERMES_BETAS)
    results: dict[str, dict] = {}

    def run(name: str, status: int, payload: dict) -> None:
        """Store the summary of the call `name` in `results` and print it as one line."""
        results[name] = describe(status, payload)
        print(f"{name}: {json.dumps(results[name], ensure_ascii=False)}", flush=True)

    hello = [{"role": "user", "content": "Reply with the single word: ready"}]
    run("plain_without_betas", *client.call(hello, betas=""))
    run("plain_with_hermes_betas", *client.call(hello))

    first = [{"role": "user", "content": QUESTION}]
    status, opening = client.call(first, tools=[TOOL], effort="max")
    run("tool_call_at_effort_max", status, opening)
    blocks = opening.get("content") or []
    call = next((b for b in blocks if b.get("type") == "tool_use"), None)
    if status == 200 and call:
        result = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": "1234"}]}
        run("tool_loop_with_thinking", *client.call(first + [{"role": "assistant", "content": blocks}, result], tools=[TOOL], effort="max"))
        no_thinking = [b for b in blocks if b.get("type") not in {"thinking", "redacted_thinking"}]
        run("tool_loop_without_thinking", *client.call(first + [{"role": "assistant", "content": no_thinking}, result], tools=[TOOL], effort="max"))
        edited = [{"role": "user", "content": QUESTION + " Please be quick."}]
        run("edited_history_with_thinking", *client.call(edited + [{"role": "assistant", "content": blocks}, result], tools=[TOOL], effort="max"))
    else:
        print("the model did not call the tool, so the tool loop questions stay open", flush=True)

    system = [{"type": "text", "text": LONG_SYSTEM, "cache_control": {"type": "ephemeral"}}]
    run("cache_first_call", *client.call(hello, system=system))
    run("cache_second_call", *client.call(hello, system=system))

    gateway = client.status()
    print(f"gateway after the check: {json.dumps(gateway)}", flush=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"model": args.model, "results": results, "gateway": gateway}, ensure_ascii=False, indent=2) + "\n")
    return 0 if results.get("plain_with_hermes_betas", {}).get("status") == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
