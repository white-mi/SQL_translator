"""Model gateway for the researcher: the only process that holds the provider's API key.

The researcher container talks to this gateway instead of the provider. The gateway
  - swaps the client's token for the operator's key, so the researcher never sees the key;
  - lets through exactly one model, because a campaign runs on one model;
  - stops at a dollar cap, kept across restarts: the sum of the call log is the amount spent;
  - logs tokens and cost per call, never prompts or answers.

The request body goes upstream byte for byte: rewriting it would break prompt caching.
Standard library only, so no third-party package sits next to the key (ADR 0007).

The operator starts it with `arena.sh gateway` (systemd unit sqltr-arena-gateway, user arena). By hand:

    python case/researcher/gateway.py --config case/researcher/gateway.haiku-5-5.json \
        --key-file /srv/sqltr-arena/secrets/model-api-key --client-token-file /srv/sqltr-arena/secrets/model-token \
        --log /srv/sqltr-arena/model-usage.jsonl --max-usd 2 --listen 172.30.0.1:8094

--key-file holds the bare provider key. --client-token-file holds the token clients must send;
without it every client is served. --log gets one JSON line per billed call and is summed at start
to restore the spend. The gateway prints one status line, then serves until it is killed; the
routes and refusals are listed in Handler.
"""

from __future__ import annotations

import argparse
import hmac
import http.client
import json
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

MODEL_PATHS = {"/v1/messages", "/v1/chat/completions"}  # billed calls: the model is checked here
FREE_PATHS = {"/v1/messages/count_tokens"}  # not billed, but the model is still checked
READ_PATHS = {"/v1/models"}
REQUEST_HEADERS = {"content-type", "accept", "anthropic-version", "anthropic-beta", "user-agent"}
RESPONSE_HEADERS = {"content-type", "retry-after", "request-id", "x-request-id"}
MAX_BODY_BYTES = 32 * 2**20


@dataclass(frozen=True)
class Config:
    """Gateway settings, read from the JSON file given by --config, such as gateway.haiku-5-5.json.

    Attributes:
        upstream: provider base URL; the client's request path is appended to its path.
        auth: how the key goes upstream: "x-api-key" header or "bearer" authorization.
        model: the only model a request may name.
        prices_per_mtok: dollars per million tokens: "input", "output" and optionally
            "cache_read", "cache_write_5m", "cache_write_1h"; a missing cache price is "input".
        long_prompt: a call whose prompt is longer than "tokens" costs "multiplier" times more in
            full; None means one price at any length.
        max_concurrent: upstream calls in flight at once; further requests wait for a slot.
        timeout_seconds: socket timeout of each upstream connection.
    """

    upstream: str
    auth: str  # "x-api-key" or "bearer"
    model: str
    prices_per_mtok: dict
    long_prompt: dict | None = None  # {"tokens": N, "multiplier": M}: a longer prompt is billed M times higher
    max_concurrent: int = 4
    timeout_seconds: float = 900


class Usage:
    """Token counts of one upstream call, read from the usage objects in its response.

    Understands the Anthropic and the OpenAI response shapes. `input` counts uncached prompt tokens
    only; `cache_read`, `write_5m` and `write_1h` are the cached parts; `seen` tells whether any
    usage object arrived. Make one per call, pass each stream event or the whole JSON reply to
    `apply_event`, then price it with `cost`.
    """

    def __init__(self):
        """Start with zero counts and no usage seen."""
        self.input = self.output = self.cache_read = self.write_5m = self.write_1h = 0
        self.seen = False

    def apply(self, usage: dict) -> None:
        """Update the counts from one usage object; anything but a dict is ignored.

        An OpenAI object (it has `prompt_tokens`) sets the input, output and cache-read counts. An
        Anthropic object updates only the fields it carries, because a stream reports input in
        message_start and the running output total in message_delta. Cache writes without the
        5m/1h breakdown count as 5-minute writes.
        """
        if not isinstance(usage, dict):
            return
        self.seen = True
        if "prompt_tokens" in usage:  # OpenAI: cached tokens are part of prompt_tokens
            details = usage.get("prompt_tokens_details") or {}
            self.cache_read = details.get("cached_tokens") or 0
            self.input = (usage.get("prompt_tokens") or 0) - self.cache_read
            self.output = usage.get("completion_tokens") or 0
            return
        # Anthropic: a stream reports input in message_start and the running output total in message_delta
        self.input = usage.get("input_tokens", self.input) or 0
        self.output = usage.get("output_tokens", self.output) or 0
        self.cache_read = usage.get("cache_read_input_tokens", self.cache_read) or 0
        detail = usage.get("cache_creation")
        if isinstance(detail, dict):
            self.write_5m = detail.get("ephemeral_5m_input_tokens") or 0
            self.write_1h = detail.get("ephemeral_1h_input_tokens") or 0
        elif "cache_creation_input_tokens" in usage:
            self.write_5m, self.write_1h = usage["cache_creation_input_tokens"] or 0, 0

    def apply_event(self, event: dict) -> None:
        """Apply the usage in one stream event or one complete JSON reply.

        Reads `message.usage` (Anthropic message_start) or a top-level `usage` (message_delta, or a
        whole non-streamed reply); events without usage and non-dicts are ignored.
        """
        if not isinstance(event, dict):
            return
        message = event.get("message")
        if isinstance(message, dict) and "usage" in message:
            self.apply(message["usage"])
        elif event.get("usage"):
            self.apply(event["usage"])

    def prompt_tokens(self) -> int:
        """Return the prompt size the price depends on: uncached input plus cache reads and writes."""
        return self.input + self.cache_read + self.write_5m + self.write_1h

    def cost(self, config: Config) -> float:
        """Return the call's price in dollars under `config`'s prices.

        Above `config.long_prompt["tokens"]` prompt tokens the whole call, cache hits included, pays
        the long-prompt multiplier.
        """
        prices = config.prices_per_mtok
        long_prompt = config.long_prompt
        multiplier = 1
        if long_prompt and self.prompt_tokens() > long_prompt["tokens"]:  # the whole request pays the higher price
            multiplier = long_prompt["multiplier"]
        micro = (self.input * prices["input"] + self.output * prices["output"]
                 + self.cache_read * prices.get("cache_read", prices["input"])
                 + self.write_5m * prices.get("cache_write_5m", prices["input"])
                 + self.write_1h * prices.get("cache_write_1h", prices["input"]))
        return micro * multiplier / 1_000_000


class EventStream:
    """Incremental parser that feeds the usage in a server-sent event stream into a Usage.

    The stream arrives in arbitrary chunks, so a line split between chunks waits in the buffer.
    """

    def __init__(self, usage: Usage):
        """Feed the counts into `usage`, starting with an empty buffer."""
        self.usage = usage
        self.buffer = b""

    def feed(self, chunk: bytes) -> None:
        """Consume one chunk: apply every complete `data:` line and keep the trailing partial line.

        Payloads that are not JSON, such as "[DONE]", are skipped.
        """
        self.buffer += chunk
        *lines, self.buffer = self.buffer.split(b"\n")
        for line in lines:
            if line.startswith(b"data:"):
                try:
                    self.usage.apply_event(json.loads(line[5:]))
                except ValueError:
                    pass  # "[DONE]" and any non-JSON payload carry no usage


class Gateway:
    """Shared state of one gateway process: the key, the client token, the spend and the slots.

    Built by build_server; every handler thread reaches it as `Handler.gateway`. The spend and the
    call count are restored from the log at construction, so the cap holds across restarts.
    """

    def __init__(self, config: Config, key: str, client_token: str | None, log_path: Path, max_usd: float):
        """Hold the settings and restore the spend from `log_path` if it exists.

        Args:
            key: the provider key, sent upstream instead of the client's token.
            client_token: the token clients must send; None serves any client.
            log_path: the JSONL call log that `record` appends to.
            max_usd: the cap; billed calls are refused once the recorded spend reaches it.

        Raises:
            ValueError: the key is empty, or a line of the log is not JSON.
        """
        if not key:
            raise ValueError("empty API key")
        self.config, self.key, self.client_token, self.log_path, self.max_usd = config, key, client_token, log_path, max_usd
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(config.max_concurrent)
        self.spent = 0.0
        self.calls = 0
        self.unreported = 0
        if log_path.exists():
            for line in log_path.read_text().splitlines():
                row = json.loads(line)
                self.spent += row.get("usd", 0)
                self.calls += 1
                self.unreported += 0 if row.get("usage_complete", True) else 1

    def record(self, row: dict) -> None:
        """Count one billed call and append its row to the log as one JSON line.

        Thread-safe. Creates the log with mode 0640 (less the umask) if it is missing. Called once
        per billed call, by the `record_once` closure in Handler.forward.
        """
        with self.lock:
            self.spent += row.get("usd", 0)
            self.calls += 1
            self.unreported += 0 if row.get("usage_complete", True) else 1
            descriptor = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
            with os.fdopen(descriptor, "a") as log:
                log.write(json.dumps(row) + "\n")

    def over_budget(self) -> bool:
        """Return whether the recorded spend has reached the cap.

        A call counts only once it is recorded. Handler checks this before a call waits for a slot
        and again once it has one, so only calls already in flight, at most max_concurrent, can
        take the spend past the cap.
        """
        with self.lock:
            return self.spent >= self.max_usd

    def status(self) -> dict:
        """Return the summary served at GET /healthz and printed at start.

        `calls` counts the rows in the log (calls to billed paths, failed ones included);
        `calls_without_usage` counts 200 replies that reported no usage, so their cost is unknown
        and logged as 0.
        """
        with self.lock:
            return {"model": self.config.model, "spent_usd": round(self.spent, 6), "cap_usd": self.max_usd,
                    "calls": self.calls, "calls_without_usage": self.unreported}


class Handler(BaseHTTPRequestHandler):
    """HTTP handler of the gateway; build_server binds it to one Gateway in a subclass.

    GET  /healthz                              200 with Gateway.status(); no token needed.
    GET  /v1/models                            forwarded, not billed.
    POST /v1/messages, /v1/chat/completions    forwarded and billed.
    POST /v1/messages/count_tokens             forwarded, not billed; the model is still checked.

    Every path but /healthz needs the client token. Refusals use the Anthropic error shape: 401 wrong
    token, 404 unknown path, 413 body over MAX_BODY_BYTES, 400 body not a JSON object, 403 another
    model or the cap reached, 502 upstream unreachable or failed before the reply started (a later
    failure closes the connection mid-reply). Path matching ignores the query string;
    the forwarded path keeps it.
    """

    protocol_version = "HTTP/1.1"
    gateway: Gateway

    def log_message(self, *_args) -> None:
        """Drop the default per-request access log on stderr."""
        pass  # the call log is the only record

    def reply(self, status: int, payload: dict) -> None:
        """Send `payload` as a complete JSON response with the given status."""
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status: int, kind: str, message: str) -> None:
        """Send an error in the Anthropic error shape, with the message prefixed by "model gateway:".

        `kind` is the error type, such as "permission_error".
        """
        self.reply(status, {"type": "error", "error": {"type": kind, "message": f"model gateway: {message}"}})

    def authorized(self) -> bool:
        """Return whether the request carries the client token.

        The token is accepted in x-api-key or as `Authorization: Bearer`; x-api-key wins when both
        are present. Compared in constant time. Always true when the gateway has no client token.
        """
        expected = self.gateway.client_token
        if expected is None:
            return True
        supplied = self.headers.get("x-api-key") or ""
        bearer = self.headers.get("authorization") or ""
        if bearer.lower().startswith("bearer "):
            supplied = supplied or bearer[7:]
        return hmac.compare_digest(supplied.encode(), expected.encode())

    def do_GET(self) -> None:
        """Serve GET: /healthz without a token, READ_PATHS forwarded unbilled; else 401 or 404."""
        path = urlsplit(self.path).path
        if path == "/healthz":
            self.reply(200, self.gateway.status())
        elif not self.authorized():
            self.fail(401, "authentication_error", "invalid client token")
        elif path in READ_PATHS:
            self.forward("GET", b"", billed=False)
        else:
            self.fail(404, "not_found_error", "unknown path")

    def do_POST(self) -> None:
        """Serve POST: check size, token, path, JSON body, model and cap in that order, then forward.

        A body over MAX_BODY_BYTES is refused unread (413) and the connection closed. The cap
        applies to billed paths only and is refused with 403, not 429, so the client does not retry.
        """
        path = urlsplit(self.path).path
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            self.fail(413, "request_too_large", "request body too large")
            return
        body = self.rfile.read(length)
        if not self.authorized():
            self.fail(401, "authentication_error", "invalid client token")
        elif path not in MODEL_PATHS | FREE_PATHS:
            self.fail(404, "not_found_error", "unknown path")
        else:
            try:
                model = json.loads(body).get("model")
            except (ValueError, AttributeError):
                self.fail(400, "invalid_request_error", "request body is not a JSON object")
                return
            if model != self.gateway.config.model:
                self.fail(403, "permission_error", f"only {self.gateway.config.model} is allowed in this campaign")
            elif path in MODEL_PATHS and self.gateway.over_budget():
                self.refuse_over_budget()
            else:
                self.forward("POST", body, billed=path in MODEL_PATHS)

    def refuse_over_budget(self) -> None:
        """Refuse a billed call because the cap is reached: 403, not 429, so the client does not retry.

        A client retries a 429, and the researcher would wait for money that is not coming.
        """
        self.fail(403, "permission_error", f"budget of ${self.gateway.max_usd} is spent; the operator decides what next")

    def forward(self, method: str, body: bytes, billed: bool) -> None:
        """Send the request upstream with the operator's key and relay the reply to the client.

        Only REQUEST_HEADERS go upstream: the client's token is replaced by the key, and the body is
        passed byte for byte, because rewriting it would break prompt caching. Waits for one of the
        max_concurrent slots, then checks the cap again: calls that finished during the wait may
        have spent it, so the cap is overshot only by calls already in flight, at most
        max_concurrent. If the upstream fails before the client got any part of the reply, the
        client gets a 502; if it fails later, mid-stream, the connection is closed so the client
        sees a broken reply at once instead of waiting for its own timeout. A billed call is
        logged exactly once, also when it fails; its cost is 0 unless the status is 200.

        Args:
            method: "GET" or "POST".
            body: the raw request body, b"" for GET.
            billed: log the call and count its cost (MODEL_PATHS only).
        """
        config = self.gateway.config
        target = urlsplit(config.upstream)
        connection_type = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        headers = {k: v for k, v in self.headers.items() if k.lower() in REQUEST_HEADERS}
        headers["accept-encoding"] = "identity"  # usage is read from the response, so it must stay uncompressed
        if config.auth == "bearer":
            headers["authorization"] = f"Bearer {self.gateway.key}"
        else:
            headers["x-api-key"] = self.gateway.key
        started = time.monotonic()
        usage = Usage()
        status, stream, recorded = 0, False, False

        def record_once() -> None:
            """Log this billed call the first time it is called; later calls do nothing.

            Does nothing for unbilled paths. The row holds the status, token counts, cost, the
            long_prompt flag, the duration and usage_complete (false for a 200 without usage).
            """
            nonlocal recorded
            if not billed or recorded:
                return
            recorded = True
            self.gateway.record({
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "path": self.path, "status": status,
                "stream": stream, "input_tokens": usage.input, "output_tokens": usage.output,
                "cache_read_tokens": usage.cache_read, "cache_write_5m_tokens": usage.write_5m,
                "cache_write_1h_tokens": usage.write_1h, "usd": round(usage.cost(config), 8) if status == 200 else 0,
                "long_prompt": bool(config.long_prompt and usage.prompt_tokens() > config.long_prompt["tokens"]),
                "seconds": round(time.monotonic() - started, 2), "usage_complete": usage.seen or status != 200})

        with self.gateway.slots:
            if billed and self.gateway.over_budget():
                self.refuse_over_budget()  # not logged: it never reached the provider
                return
            connection = connection_type(target.hostname, target.port, timeout=config.timeout_seconds)
            self.response_started = False  # set by relay() just before the status line goes out
            try:
                connection.request(method, target.path.rstrip("/") + self.path, body, headers)
                response = connection.getresponse()
                status = response.status
                stream = "text/event-stream" in (response.getheader("content-type") or "")
                self.relay(response, usage, stream, status, record_once)
            except (OSError, http.client.HTTPException) as error:
                self.close_connection = True  # the reply cannot be completed, so end the connection
                if not self.response_started:
                    self.fail(502, "api_error", f"upstream unavailable: {type(error).__name__}")
            finally:
                connection.close()
        record_once()

    def relay(self, response, usage: Usage, stream: bool, status: int, record_once) -> None:
        """Pass the upstream response on to the client, recording the call before the response ends.

        A plain reply is read whole, recorded, then sent with a content-length. A stream is relayed
        chunk by chunk as it arrives and recorded before the terminating chunk. Either way the next
        call of the same client always finds its predecessor counted against the cap. If the client
        goes away mid-stream, the rest is still read to count the usage, then the connection is
        closed. Only RESPONSE_HEADERS and anthropic-ratelimit-* headers are passed on. Sets
        `self.response_started` just before the status line, so `forward` knows whether a 502 can
        still be sent. Upstream read errors propagate to `forward`, including a stream that ends
        before its announced content-length (IncompleteRead), so the client never gets a
        truncated reply that looks complete.

        Args:
            response: the upstream http.client.HTTPResponse.
            usage: filled from the response.
            stream: the response is a server-sent event stream.
            status: the upstream status; a plain reply's usage is read only when it is 200.
            record_once: the closure from `forward` that logs the call.
        """
        if not stream:
            payload = response.read()
            if status == 200:
                try:
                    usage.apply_event(json.loads(payload))
                except ValueError:
                    pass
            record_once()
        self.response_started = True
        self.send_response(status)
        for name, value in response.getheaders():
            if name.lower() in RESPONSE_HEADERS or name.lower().startswith("anthropic-ratelimit-"):
                self.send_header(name, value)
        if not stream:
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        events = EventStream(usage)
        client_gone = False
        while chunk := response.read1(65536):
            events.feed(chunk)
            if client_gone:
                continue  # keep reading: the call is billed either way, so the usage must still be counted
            try:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            except OSError:
                client_gone = True
        if response.length:  # the upstream closed before the content-length it announced
            raise http.client.IncompleteRead(b"", response.length)
        record_once()
        if client_gone:
            self.close_connection = True
        else:
            self.wfile.write(b"0\r\n\r\n")


def build_server(config: Config, key: str, client_token: str | None, log_path: Path, max_usd: float,
                 host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """Return a threaded HTTP server for the gateway, bound but not yet serving.

    The caller runs `serve_forever()` (main, or a thread in the tests). `server.gateway` is the
    shared Gateway. Port 0 picks a free port; read it from `server.server_address`. The other
    parameters are those of Gateway, which raises ValueError for an empty key.
    """
    gateway = Gateway(config, key, client_token, log_path, max_usd)
    handler = type("BoundHandler", (Handler,), {"gateway": gateway})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.gateway = gateway
    return server


def main() -> int:
    """Run the gateway from the command line until it is killed (see the module docstring).

    Surrounding whitespace and a leading byte order mark are stripped from the key file.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--client-token-file", type=Path)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--max-usd", required=True, type=float)
    parser.add_argument("--listen", default="127.0.0.1:8094")
    args = parser.parse_args()
    host, port = args.listen.rsplit(":", 1)
    config = Config(**json.loads(args.config.read_text()))
    token = args.client_token_file.read_text().strip() if args.client_token_file else None
    key = args.key_file.read_text().strip().lstrip("﻿")  # a key piped in from Windows PowerShell can carry a BOM
    server = build_server(config, key, token, args.log, args.max_usd, host, int(port))
    print(f"model gateway on {args.listen}: {json.dumps(server.gateway.status())}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
