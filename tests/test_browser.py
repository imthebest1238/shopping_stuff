"""The browser layer against a real (headless) Chromium and a local test shop."""

import tempfile
from pathlib import Path

import pytest

from conftest import browser_executable, run
from shopping_agent import safety
from shopping_agent.browser import BrowserError, BrowserSession


async def start_browser(blocked=None) -> BrowserSession:
    browser = BrowserSession(Path(tempfile.mkdtemp()) / "profile", headless=True,
                             executable_path=browser_executable(), blocked_origins=blocked)
    try:
        await browser.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Chromium not available: {exc}")
    return browser


async def open_shop(base_url: str) -> BrowserSession:
    browser = await start_browser()
    await browser.navigate(f"{base_url}/shop.html")
    return browser


def ref_for(snapshot: str, label: str) -> str:
    for line in snapshot.splitlines():
        if line.startswith("[") and label in line:
            return line[1:line.index("]")]
    raise AssertionError(f"{label!r} not in snapshot:\n{snapshot}")


def test_snapshot_and_actions(fixture_server):
    async def scenario():
        browser = await open_shop(fixture_server)
        try:
            snap = await browser.snapshot()
            assert "Title: Test Shop - AA Batteries" in snap
            assert "Energizer AA Batteries, 24 pack" in snap and "$18.99" in snap
            assert "SECRET HIDDEN TEXT" not in snap
            assert "<label for checkbox checked>Add 2-year protection plan" in snap

            search = ref_for(snap, 'placeholder="Search products"')
            await browser.type_text(search, "aa batteries", press_enter=True)
            snap = await browser.snapshot()
            assert "searched: aa batteries" in snap

            await browser.click(ref_for(snap, "<button>Add to cart"))
            await browser.click(ref_for(snap, "protection plan"))
            chosen = await browser.select_option(ref_for(snap, "<select"), "three")
            snap = await browser.snapshot()
            assert "Cart: 1 item" in snap
            assert "<label for checkbox>Add 2-year protection plan" in snap  # unchecked now
            assert chosen == "3"

            # Refs stay stable between snapshots of the same page.
            assert ref_for(snap, 'placeholder="Search products"') == search

            with pytest.raises(BrowserError):
                await browser.click("9999")

            await browser.scroll("down")
            await browser.scroll("down")
            await browser.scroll("down")
            snap = await browser.snapshot()
            assert "Footer text at the very bottom" in snap
            assert "Energizer" not in snap  # scrolled past
        finally:
            await browser.close()
    run(scenario())


def test_safety_info_from_real_page(fixture_server):
    async def scenario():
        browser = await open_shop(fixture_server)
        try:
            snap = await browser.snapshot()
            order = await browser.element_info(ref_for(snap, "Place your order"))
            buy_now = await browser.element_info(ref_for(snap, "Buy Now"))
            card = await browser.element_info(ref_for(snap, 'label="Card number"'))
            password = await browser.element_info(ref_for(snap, "type=password"))
            promo = await browser.element_info(ref_for(snap, "Promo code"))
            add = await browser.element_info(ref_for(snap, "Add to cart"))
            assert safety.click_purchase_reason(order)
            assert safety.click_purchase_reason(buy_now)
            assert safety.click_purchase_reason(add) is None
            assert safety.sensitive_field_reason(card)
            assert safety.sensitive_field_reason(password)
            assert safety.sensitive_field_reason(promo) is None
            assert safety.enter_submits_purchase(promo)
        finally:
            await browser.close()
    run(scenario())


def test_ui_origin_is_blocked_in_agent_browser(fixture_server):
    async def scenario():
        browser = await start_browser(blocked={fixture_server})
        try:
            with pytest.raises(BrowserError):
                await browser.navigate(f"{fixture_server}/shop.html")
        finally:
            await browser.close()
    run(scenario())
