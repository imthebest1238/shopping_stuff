"""Password login (SHOP_PASSWORD) for using the chat page from another computer."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import FakeClient
from shopping_agent import server as server_module
from shopping_agent.config import Config
from shopping_agent.server import COOKIE, create_app


def make_client(password=None):
    config = Config(data_dir=Path(tempfile.mkdtemp()), headless=True, open_ui=False, password=password)
    app = create_app(config, client=FakeClient([]))
    # Not used as a context manager, so the lifespan (which opens the agent's browser) doesn't run.
    return TestClient(app, base_url="http://192.168.1.50:8000"), app.state.shop


def ws_hello(client, origin, cookie):
    client.cookies.clear()
    headers = {"origin": origin, "cookie": f"{COOKIE}={cookie}"}
    with client.websocket_connect("ws://192.168.1.50:8000/ws", headers=headers) as ws:
        return ws.receive_json()["type"]


def test_without_a_password_there_is_no_login():
    client, _ = make_client()
    assert client.get("/login").status_code == 403
    assert client.post("/login", data={"password": "x"}).status_code == 403
    assert client.get("/", follow_redirects=False).status_code == 403


def test_login_with_password_then_use_the_chat_from_another_computer():
    client, shop = make_client(password="correct horse")
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert 'type="password"' in client.get("/login").text

    assert client.post("/login", data={"password": "wrong"}, follow_redirects=False).status_code == 401
    assert COOKIE not in client.cookies
    response = client.post("/login", data={"password": "correct horse"}, follow_redirects=False)
    assert response.status_code == 303
    cookie = client.cookies[COOKIE]
    assert cookie == shop.session != shop.token  # the cookie depends on the password
    assert client.get("/").status_code == 200

    # The WebSocket accepts the address the browser used, but not other websites.
    assert ws_hello(client, "http://192.168.1.50:8000", cookie) == "hello"
    for origin, value in [("https://evil.example", cookie), ("http://192.168.1.50:8000", shop.token),
                          ("http://192.168.1.50:8000", "")]:
        with pytest.raises(WebSocketDisconnect):
            ws_hello(client, origin, value)


def test_secret_link_still_works_with_a_password():
    client, shop = make_client(password="pw")
    response = client.get(f"/?token={shop.token}", follow_redirects=False)
    assert response.status_code == 303 and client.cookies[COOKIE] == shop.session


def test_other_origins_are_only_allowed_with_a_password():
    client, shop = make_client()
    with pytest.raises(WebSocketDisconnect):
        ws_hello(client, "http://192.168.1.50:8000", shop.session)


def test_too_many_wrong_passwords_lock_the_login(monkeypatch):
    monkeypatch.setattr(server_module, "MAX_LOGIN_FAILURES", 2)
    client, _ = make_client(password="pw")
    for _ in range(2):
        assert client.post("/login", data={"password": "nope"}).status_code == 401
    response = client.post("/login", data={"password": "pw"}, follow_redirects=False)
    assert response.status_code == 429  # even the right password waits out the lock
    assert COOKIE not in client.cookies
