"""Run the agent against the real Anthropic SDK with a mocked HTTP transport:
checks the exact request we send and that streamed replies are parsed and replayed."""

import json

import anthropic
import httpx2

from conftest import run
from shopping_agent.agent import BETAS, ShoppingAgent
from shopping_agent.config import Config, UserSettings
from test_agent import FakeBrowser


def sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def streamed_message(blocks: list[tuple[dict, list[dict]]], stop_reason: str) -> bytes:
    events = [{
        "type": "message_start",
        "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                    "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 1200, "output_tokens": 1,
                              "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0}},
    }]
    for index, (start, deltas) in enumerate(blocks):
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events += [{"type": "content_block_delta", "index": index, "delta": d} for d in deltas]
        events.append({"type": "content_block_stop", "index": index})
    events.append({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                   "usage": {"output_tokens": 40}})
    events.append({"type": "message_stop"})
    return sse(*events)


FIRST = streamed_message([
    ({"type": "thinking", "thinking": "", "signature": ""},
     [{"type": "thinking_delta", "thinking": "Opening the store."}, {"type": "signature_delta", "signature": "sig1"}]),
    ({"type": "tool_use", "id": "toolu_01", "name": "navigate", "input": {}},
     [{"type": "input_json_delta", "partial_json": '{"url": "https://www.'},
      {"type": "input_json_delta", "partial_json": 'amazon.com"}'}]),
], "tool_use")

SECOND = streamed_message([
    ({"type": "text", "text": ""}, [{"type": "text_delta", "text": "Found it for $5."}]),
], "end_turn")


def test_real_sdk_request_and_replay():
    bodies: list[dict] = []
    headers: list[httpx2.Headers] = []
    replies = [FIRST, SECOND, SECOND]

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        headers.append(request.headers)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=replies.pop(0))

    client = anthropic.AsyncAnthropic(
        api_key="sk-test", max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    events: list[dict] = []

    async def emit(event):
        events.append(event)

    settings = UserSettings(notes="size M")
    browser = FakeBrowser()
    agent = ShoppingAgent(Config(), browser, emit, settings, client=client)
    run(agent.run("find soap"))

    assert browser.actions == [("navigate", "https://www.amazon.com")]
    first, second = bodies
    assert first["model"] == "claude-opus-5-5"
    assert first["stream"] is True
    assert first["fallbacks"] == "default"
    assert first["thinking"] == {"type": "adaptive", "display": "updates"}
    assert first["output_config"] == {"effort": "medium"}
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["tools"][0]["eager_input_streaming"] is True
    for beta in BETAS:
        assert beta in headers[0]["anthropic-beta"]

    # The assistant turn is replayed exactly, thinking signature included, then the tool result.
    assistant = second["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {"type": "thinking", "thinking": "Opening the store.", "signature": "sig1"}
    assert assistant["content"][1]["input"] == {"url": "https://www.amazon.com"}
    result = second["messages"][2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_01"

    assert {"type": "progress", "text": "Opening the store."} in [
        {"type": e["type"], "text": e.get("text")} for e in events if e["type"] == "progress"]
    assert any(e["type"] == "assistant" and e["text"] == "Found it for $5." for e in events)
    usage = [e for e in events if e["type"] == "usage"][-1]
    assert usage["cost_usd"] > 0

    # A settings change mid-conversation goes out as a mid-conversation system message.
    settings.notes = "size L"
    run(agent.run("thanks"))
    third = bodies[2]
    assert third["system"] == first["system"]
    assert third["messages"][-1]["role"] == "system" and "size L" in third["messages"][-1]["content"]
