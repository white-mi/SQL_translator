"""The fake model provider judges whether a request would pass the real API, so its rules need tests too."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "fake_model", Path(__file__).resolve().parents[1] / "case" / "researcher" / "fake_model.py")
fake = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fake
SPEC.loader.exec_module(fake)

MODEL = "claude-haiku-5-5"
SYSTEM = [{"type": "text", "text": "You are a researcher."}]
TOOLS = [{"name": "terminal"}]
USER = {"role": "user", "content": "start"}
ADAPTIVE = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}}


def request(**fields) -> dict:
    return {"model": MODEL, "max_tokens": 1000, "system": SYSTEM, "tools": TOOLS, "messages": [USER], **ADAPTIVE, **fields}


def tool_loop(first_message: dict = USER, system=SYSTEM, signature: str | None = None) -> dict:
    """A second request: the assistant called a tool, the result comes back."""
    opening = request()
    signed = signature or fake.conversation_digest(opening, 1)
    return request(system=system, messages=[
        first_message,
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "Let me look.", "signature": signed},
                                          {"type": "tool_use", "id": "t1", "name": "terminal", "input": {"command": "ls"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ])


def test_a_clean_request_passes():
    assert fake.request_problems(request(), MODEL) == []


def test_what_haiku_5_5_rejects_is_rejected():
    for bad in ({"temperature": 1}, {"top_p": 0.9}, {"top_k": 5},
                {"thinking": {"type": "enabled", "budget_tokens": 1000}},
                {"output_config": {"effort": "extreme"}},
                {"max_tokens": 200_000},
                {"model": "claude-opus-5-5"},
                {"messages": [USER, {"role": "assistant", "content": "prefill"}]}):
        assert fake.request_problems(request(**bad), MODEL), bad


def test_replayed_thinking_passes():
    assert fake.request_problems(tool_loop(), MODEL) == []


def test_a_plain_string_and_a_text_block_are_the_same_message():
    # Hermes moves the cache marker, which changes the spelling of the first message between requests
    block = {"role": "user", "content": [{"type": "text", "text": "start", "cache_control": {"type": "ephemeral"}}]}
    assert fake.request_problems(tool_loop(first_message=block), MODEL) == []


def test_edited_history_invalidates_the_thinking_block():
    edited = {"role": "user", "content": "start, with an edit"}
    problems = fake.request_problems(tool_loop(first_message=edited), MODEL)
    assert any("Invalid `signature`" in p for p in problems)


def test_a_changed_system_prompt_invalidates_the_thinking_block():
    problems = fake.request_problems(tool_loop(system=[{"type": "text", "text": "Changed."}]), MODEL)
    assert any("Invalid `signature`" in p for p in problems)


def test_a_tool_loop_without_its_thinking_is_a_warning_not_a_rejection():
    # the API reference names stripping thinking blocks as the recovery from an edited history
    body = tool_loop()
    assert fake.thinking_warnings(body) == []
    body["messages"][1]["content"] = body["messages"][1]["content"][1:]
    assert fake.request_problems(body, MODEL) == []
    assert any("without its thinking" in w for w in fake.thinking_warnings(body))


def test_a_stream_carries_tool_call_and_usage():
    blocks = [{"type": "thinking", "thinking": "hm", "signature": "s"},
              {"type": "tool_use", "id": "t1", "name": "terminal", "input": {"command": "ls"}}]
    usage = {"input_tokens": 10, "output_tokens": 7, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    events = [json.loads(line[5:]) for line in fake.stream_events(MODEL, blocks, "tool_use", usage).decode().splitlines()
              if line.startswith("data:")]
    assert events[0]["message"]["usage"]["input_tokens"] == 10
    assert any(e.get("delta", {}).get("signature") == "s" for e in events)
    assert any(e.get("delta", {}).get("partial_json") == json.dumps({"command": "ls"}) for e in events)
    assert events[-2]["usage"]["output_tokens"] == 7 and events[-1]["type"] == "message_stop"


def test_only_requests_with_tools_advance_the_script():
    script = fake.Script([{"command": "echo a"}, {"text": "done"}], None, 0)
    blocks, stop, _ = script.next_blocks({"messages": []})  # a helper call, for example compression
    assert stop == "end_turn" and blocks[0]["text"].startswith("Summary")
    blocks, stop, _ = script.next_blocks({"tools": TOOLS, "messages": [USER]})
    assert stop == "tool_use" and blocks[1]["input"] == {"command": "echo a"}
    blocks, stop, _ = script.next_blocks({"tools": TOOLS, "messages": [USER]})
    assert stop == "end_turn" and blocks[1]["text"] == "done"
