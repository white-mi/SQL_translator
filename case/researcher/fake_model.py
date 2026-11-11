"""A stand-in for the model provider, so the whole researcher route can be tested without spending money.

Only the provider is fake. Hermes, the gateway, the firewall, the journal, the train evaluator and the
runner are the real ones, so a scripted "researcher" exercises the same path as a real model.

It rejects what the real Claude Haiku 5.5 API rejects (budget_tokens, sampling parameters, a request
that does not end with a user turn, a thinking block replayed after the history before it was edited,
and a few more: see request_problems), so a badly shaped request shows up here and not on the bill.
Signatures are digests of the conversation so far, an approximation of the real binding rule. A tool
loop without its thinking blocks is only flagged as a warning: the API reference asks to send them
back, but also names stripping them as the recovery from an edited history, so only a real call can
show how strict it is. Every request is summarised in a JSONL log, without message text except the
latest tool result. The log keeps the x-api-key header as received, so the rehearsal can check that
the gateway sends the operator key: point a gateway at it only with a fake key.

rehearsal.sh runs it as the systemd unit sqltr-fake-model on the default --listen address and points
the gateway's upstream there. By hand:

    python case/researcher/fake_model.py --script case/researcher/rehearsal_script.json --log /tmp/fake.jsonl

--script is a JSON list of steps (see Script); --inflate-at and --inflate-tokens make one step report
a huge prompt so that Hermes compresses its context; --dump-dir (an existing directory) keeps every
raw request body. Serves until it is killed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

EFFORTS = {"low", "medium", "high", "xhigh", "max"}
MAX_OUTPUT_TOKENS = 128_000
TOOL_RESULT_CHARS = 6000


def _without_markers(node):
    """Return a copy of a request fragment without what does not count as an edit, for digests.

    Cache markers may move between requests and old thinking may be dropped: neither is an edit, so
    `cache_control` keys and thinking blocks are removed. A plain string and a single text block are
    two spellings of the same message content, so a message's string content becomes a text block.
    """
    if isinstance(node, dict):
        cleaned = {k: _without_markers(v) for k, v in node.items() if k != "cache_control"}
        if isinstance(cleaned.get("content"), str) and "role" in cleaned:
            cleaned["content"] = [{"type": "text", "text": cleaned["content"]}]
        return cleaned
    if isinstance(node, list):
        return [_without_markers(v) for v in node if not (isinstance(v, dict) and v.get("type") == "thinking")]
    return node


def conversation_digest(body: dict, upto: int) -> str:
    """Return the fake signature of a thinking block in message `upto`.

    It digests what the block was produced from: system, tools and the messages before `upto`.
    Script.next_blocks signs replies with it and thinking_problems checks replayed blocks against it.
    """
    prefix = {"system": body.get("system"), "tools": body.get("tools"), "messages": (body.get("messages") or [])[:upto]}
    text = json.dumps(_without_markers(prefix), sort_keys=True, ensure_ascii=False)
    return "fake-signature-" + hashlib.sha256(text.encode()).hexdigest()[:32]


def thinking_problems(body: dict) -> list[str]:
    """Return an API-style error for each replayed thinking block whose signature does not match.

    A mismatch means the history before the block was edited (as after compression) or the
    signature did not come from this provider.
    """
    problems = []
    for index, message in enumerate(body.get("messages") or []):
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            continue
        for position, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "thinking" and block.get("signature") != conversation_digest(body, index):
                problems.append(f"messages.{index}.content.{position}: Invalid `signature` in `thinking` block. "
                                "The block is bound to a different conversation.")
    return problems


def thinking_warnings(body: dict) -> list[str]:
    """Return a warning when the running tool loop comes back without its thinking block.

    With adaptive thinking, the assistant turn that opened the loop (the second-to-last message)
    should carry its thinking back with its tool_use. A warning is logged, not rejected.
    """
    messages = body.get("messages") or []
    if (body.get("thinking") or {}).get("type") != "adaptive" or len(messages) < 2:
        return []
    last_assistant = messages[-2]
    if last_assistant.get("role") != "assistant" or not isinstance(last_assistant.get("content"), list):
        return []
    blocks = [b for b in last_assistant["content"] if isinstance(b, dict)]
    if any(b.get("type") == "tool_use" for b in blocks) and not any(b.get("type") in {"thinking", "redacted_thinking"} for b in blocks):
        return [f"messages.{len(messages) - 2}: the tool loop came back without its thinking block"]
    return []


def request_problems(body: dict, expected_model: str) -> list[str]:
    """Return why the real API would answer `body` with a 400; an empty list if it would pass.

    As far as the model reference documents it: a model other than `expected_model`, sampling
    parameters, thinking.budget_tokens or a thinking type other than adaptive or disabled, an
    unknown effort, max_tokens outside 1..MAX_OUTPUT_TOKENS, a last message that is not a user turn,
    and replayed thinking blocks with a wrong signature.
    """
    problems = thinking_problems(body)
    if body.get("model") != expected_model:
        problems.append(f"model {body.get('model')!r}")
    for name in ("temperature", "top_p", "top_k"):
        if name in body:
            problems.append(f"sampling parameter {name}")
    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        if "budget_tokens" in thinking:
            problems.append("thinking.budget_tokens")
        if thinking.get("type") not in {"adaptive", "disabled"}:
            problems.append(f"thinking.type {thinking.get('type')!r}")
    effort = (body.get("output_config") or {}).get("effort")
    if effort is not None and effort not in EFFORTS:
        problems.append(f"effort {effort!r}")
    if not isinstance(body.get("max_tokens"), int) or not 0 < body["max_tokens"] <= MAX_OUTPUT_TOKENS:
        problems.append(f"max_tokens {body.get('max_tokens')!r}")
    messages = body.get("messages") or []
    if not messages or messages[-1].get("role") != "user":
        problems.append("the request does not end with a user turn")
    return problems


def count_cache_markers(body: dict) -> int:
    """Return the number of cache_control markers in the system prompt, messages and tools."""
    def walk(node) -> int:
        """Count the dicts with a cache_control key in a JSON fragment, recursively."""
        if isinstance(node, dict):
            return ("cache_control" in node) + sum(walk(v) for v in node.values())
        if isinstance(node, list):
            return sum(walk(v) for v in node)
        return 0
    return walk(body.get("system")) + walk(body.get("messages")) + walk(body.get("tools"))


def latest_tool_result(body: dict) -> str | None:
    """Return the text of the newest tool result in the last message, or None if it has none.

    Only the last message is looked at. The text is cut to its last TOOL_RESULT_CHARS characters;
    the rehearsal reads its checks from what the scripted commands printed.
    """
    for message in reversed(body.get("messages") or []):
        content = message.get("content")
        if isinstance(content, list):
            for block in reversed(content):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    inner = block.get("content")
                    if isinstance(inner, list):
                        inner = "\n".join(p.get("text", "") for p in inner if isinstance(p, dict))
                    return str(inner)[-TOOL_RESULT_CHARS:]
        return None
    return None


def thinking_blocks_replayed(body: dict) -> int:
    """Return how many thinking blocks the request's assistant turns carry back."""
    count = 0
    for message in body.get("messages") or []:
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, list):
            count += sum(1 for block in content if isinstance(block, dict) and block.get("type") == "thinking")
    return count


class Script:
    """The scripted researcher: assistant turns handed out in order, one per request with tools.

    Each step is {"command": "..."} (a terminal tool call), {"tool": name, "input": {...}} (any
    tool call) or {"text": "..."} (a final answer that ends the turn). Once the steps run out, every
    reply is a final answer. Requests without tools are helper calls, such as compression, and get
    a summary without using up a step. Thread-safe.
    """

    def __init__(self, steps: list[dict], inflate_at: int | None, inflate_tokens: int):
        """Hold the steps, starting at the first one.

        The reply to step number `inflate_at` (None: no step) reports `inflate_tokens` extra input
        tokens, which makes Hermes believe its context is full and compress it.
        """
        self.steps, self.inflate_at, self.inflate_tokens = steps, inflate_at, inflate_tokens
        self.position = 0
        self.lock = threading.Lock()

    def next_blocks(self, body: dict) -> tuple[list[dict], str, int]:
        """Return the reply to `body` as (content blocks, stop_reason, extra input tokens).

        Every scripted reply starts with a thinking block signed over the request's whole history,
        so a later request that replays the block passes only while that history is unedited.
        """
        if not body.get("tools"):
            return [{"type": "text", "text": "Summary of the earlier turns: the rehearsal is in progress."}], "end_turn", 0
        with self.lock:
            index = self.position
            self.position += 1
        extra = self.inflate_tokens if index == self.inflate_at else 0
        thinking = {"type": "thinking", "thinking": f"Step {index} of the rehearsal.",
                    "signature": conversation_digest(body, len(body.get("messages") or []))}
        if index >= len(self.steps):
            return [thinking, {"type": "text", "text": "The scripted rehearsal is complete."}], "end_turn", extra
        step = self.steps[index]
        if "text" in step:
            return [thinking, {"type": "text", "text": step["text"]}], "end_turn", extra
        call = {"type": "tool_use", "id": f"toolu_fake_{index}", "name": step.get("tool", "terminal"),
                "input": step.get("input") or {"command": step["command"]}}
        return [thinking, call], "tool_use", extra


def sse(event: dict) -> bytes:
    """Encode one event as a server-sent event frame named after its type."""
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


def stream_events(model: str, blocks: list[dict], stop: str, usage: dict) -> bytes:
    """Return a whole streamed reply as SSE bytes, in the Anthropic event order.

    message_start carries `usage` with one output token, each block gets its start, deltas and
    stop events, and message_delta carries `stop` and the final output tokens.
    """
    start_usage = {**usage, "output_tokens": 1}
    out = sse({"type": "message_start", "message": {
        "id": "msg_fake", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": start_usage}})
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            out += sse({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            out += sse({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": block["text"]}})
        elif block["type"] == "thinking":
            out += sse({"type": "content_block_start", "index": index,
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            out += sse({"type": "content_block_delta", "index": index, "delta": {"type": "thinking_delta", "thinking": block["thinking"]}})
            out += sse({"type": "content_block_delta", "index": index, "delta": {"type": "signature_delta", "signature": block["signature"]}})
        else:
            out += sse({"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})
            out += sse({"type": "content_block_delta", "index": index, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
        out += sse({"type": "content_block_stop", "index": index})
    out += sse({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": usage["output_tokens"]}})
    out += sse({"type": "message_stop"})
    return out


def build_server(script: Script, model: str, log_path: Path, host: str = "127.0.0.1", port: int = 0,
                 dump_dir: Path | None = None) -> ThreadingHTTPServer:
    """Return a threaded HTTP server of the fake provider, bound but not yet serving.

    Args:
        script: the scripted replies, shared by all requests.
        model: the only model accepted, also the one listed by /v1/models.
        log_path: the JSONL log that gets one summary per POST.
        port: 0 picks a free port; read it from `server.server_address`.
        dump_dir: an existing directory to save each raw request body in, as request-NNN.json.
    """
    lock = threading.Lock()
    requests_seen = [0]

    class Handler(BaseHTTPRequestHandler):
        """HTTP handler of the fake provider; it checks no token.

        GET /v1/models answers 200 with `model`; any other GET 404. A POST to any path is taken as
        a Messages request: it is logged, then answered 400 invalid_request_error listing the
        problems, or 200 with the next scripted reply, as SSE when the body asks for a stream.
        """

        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:
            """Drop the default per-request access log on stderr."""
            pass

        def send_body(self, status: int, content_type: str, payload: bytes) -> None:
            """Send a complete response with the given status, content type and body."""
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            """Serve GET: 200 with the model list for /v1/models, 404 for anything else."""
            if self.path.split("?")[0] == "/v1/models":
                self.send_body(200, "application/json", json.dumps({"data": [{"id": model, "type": "model"}]}).encode())
            else:
                self.send_body(404, "application/json", b'{"type":"error","error":{"type":"not_found_error","message":"unknown"}}')

        def do_POST(self) -> None:
            """Serve POST: log a summary of the request, then answer 400 with its problems or 200.

            The 200 reply is the script's next turn; its usage reports about a quarter of the request
            bytes as input tokens, plus the inflated amount on the inflate step.
            """
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            body = json.loads(raw)
            if dump_dir:
                with lock:
                    requests_seen[0] += 1
                    (dump_dir / f"request-{requests_seen[0]:03d}.json").write_bytes(raw)
            problems = request_problems(body, model)
            summary = {
                "path": self.path, "model": body.get("model"), "stream": bool(body.get("stream")),
                "max_tokens": body.get("max_tokens"), "thinking": body.get("thinking"),
                "output_config": body.get("output_config"), "cache_markers": count_cache_markers(body),
                "messages": len(body.get("messages") or []), "tools": sorted(t.get("name", "") for t in body.get("tools") or []),
                "thinking_replayed": thinking_blocks_replayed(body), "request_bytes": len(raw),
                "version": self.headers.get("anthropic-version"), "betas": self.headers.get("anthropic-beta"),
                "auth_seen": "x-api-key" if self.headers.get("x-api-key") else "none",
                "key": self.headers.get("x-api-key"), "problems": problems, "warnings": thinking_warnings(body),
                "latest_tool_result": latest_tool_result(body),
            }
            with lock, log_path.open("a") as log:
                log.write(json.dumps(summary, ensure_ascii=False) + "\n")
            if problems:
                error = {"type": "error", "error": {"type": "invalid_request_error", "message": "; ".join(problems)}}
                self.send_body(400, "application/json", json.dumps(error).encode())
                return
            blocks, stop, extra = script.next_blocks(body)
            usage = {"input_tokens": len(raw) // 4 + extra, "output_tokens": 50 + sum(len(json.dumps(b)) for b in blocks) // 4,
                     "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
            if body.get("stream"):
                self.send_body(200, "text/event-stream", stream_events(model, blocks, stop, usage))
            else:
                message = {"id": "msg_fake", "type": "message", "role": "assistant", "model": model, "content": blocks,
                           "stop_reason": stop, "stop_sequence": None, "usage": usage}
                self.send_body(200, "application/json", json.dumps(message).encode())

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main() -> int:
    """Run the fake provider from the command line until it is killed (see the module docstring)."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--script", required=True, type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--listen", default="127.0.0.1:18099")
    parser.add_argument("--inflate-at", type=int, help="scripted step that reports a huge prompt, to trigger compression")
    parser.add_argument("--inflate-tokens", type=int, default=90_000)
    parser.add_argument("--dump-dir", type=Path, help="save raw request bodies here, to compare them when debugging")
    args = parser.parse_args()
    host, port = args.listen.rsplit(":", 1)
    script = Script(json.loads(args.script.read_text()), args.inflate_at, args.inflate_tokens)
    server = build_server(script, args.model, args.log, host, int(port), args.dump_dir)
    print(f"fake model on {args.listen}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
