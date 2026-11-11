"""The researcher's model gateway: the key stays hidden, one model, a hard dollar cap, no content in the log."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "model_gateway", Path(__file__).resolve().parents[1] / "case" / "researcher" / "gateway.py")
gw = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gw  # dataclasses look the module up while the class is being built
SPEC.loader.exec_module(gw)

KEY = "sk-operator-secret"
CLIENT = "client-token"
MODEL = "claude-haiku-5-5"
PRICES = {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write_5m": 0.125, "cache_write_1h": 0.20}
SECRET_TEXT = "the-prompt-must-not-reach-the-log"


class Upstream:
    """A fake provider: records what it receives and answers with a prepared response.

    Tests change the reply by setting `body`, `content_type` and `status` before posting.
    `requests` holds one `{"path", "headers", "body"}` per request received (header names
    lowercased, body as raw bytes); `url` is the base URL; `server.shutdown()` stops it.
    """

    def __init__(self):
        """Serve on a free local port; the default reply reports 1000 input, 100 output tokens."""
        self.requests: list[dict] = []
        self.body = json.dumps({"usage": {"input_tokens": 1000, "output_tokens": 100}, "content": [{"text": "answer"}]}).encode()
        self.content_type = "application/json"
        self.status = 200
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            """Request handler that records into and replies from this Upstream."""

            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                """Silence the per-request access log."""
                pass

            def do_POST(self):
                """Record the request, then send the prepared status, content type and body."""
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                upstream.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                self.send_response(upstream.status)
                self.send_header("content-type", upstream.content_type)
                self.send_header("content-length", str(len(upstream.body)))
                self.end_headers()
                self.wfile.write(upstream.body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def upstream():
    """Yield a running Upstream and shut it down after the test."""
    fake = Upstream()
    yield fake
    fake.server.shutdown()


def start(upstream_url: str, log: Path, max_usd: float = 1.0, auth: str = "x-api-key"):
    """Start a gateway in front of `upstream_url` on a free local port; return `(server, base URL)`.

    The gateway serves MODEL at PRICES, accepts CLIENT as the client token, forwards with KEY,
    writes its usage log to `log` and refuses calls once the spend reaches `max_usd`; a call with
    more than 100,000 prompt tokens costs five times as much. `auth` is "x-api-key" or "bearer".
    `server.gateway.spent` is the spend so far; the test calls `server.shutdown()`.
    """
    config = gw.Config(upstream=upstream_url, auth=auth, model=MODEL, prices_per_mtok=PRICES,
                       long_prompt={"tokens": 100000, "multiplier": 5})
    server = gw.build_server(config, KEY, CLIENT, log, max_usd)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def post(url: str, payload: dict | None = None, token: str | None = CLIENT, path: str = "/v1/messages"):
    """POST a JSON request to the gateway; return `(status, response body, request body sent)`.

    Args:
        payload: the request JSON; None sends a messages request for MODEL whose prompt is
            SECRET_TEXT.
        token: client token sent as x-api-key; None sends no token.
        path: request path on the gateway.

    HTTP error statuses are returned, not raised.
    """
    body = json.dumps(payload if payload is not None else {"model": MODEL, "messages": [{"role": "user", "content": SECRET_TEXT}]}).encode()
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    if token:
        headers["x-api-key"] = token
    request = urllib.request.Request(url + path, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), body
    except urllib.error.HTTPError as error:
        return error.code, error.read(), body


def test_key_replaces_client_token_and_body_is_untouched(upstream, tmp_path):
    server, url = start(upstream.url, tmp_path / "log.jsonl")
    status, answer, sent = post(url)
    assert status == 200
    received = upstream.requests[0]
    assert received["headers"]["x-api-key"] == KEY
    assert CLIENT not in json.dumps(received["headers"])
    assert received["body"] == sent  # byte for byte, or prompt caching breaks
    assert received["headers"]["anthropic-version"] == "2023-06-01"
    assert KEY.encode() not in answer
    server.shutdown()


def test_bearer_auth_for_openai_style_providers(upstream, tmp_path):
    server, url = start(upstream.url, tmp_path / "log.jsonl", auth="bearer")
    post(url, path="/v1/chat/completions")
    assert upstream.requests[0]["headers"]["authorization"] == f"Bearer {KEY}"
    server.shutdown()


@pytest.mark.parametrize("token,payload,path,expected", [
    (None, None, "/v1/messages", 401),
    ("wrong", None, "/v1/messages", 401),
    (CLIENT, {"model": "claude-opus-5-5", "messages": []}, "/v1/messages", 403),
    (CLIENT, None, "/v1/files", 404),
    (CLIENT, {"model": "claude-opus-5-5"}, "/v1/messages/count_tokens", 403),
])
def test_refused_requests_never_reach_the_provider(upstream, tmp_path, token, payload, path, expected):
    server, url = start(upstream.url, tmp_path / "log.jsonl")
    status, _, _ = post(url, payload, token, path)
    assert status == expected
    assert upstream.requests == []
    server.shutdown()


def test_cost_includes_cache_tokens(upstream, tmp_path):
    upstream.body = json.dumps({"usage": {"input_tokens": 1000, "output_tokens": 2000, "cache_read_input_tokens": 10000,
                                          "cache_creation_input_tokens": 4000}}).encode()
    server, url = start(upstream.url, tmp_path / "log.jsonl")
    post(url)
    expected = (1000 * 0.10 + 2000 * 0.50 + 10000 * 0.01 + 4000 * 0.125) / 1e6
    assert server.gateway.spent == pytest.approx(expected)
    server.shutdown()


def test_prompt_over_the_threshold_pays_the_higher_price_for_everything(upstream, tmp_path):
    # 90k of cache reads plus 20k of input: the prompt is 110k even though most of it is cached
    upstream.body = json.dumps({"usage": {"input_tokens": 20000, "output_tokens": 1000, "cache_read_input_tokens": 90000}}).encode()
    server, url = start(upstream.url, tmp_path / "log.jsonl")
    post(url)
    expected = (20000 * 0.10 + 1000 * 0.50 + 90000 * 0.01) * 5 / 1e6
    assert server.gateway.spent == pytest.approx(expected)
    server.shutdown()


def test_streamed_usage_is_counted_and_passed_through(upstream, tmp_path):
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 500, "output_tokens": 1, "cache_read_input_tokens": 100}}},
        {"type": "content_block_delta", "delta": {"text": "hello"}},
        {"type": "message_delta", "usage": {"output_tokens": 300}},
    ]
    upstream.body = "".join(f"event: x\ndata: {json.dumps(e)}\n\n" for e in events).encode()
    upstream.content_type = "text/event-stream"
    server, url = start(upstream.url, tmp_path / "log.jsonl")
    status, answer, _ = post(url)
    assert status == 200 and answer == upstream.body
    row = json.loads((tmp_path / "log.jsonl").read_text())
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"], row["stream"]) == (500, 300, 100, True)
    server.shutdown()


def test_openai_usage_does_not_count_cached_tokens_twice(upstream, tmp_path):
    upstream.body = json.dumps({"usage": {"prompt_tokens": 1000, "completion_tokens": 10,
                                          "prompt_tokens_details": {"cached_tokens": 400}}}).encode()
    server, url = start(upstream.url, tmp_path / "log.jsonl", auth="bearer")
    post(url, path="/v1/chat/completions")
    row = json.loads((tmp_path / "log.jsonl").read_text())
    assert (row["input_tokens"], row["cache_read_tokens"], row["output_tokens"]) == (600, 400, 10)
    server.shutdown()


def test_budget_stops_calls_and_survives_a_restart(upstream, tmp_path):
    log = tmp_path / "log.jsonl"
    upstream.body = json.dumps({"usage": {"input_tokens": 0, "output_tokens": 200_000}}).encode()  # $0.10 per call
    server, url = start(upstream.url, log, max_usd=0.15)
    assert post(url)[0] == 200
    assert post(url)[0] == 200  # 0.10 spent is under the cap, so this one still goes through
    assert len(upstream.requests) == 2
    status, answer, _ = post(url)
    assert status == 403 and b"budget" in answer
    assert len(upstream.requests) == 2
    server.shutdown()
    restarted, url = start(upstream.url, log, max_usd=0.15)
    assert restarted.gateway.spent == pytest.approx(0.20)
    assert post(url)[0] == 403
    assert len(upstream.requests) == 2
    restarted.shutdown()


def test_log_has_tokens_but_no_content_and_no_key(upstream, tmp_path):
    log = tmp_path / "log.jsonl"
    server, url = start(upstream.url, log)
    post(url)
    text = log.read_text()
    assert SECRET_TEXT not in text and "answer" not in text and KEY not in text
    assert json.loads(text)["input_tokens"] == 1000
    server.shutdown()


def test_provider_errors_are_passed_on_and_not_billed(upstream, tmp_path):
    upstream.status = 400
    upstream.body = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}).encode()
    server, url = start(upstream.url, tmp_path / "log.jsonl")
    status, answer, _ = post(url)
    assert status == 400 and b"invalid_request_error" in answer
    assert server.gateway.spent == 0
    server.shutdown()


def test_unreachable_provider_gives_502_without_cost(tmp_path):
    server, url = start("http://127.0.0.1:9", tmp_path / "log.jsonl")
    status, _, _ = post(url)
    assert status == 502
    assert server.gateway.spent == 0
    server.shutdown()


def test_empty_key_is_refused(tmp_path):
    config = gw.Config(upstream="http://x", auth="x-api-key", model=MODEL, prices_per_mtok=PRICES)
    with pytest.raises(ValueError):
        gw.Gateway(config, "", None, tmp_path / "log.jsonl", 1.0)
