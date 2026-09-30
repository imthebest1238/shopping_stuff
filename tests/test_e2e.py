"""End to end: the real server and web UI, a real agent browser, and a scripted Claude.

The user asks for batteries, approves the purchase in the web UI, and the order is
placed on the local test shop.
"""

import json
import re
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from conftest import FakeClient, progress, reply, text, tool
from shopping_agent import agent as agent_module
from shopping_agent.config import Config
from shopping_agent.server import create_app

SCREENSHOT_DIR = Path(__file__).parent / "output"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def last_tool_output(request: dict) -> str:
    content = request["messages"][-1]["content"][-1]["content"]
    return content if isinstance(content, str) else ""


def latest_snapshot(request: dict) -> str:
    for message in reversed(request["messages"]):
        if message["role"] != "user" or isinstance(message["content"], str):
            continue
        for block in reversed(message["content"]):
            if isinstance(block.get("content"), str) and "| URL:" in block["content"]:
                return block["content"]
    raise AssertionError("no page snapshot in the conversation")


def ref_of(snapshot: str, label: str) -> str:
    match = re.search(r"^\[([\w-]+)\][^\n]*" + re.escape(label), snapshot, re.M)
    assert match, f"{label!r} not found in:\n{snapshot}"
    return match.group(1)


@pytest.fixture
def server(fixture_server, monkeypatch):
    # The test shop runs on 127.0.0.1, which the agent normally refuses to open.
    monkeypatch.setattr(agent_module, "check_url_allowed", lambda url: None)
    executable = __import__("conftest").browser_executable()
    shop = f"{fixture_server}/shop.html"
    script = [
        reply(progress("Opening the shop."), tool("navigate", url=shop)),
        lambda req: reply(tool("click", ref=ref_of(latest_snapshot(req), "Add to cart"))),
        lambda req: reply(tool("request_purchase_approval", store="Test Shop",
                               items="Energizer AA Batteries, 24 pack - qty 1 - $18.99",
                               total=18.99, currency="USD", details="Ships to Home. Visa ending 4242.")),
        lambda req: reply(tool("click", ref=ref_of(latest_snapshot(req), "Place your order"))),
        lambda req: reply(text("Done! Your order was placed: **Energizer AA 24 pack** for $18.99.")),
    ]
    client = FakeClient(script)
    port = free_port()
    config = Config(port=port, data_dir=Path(tempfile.mkdtemp()), headless=True,
                    browser_executable=executable, open_ui=False)
    app = create_app(config, client=client)
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if uv.started:
            break
        time.sleep(0.1)
    yield {"url": f"http://127.0.0.1:{port}", "token": app.state.shop.token, "client": client}
    uv.should_exit = True
    thread.join(timeout=20)


def test_buy_with_approval_through_the_web_ui(server):
    from playwright.sync_api import sync_playwright

    from conftest import browser_executable

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=browser_executable())
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {exc}")
        page = browser.new_page(viewport={"width": 1100, "height": 1300})

        # Without the secret link, the UI refuses to load.
        page.goto(server["url"] + "/")
        assert "Almost there" in page.content()

        page.goto(f"{server['url']}/?token={server['token']}")
        page.wait_for_selector("text=What should I shop for?")
        page.fill("#input", "Buy a 24-pack of AA batteries from the test shop")
        page.keyboard.press("Enter")

        card = page.wait_for_selector(".card.approval", timeout=30000)
        assert "$18.99" in card.inner_text()
        assert card.query_selector("img") is not None  # screenshot of the real order page
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_DIR / "approval.png"))

        page.click("text=Approve & buy")
        page.wait_for_selector("text=Your order was placed", timeout=30000)
        page.wait_for_selector("#status[data-state='idle']", timeout=10000)
        assert "Approved" in page.inner_text(".card.approval")
        page.screenshot(path=str(SCREENSHOT_DIR / "done.png"))

        # The click really went through on the shop page.
        final_snapshot = last_tool_output(server["client"].requests[-1])
        assert "ORDER PLACED" in final_snapshot

        # Reloading the page restores the conversation.
        page.reload()
        page.wait_for_selector("text=Your order was placed")
        assert page.query_selector(".card.approval .result.ok") is not None
        browser.close()


def test_websocket_rejects_other_origins(server):
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    ws_url = server["url"].replace("http", "ws") + "/ws"
    cookie = f"shop_agent_ui={server['token']}"
    for origin, headers in [("https://evil.example", {"Cookie": cookie}), (server["url"], {})]:
        with pytest.raises((ConnectionClosed, InvalidStatus)):
            with connect(ws_url, origin=origin, additional_headers=headers) as ws:
                ws.recv(timeout=5)
    with connect(ws_url, origin=server["url"], additional_headers={"Cookie": cookie}) as ws:
        assert json.loads(ws.recv(timeout=5))["type"] == "hello"
