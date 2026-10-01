"""When the agent's browser runs on a server (Docker), the chat links to it (SHOP_BROWSER_VIEW_PORT)."""

import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from conftest import FakeClient, browser_executable, reply, text, tool
from shopping_agent.config import Config
from shopping_agent.prompts import build_system_prompt
from shopping_agent.config import UserSettings
from shopping_agent.server import create_app
from test_e2e import free_port

VIEW = "http://127.0.0.1:3456/vnc.html?autoconnect=1&resize=scale"


def test_remote_view_text_only_in_remote_prompt():
    assert "Open the shop browser" in build_system_prompt(UserSettings(), remote_view=True)
    assert "Open the shop browser" not in build_system_prompt(UserSettings())


@pytest.fixture
def remote_server():
    client = FakeClient([
        reply(tool("hand_over_to_user", reason="Please log in to Migros with your email and password.")),
        reply(text("Thanks, you're logged in.")),
    ])
    port = free_port()
    config = Config(port=port, data_dir=Path(tempfile.mkdtemp()), headless=True,
                    browser_executable=browser_executable(), open_ui=False, browser_view_port=3456)
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


def test_handover_card_links_to_the_shop_browser(remote_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=browser_executable())
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {exc}")
        page = browser.new_page()
        page.goto(f"{remote_server['url']}/?token={remote_server['token']}")
        page.wait_for_selector("text=What should I shop for?")
        assert page.get_attribute("#view-link", "href") == VIEW
        assert page.is_visible("#view-link")
        assert "on the server" in page.inner_text("#empty-where")

        page.fill("#input", "Log me in to Migros")
        page.keyboard.press("Enter")
        card = page.wait_for_selector("text=Your turn: open the shop browser", timeout=30000)
        card = page.query_selector(".card:has-text('Your turn')")
        link = card.query_selector("a.button.primary")
        assert link.inner_text().startswith("Open the shop browser") and link.get_attribute("href") == VIEW
        card.query_selector("button.primary").click()
        page.wait_for_selector("text=you're logged in", timeout=30000)
        browser.close()

    system = remote_server["client"].requests[0]["system"][0]["text"]
    assert "Never say you opened a tab" in system
