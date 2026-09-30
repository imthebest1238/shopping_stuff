from __future__ import annotations

import asyncio
import functools
import http.server
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).parent / "fixtures"


def run(coro):
    return asyncio.run(coro)


def browser_executable() -> str | None:
    """Tests use SHOP_BROWSER_EXECUTABLE if set, else Playwright's own Chromium."""
    return os.environ.get("SHOP_BROWSER_EXECUTABLE") or None


@pytest.fixture(scope="session")
def fixture_server():
    """Serve tests/fixtures over http on a free port."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.RequestHandlerClass.log_message = lambda *args: None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


# ---------------------------------------------------------------- a scripted, fake Claude

def text(value: str):
    return SimpleNamespace(type="text", text=value)


def progress(value: str):
    return SimpleNamespace(type="thinking", thinking=value, signature="sig")


def tool(name: str, call_id: str | None = None, **inputs):
    return SimpleNamespace(type="tool_use", id=call_id or f"toolu_{name}_{id(inputs)}", name=name, input=inputs)


def reply(*blocks, stop_reason: str | None = None):
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, stop_details=None, usage=usage)


class _FakeStream:
    def __init__(self, message):
        self._message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self._message


class FakeClient:
    """Stands in for anthropic.AsyncAnthropic. Each script item is a reply, or a
    function(request_kwargs) -> reply."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self.script:
            raise AssertionError("the agent called Claude more times than scripted")
        step = self.script.pop(0)
        return _FakeStream(step(kwargs) if callable(step) else step)


def tool_results(request: dict) -> list[dict]:
    """The tool_result blocks in the last message of a request."""
    last = request["messages"][-1]
    assert last["role"] == "user"
    return [b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
