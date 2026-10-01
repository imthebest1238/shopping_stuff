"""The agent loop, driven by a scripted fake Claude and a fake browser."""

import asyncio

from conftest import FakeClient, progress, reply, run, text, tool, tool_results
from shopping_agent.agent import BETAS, ShoppingAgent
from shopping_agent.config import Config, UserSettings

ELEMENTS = {
    "1": {"tag": "input", "type": "text", "name": "q", "placeholder": "Search", "formSubmitLabels": ["Search"]},
    "2": {"tag": "button", "type": "submit", "text": "Add to cart"},
    "3": {"tag": "input", "type": "submit", "value": "Place your order", "name": "placeYourOrder1"},
    "4": {"tag": "input", "type": "text", "autocomplete": "cc-number", "name": "cardnumber"},
    "5": {"tag": "input", "type": "text", "name": "promo", "formSubmitLabels": ["Place your order"]},
}


class FakeBrowser:
    def __init__(self, url="https://www.example-shop.com/checkout"):
        self.url = url
        self.actions: list[tuple] = []

    async def snapshot(self, max_chars=12000):
        return f"URL: {self.url}\n[1]<input> [2]<button>Add to cart [3]<input type=submit>Place your order"

    async def element_info(self, ref):
        return {"host": None, **ELEMENTS[ref]}

    async def focused_element_info(self):
        return None

    async def click(self, ref):
        self.actions.append(("click", ref))

    async def type_text(self, ref, value, press_enter):
        self.actions.append(("type", ref, value, press_enter))

    async def navigate(self, url):
        self.actions.append(("navigate", url))
        self.url = url

    async def current_url(self):
        return self.url

    async def current_title(self):
        return "Checkout"

    async def screenshot(self):
        return b"\xff\xd8fake-jpeg"


def make_agent(script, settings=None, on_event=None):
    events: list[dict] = []
    holder = {}

    async def emit(event):
        events.append(event)
        if on_event:
            await on_event(event, holder["agent"])

    client = FakeClient(script)
    browser = FakeBrowser()
    agent = ShoppingAgent(Config(max_steps=20), browser, emit, settings or UserSettings(), client=client)
    holder["agent"] = agent
    return agent, client, browser, events


def auto_approve(approved: bool, answer_text: str = ""):
    async def on_event(event, agent):
        if event["type"] == "approval":
            asyncio.get_running_loop().call_soon(
                agent.resolve, event["id"], "approval", {"approved": approved, "text": answer_text})
    return on_event


def assert_history_valid(messages):
    """Every tool_use must be answered by a tool_result in the next message."""
    for i, message in enumerate(messages):
        if message["role"] != "assistant":
            continue
        calls = [b.id for b in message["content"] if getattr(b, "type", None) == "tool_use"]
        if not calls:
            continue
        answered = {b["tool_use_id"] for b in messages[i + 1]["content"] if b.get("type") == "tool_result"}
        assert set(calls) <= answered, f"unanswered tool calls at message {i}"


def test_request_shape():
    agent, client, _, events = make_agent([reply(text("Hi! What should I buy?"))])
    run(agent.run("hello"))
    request = client.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["thinking"] == {"type": "adaptive", "display": "updates"}
    assert request["output_config"] == {"effort": "medium"}
    assert request["fallbacks"] == "default"
    assert set(request["betas"]) == set(BETAS)
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["context_management"]["edits"][0]["type"] == "clear_tool_uses_20250919"
    assert all(t["eager_input_streaming"] for t in request["tools"])
    assert "tool_choice" not in request  # forced tool choice is rejected on this model
    assert {"type": "assistant", "text": "Hi! What should I buy?"} in [
        {k: e[k] for k in ("type", "text")} for e in events if e["type"] == "assistant"]
    assert events[-1] == {"type": "status", "state": "idle"}


def test_purchase_click_is_blocked_without_approval():
    agent, client, browser, events = make_agent([
        reply(tool("click", ref="3")),
        reply(text("I need your approval first.")),
    ])
    run(agent.run("buy it"))
    result = tool_results(client.requests[1])[0]
    assert result["is_error"] and "request_purchase_approval" in result["content"]
    assert ("click", "3") not in browser.actions
    assert any(e["type"] == "action_done" and not e["ok"] for e in events)


def test_approved_purchase_allows_exactly_one_order_click():
    approval_call = tool("request_purchase_approval", store="Example Shop", items="1x Soap - $5.00",
                         total=5.0, currency="USD", details="Ships to home, Visa ending 1234")
    agent, client, browser, events = make_agent([
        reply(progress("Checking the order page."), approval_call),
        reply(tool("click", ref="3")),
        reply(tool("click", ref="3")),  # a second order click must be blocked again
        reply(text("Order placed.")),
    ], on_event=auto_approve(True))
    run(agent.run("buy soap"))

    approval_event = next(e for e in events if e["type"] == "approval")
    assert approval_event["total"] == 5.0 and approval_event["url"] == browser.url
    assert approval_event["screenshot"]  # the user sees the real page
    assert "APPROVED" in tool_results(client.requests[1])[0]["content"]
    assert not tool_results(client.requests[2])[0].get("is_error")
    assert tool_results(client.requests[3])[0]["is_error"]
    assert browser.actions.count(("click", "3")) == 1
    assert {"type": "progress", "text": "Checking the order page."} in [
        {k: e[k] for k in ("type", "text")} for e in events if e["type"] == "progress"]
    assert_history_valid(agent.messages)


def test_declined_purchase_keeps_button_blocked():
    agent, client, browser, _ = make_agent([
        reply(tool("request_purchase_approval", store="S", items="x", total=5, currency="USD", details="d")),
        reply(tool("click", ref="3")),
        reply(text("OK, I won't buy it.")),
    ], on_event=auto_approve(False, "too expensive"))
    run(agent.run("buy"))
    declined = tool_results(client.requests[1])[0]["content"]
    assert "DECLINED" in declined and "too expensive" in declined
    assert tool_results(client.requests[2])[0]["is_error"]
    assert ("click", "3") not in browser.actions


def test_orders_over_the_limit_are_declined_without_asking():
    agent, client, _, events = make_agent([
        reply(tool("request_purchase_approval", store="S", items="TV", total=899.0, currency="USD", details="d")),
        reply(text("That's over your limit.")),
    ], settings=UserSettings(max_order_total=100))
    run(agent.run("buy a tv"))
    assert "over the user's limit" in tool_results(client.requests[1])[0]["content"]
    assert not any(e["type"] == "approval" for e in events)


def test_card_fields_and_order_submitting_enter_are_blocked():
    agent, client, browser, _ = make_agent([
        reply(tool("type_text", ref="4", text="4111 1111 1111 1111", press_enter=False)),
        reply(tool("type_text", ref="5", text="SAVE10", press_enter=True)),
        reply(tool("type_text", ref="5", text="SAVE10", press_enter=False)),
        reply(text("done")),
    ])
    run(agent.run("checkout"))
    assert "hand_over_to_user" in tool_results(client.requests[1])[0]["content"]
    assert "submit the order form" in tool_results(client.requests[2])[0]["content"]
    assert not tool_results(client.requests[3])[0].get("is_error")
    assert browser.actions == [("type", "5", "SAVE10", False)]


def test_navigation_to_local_addresses_is_refused():
    agent, client, browser, _ = make_agent([
        reply(tool("navigate", url="http://127.0.0.1:8000/?token=abc")),
        reply(text("ok")),
    ])
    run(agent.run("go"))
    assert tool_results(client.requests[1])[0]["is_error"]
    assert browser.actions == []


def test_invalid_tool_input_is_reported_back():
    agent, client, _, _ = make_agent([
        reply(tool("type_text", ref="1", text="soap")),  # press_enter missing
        reply(text("ok")),
    ])
    run(agent.run("search"))
    result = tool_results(client.requests[1])[0]
    assert result["is_error"] and "INVALID_INPUT" in result["content"]


def test_stop_while_waiting_leaves_a_valid_history():
    async def scenario():
        agent, client, _, events = make_agent([
            reply(tool("ask_user", question="Which size?")),
            reply(text("Continuing.")),
        ])
        task = asyncio.create_task(agent.run("buy shoes"))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if any(e["type"] == "question" for e in events):
                break
        task.cancel()
        await task  # run() handles the cancellation itself
        assert_history_valid(agent.messages)
        assert agent.pending_events() == []
        # The conversation can continue afterwards.
        await agent.run("size 42")
        assert client.requests[-1]["messages"][-1] == {"role": "user", "content": "size 42"}
        assert_history_valid(agent.messages)
    run(scenario())


def test_questions_are_answered_by_the_user():
    async def on_event(event, agent):
        if event["type"] == "question":
            asyncio.get_running_loop().call_soon(agent.resolve, event["id"], "question", {"text": "size 42"})
    agent, client, _, _ = make_agent([
        reply(tool("ask_user", question="Which size?")),
        reply(text("Got it.")),
    ], on_event=on_event)
    run(agent.run("buy shoes"))
    assert tool_results(client.requests[1])[0]["content"] == "The user answered: size 42"


def test_settings_change_is_appended_not_edited():
    settings = UserSettings(max_order_total=50, notes="I like Target")
    agent, client, _, _ = make_agent([reply(text("a")), reply(text("b"))], settings=settings)
    run(agent.run("first"))
    system_before = client.requests[0]["system"]
    settings.notes = "Now I prefer Walmart"
    run(agent.run("second"))
    second = client.requests[1]
    assert second["system"] == system_before  # frozen for the conversation
    assert second["messages"][-1]["role"] == "system"
    assert "Walmart" in second["messages"][-1]["content"]
    # Earlier messages are untouched (append-only history).
    assert second["messages"][: len(client.requests[0]["messages"])] == client.requests[0]["messages"]


def test_refusal_is_not_added_to_history():
    agent, client, _, events = make_agent([reply(text("partial"), stop_reason="refusal")])
    run(agent.run("do something"))
    assert agent.messages == [{"role": "user", "content": "do something"}]
    assert any(e["type"] == "error" and "declined" in e["text"] for e in events)


def test_failed_request_after_settings_change_recovers():
    settings = UserSettings(notes="a")

    def fail(_request):
        raise ValueError("unreadable")  # every retry fails -> the run errors out

    agent, client, _, events = make_agent([reply(text("hi")), fail, fail, fail, reply(text("ok"))], settings=settings)
    run(agent.run("first"))
    settings.notes = "b"
    run(agent.run("second"))  # errors after appending the settings note
    assert any(e["type"] == "error" for e in events)
    run(agent.run("third"))
    roles = [m["role"] for m in client.requests[-1]["messages"]]
    assert roles[-2:] == ["user", "system"]  # the note is re-sent after the newest message
    for before, after in zip(roles, roles[1:]):
        assert not (before == "system" and after == "user")
