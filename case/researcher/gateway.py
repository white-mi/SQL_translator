"""Model gateway for the researcher: the only process that holds the API key.

The researcher container talks to this gateway instead of the provider. The gateway
  - swaps the client's token for the operator's key, so the researcher never sees the key;
  - lets through exactly one model, because a campaign runs on one model;
  - stops at a dollar cap, kept across restarts: the sum of the call log is the amount spent;
  - logs tokens and cost per call, never prompts or answers.

The request body goes upstream byte for byte: rewriting it would break prompt caching.
Standard library only, so no third-party package sits next to the key (ADR 0007).

    python case/researcher/gateway.py --config case/researcher/gateway.haiku-5-5.json \
        --key-file /etc/sqltr-arena/model-api-key --client-token-file /srv/sqltr-arena/secrets/model-token \
        --log /srv/sqltr-arena/model-usage.jsonl --max-usd 2 --listen 172.30.0.1:8094
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
    upstream: str
    auth: str  # "x-api-key" or "bearer"
    model: str
    prices_per_mtok: dict
    long_prompt: dict | None = None  # {"tokens": N, "multiplier": M}: a longer prompt is billed M times higher
    max_concurrent: int = 4
    timeout_seconds: float = 900


class Usage:
    """Token counts of one call. Understands the Anthropic and the OpenAI response shapes."""

    def __init__(self):
        self.input = self.output = self.cache_read = self.write_5m = self.write_1h = 0
        self.seen = False

    def apply(self, usage: dict) -> None:
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
        if not isinstance(event, dict):
            return
        message = event.get("message")
        if isinstance(message, dict) and "usage" in message:
            self.apply(message["usage"])
        elif event.get("usage"):
            self.apply(event["usage"])

    def prompt_tokens(self) -> int:
        return self.input + self.cache_read + self.write_5m + self.write_1h

    def cost(self, config: Config) -> float:
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
    """Collects usage from a server-sent event stream that arrives in arbitrary chunks."""

    def __init__(self, usage: Usage):
        self.usage = usage
        self.buffer = b""

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        *lines, self.buffer = self.buffer.split(b"\n")
        for line in lines:
            if line.startswith(b"data:"):
                try:
                    self.usage.apply_event(json.loads(line[5:]))
                except ValueError:
                    pass  # "[DONE]" and any non-JSON payload carry no usage


class Gateway:
    def __init__(self, config: Config, key: str, client_token: str | None, log_path: Path, max_usd: float):
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
        with self.lock:
            self.spent += row.get("usd", 0)
            self.calls += 1
            self.unreported += 0 if row.get("usage_complete", True) else 1
            descriptor = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
            with os.fdopen(descriptor, "a") as log:
                log.write(json.dumps(row) + "\n")

    def over_budget(self) -> bool:
        with self.lock:
            return self.spent >= self.max_usd

    def status(self) -> dict:
        with self.lock:
            return {"model": self.config.model, "spent_usd": round(self.spent, 6), "cap_usd": self.max_usd,
                    "calls": self.calls, "calls_without_usage": self.unreported}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    gateway: Gateway

    def log_message(self, *_args) -> None:
        pass  # the call log is the only record

    def reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status: int, kind: str, message: str) -> None:
        self.reply(status, {"type": "error", "error": {"type": kind, "message": f"model gateway: {message}"}})

    def authorized(self) -> bool:
        expected = self.gateway.client_token
        if expected is None:
            return True
        supplied = self.headers.get("x-api-key") or ""
        bearer = self.headers.get("authorization") or ""
        if bearer.lower().startswith("bearer "):
            supplied = supplied or bearer[7:]
        return hmac.compare_digest(supplied.encode(), expected.encode())

    def do_GET(self) -> None:
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
                # 403, not 429: a client retries a 429 and the researcher would wait for nothing
                self.fail(403, "permission_error", f"budget of ${self.gateway.max_usd} is spent; the operator decides what next")
            else:
                self.forward("POST", body, billed=path in MODEL_PATHS)

    def forward(self, method: str, body: bytes, billed: bool) -> None:
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
            connection = connection_type(target.hostname, target.port, timeout=config.timeout_seconds)
            try:
                connection.request(method, target.path.rstrip("/") + self.path, body, headers)
                response = connection.getresponse()
                status = response.status
                stream = "text/event-stream" in (response.getheader("content-type") or "")
                self.relay(response, usage, stream, status, record_once)
            except (OSError, http.client.HTTPException) as error:
                if not status:  # nothing reached the client yet
                    self.close_connection = True
                    self.fail(502, "api_error", f"upstream unavailable: {type(error).__name__}")
            finally:
                connection.close()
        record_once()

    def relay(self, response, usage: Usage, stream: bool, status: int, record_once) -> None:
        """Pass the response on. The cost is recorded before the client sees the end of the response,
        so the next call of the same client always finds its predecessor counted against the cap."""
        if not stream:
            payload = response.read()
            if status == 200:
                try:
                    usage.apply_event(json.loads(payload))
                except ValueError:
                    pass
            record_once()
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
        record_once()
        if client_gone:
            self.close_connection = True
        else:
            self.wfile.write(b"0\r\n\r\n")


def build_server(config: Config, key: str, client_token: str | None, log_path: Path, max_usd: float,
                 host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    gateway = Gateway(config, key, client_token, log_path, max_usd)
    handler = type("BoundHandler", (Handler,), {"gateway": gateway})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.gateway = gateway
    return server


def main() -> int:
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
