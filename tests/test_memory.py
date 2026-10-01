"""Long-term memory: remember / forget tools, order history, and the system prompt."""

from conftest import FakeClient, reply, run, text, tool, tool_results
from test_agent import FakeBrowser, auto_approve, make_agent
from shopping_agent.agent import ShoppingAgent
from shopping_agent.config import Config, UserSettings
from shopping_agent.memory import Memory


def test_memory_saves_loads_and_dedupes(tmp_path):
    path = tmp_path / "memory.json"
    memory = Memory.load(path)
    first = memory.remember("Family of 4,  kids like apples")
    assert memory.remember("family of 4, kids like apples") == first  # no duplicates
    memory.remember("Milk: 2 litres a week")
    memory.record_order("Migros", "6x Apples - 3.90", 3.9, "CHF")

    loaded = Memory.load(path)
    assert [f["text"] for f in loaded.facts] == ["Family of 4, kids like apples", "Milk: 2 litres a week"]
    assert loaded.orders[0]["store"] == "Migros"
    assert loaded.remember("New fact")["id"] == "m3"  # ids keep counting after a reload
    assert loaded.forget("[m1]")["text"] == "Family of 4, kids like apples"
    assert loaded.forget("m1") is None


def test_memory_is_in_the_system_prompt():
    memory = Memory()
    memory.remember("The kids don't like mushrooms")
    memory.record_order("Migros", "1x Bread - 2.50", 2.5, "CHF")
    agent, client, _, _ = make_agent([reply(text("Hi"))])
    agent.memory = memory
    run(agent.run("hello"))
    system = client.requests[0]["system"][0]["text"]
    assert "[m1] The kids don't like mushrooms" in system
    assert "1x Bread - 2.50" in system and "2.50 CHF" in system


def test_remember_and_forget_tools(tmp_path):
    memory = Memory.load(tmp_path / "memory.json")
    events: list[dict] = []

    async def emit(event):
        events.append(event)

    client = FakeClient([
        reply(tool("remember", text="We are a family of 4")),
        reply(tool("forget", id="m1")),
        reply(tool("forget", id="m99")),
        reply(text("Done.")),
    ])
    agent = ShoppingAgent(Config(max_steps=20), FakeBrowser(), emit, UserSettings(), client=client, memory=memory)
    run(agent.run("remember we are 4, then forget it"))

    assert "[m1]" in tool_results(client.requests[1])[0]["content"]
    assert "Deleted [m1]" in tool_results(client.requests[2])[0]["content"]
    assert tool_results(client.requests[3])[0]["is_error"]
    assert Memory.load(tmp_path / "memory.json").facts == []
    notices = [e["text"] for e in events if e["type"] == "notice"]
    assert "Remembered: We are a family of 4" in notices and "Forgot: We are a family of 4" in notices


def test_only_placed_orders_are_recorded():
    approval_call = tool("request_purchase_approval", store="Migros", items="6x Apples - 3.90",
                         total=3.9, currency="CHF", details="Home delivery")
    agent, _, _, _ = make_agent([
        reply(approval_call), reply(tool("click", ref="3")), reply(text("Ordered.")),
    ], on_event=auto_approve(True))
    run(agent.run("buy apples"))
    assert agent.memory.orders == [
        {"date": agent.memory.orders[0]["date"], "store": "Migros", "items": "6x Apples - 3.90",
         "total": 3.9, "currency": "CHF"}]

    declined, _, _, _ = make_agent([reply(approval_call), reply(text("OK."))], on_event=auto_approve(False))
    run(declined.run("buy apples"))
    assert declined.memory.orders == []
