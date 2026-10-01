"""Local web UI: a FastAPI app with one WebSocket that streams what the agent does.

By default only reachable from this computer (binds to 127.0.0.1), and only from a
browser that opened the secret link printed in the terminal (cookie + Origin check),
so web pages - including ones the agent visits - can't talk to it or approve orders.

With SHOP_PASSWORD set, a browser on another computer can log in with the password
instead. The WebSocket then also accepts the address the browser actually used (its
Origin must match the Host it connected to), which other websites can't fake.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import math
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anthropic
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .agent import ShoppingAgent
from .browser import BrowserSession
from .config import Config, UserSettings
from .memory import Memory

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "shop_agent_ui"
MAX_LOG_EVENTS = 1500

FORBIDDEN_PAGE = """<!doctype html><meta charset="utf-8"><title>Shopping Agent</title>
<body style="font:16px system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem">
<h1>Almost there</h1><p>For your safety, open the shopping agent using the link printed in the
terminal where you started it (it ends in <code>?token=…</code>).</p></body>"""


LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Shopping Agent</title>
<body style="font:16px system-ui;max-width:22rem;margin:4rem auto;padding:0 1rem">
<h1>Shopping Agent</h1>{error}
<form method="post" action="/login">
<label>Password<br><input type="password" name="password" autofocus required
 style="font:inherit;width:100%;box-sizing:border-box;padding:.5rem;margin:.3rem 0 1rem"></label>
<button style="font:inherit;padding:.5rem 1.2rem">Log in</button></form></body>"""

MAX_LOGIN_FAILURES = 10
LOGIN_LOCK_SECONDS = 15 * 60


def load_or_create_token(data_dir: Path) -> str:
    path = data_dir / "ui_token"
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 20:
            return token
    except OSError:
        pass
    data_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    path.write_text(token, encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return token


class Hub:
    """Fans agent events out to every open UI tab and keeps a log for page reloads."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.log: list[dict] = []
        self.status = "idle"
        self.usage: dict | None = None

    async def emit(self, event: dict) -> None:
        event = {**event, "ts": time.time()}
        if event["type"] == "status":
            self.status = event["state"]
        elif event["type"] == "usage":
            self.usage = event
        else:
            self.log.append(event)
            if len(self.log) > MAX_LOG_EVENTS:
                del self.log[: len(self.log) - MAX_LOG_EVENTS]
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:  # noqa: BLE001 - a closed tab
                self.clients.discard(ws)

    def clear(self) -> None:
        self.log.clear()
        self.usage = None


class App:
    def __init__(self, config: Config, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.config = config
        self.settings = UserSettings.load(config.settings_file)
        self.token = load_or_create_token(config.data_dir)
        # The cookie value. With a password it also depends on the password, so changing
        # the password logs out every browser.
        self.session = self.token
        if config.password:
            self.session = hmac.new(self.token.encode(), config.password.encode(), hashlib.sha256).hexdigest()
        self.login_failures: dict[str, tuple[int, float]] = {}  # client ip -> (failures, locked until)
        self.hub = Hub()
        ui_origins = {f"http://{host}:{config.port}" for host in {"127.0.0.1", "localhost", config.host}}
        self.allowed_origins = ui_origins
        self.browser = BrowserSession(
            config.profile_dir,
            headless=config.headless,
            channel=config.browser_channel,
            executable_path=config.browser_executable,
            blocked_origins=ui_origins,
        )
        self.memory = Memory.load(config.data_dir / "memory.json")
        self.agent = ShoppingAgent(config, self.browser, self.hub.emit, self.settings, client=client,
                                   memory=self.memory)
        self.task: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start_browser(self) -> bool:
        try:
            await self.browser.start()
            return True
        except Exception as exc:  # noqa: BLE001
            log.exception("browser failed to start")
            await self.hub.emit({
                "type": "error",
                "text": "Couldn't start the browser. If this is the first run, install it with "
                        f"`python -m playwright install chromium`, then restart. Details: {exc}",
            })
            return False

    async def send_message(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self.busy:
            await self.hub.emit({"type": "error", "text": "Still working on the last request - press Stop first."})
            return
        await self.hub.emit({"type": "user", "text": text})
        if not await self.start_browser():
            return
        self.task = asyncio.create_task(self.agent.run(text))

    async def stop(self) -> None:
        if self.busy:
            assert self.task is not None
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def reset(self) -> None:
        await self.stop()
        self.agent.reset()
        self.hub.clear()
        await self.hub.emit({"type": "reset"})

    async def update_settings(self, data: dict) -> None:
        try:
            limit = float(data.get("max_order_total", self.settings.max_order_total))
        except (TypeError, ValueError):
            limit = math.nan
        if not math.isfinite(limit) or limit < 0 or limit > 1_000_000:
            await self.hub.emit({"type": "error", "text": "The spending limit must be a number between 0 and 1,000,000."})
            return
        currency = str(data.get("currency", self.settings.currency)).strip().upper()[:8] or "USD"
        self.settings.max_order_total = round(limit, 2)
        self.settings.currency = currency
        self.settings.notes = str(data.get("notes", self.settings.notes))[:4000]
        self.settings.save(self.config.settings_file)
        await self.hub.emit({"type": "settings", **self.settings.to_dict()})

    def hello(self) -> dict:
        return {
            "type": "hello",
            "model": self.config.model,
            "status": self.hub.status,
            "busy": self.busy,
            "settings": self.settings.to_dict(),
            "usage": self.hub.usage,
            "events": self.hub.log,
        }


def create_app(config: Config, client: anthropic.AsyncAnthropic | None = None) -> FastAPI:
    state = App(config, client)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Open the browser right away so the user can log in to their stores.
        starter = asyncio.create_task(state.start_browser())
        yield
        starter.cancel()
        await state.stop()
        with contextlib.suppress(Exception):
            await state.browser.close()

    app = FastAPI(title="Shopping Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.shop = state
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def logged_in(response: RedirectResponse) -> RedirectResponse:
        response.set_cookie(COOKIE, state.session, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 365)
        return response

    def origin_allowed(origin: str, host: str) -> bool:
        if origin in state.allowed_origins:
            return True
        # With a password, also allow the address the browser connected to (another computer).
        return bool(config.password) and bool(host) and urlparse(origin).netloc == host.lower()

    @app.get("/")
    async def index(request: Request):
        if secrets.compare_digest(request.query_params.get("token", ""), state.token):
            return logged_in(RedirectResponse("/", status_code=303))
        if not secrets.compare_digest(request.cookies.get(COOKIE, ""), state.session):
            if config.password:
                return RedirectResponse("/login", status_code=303)
            return HTMLResponse(FORBIDDEN_PAGE, status_code=403)
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/login")
    async def login_page():
        if not config.password:
            return HTMLResponse(FORBIDDEN_PAGE, status_code=403)
        return HTMLResponse(LOGIN_PAGE.format(error=""), headers={"Cache-Control": "no-store"})

    @app.post("/login")
    async def login(request: Request):
        if not config.password:
            return HTMLResponse(FORBIDDEN_PAGE, status_code=403)
        ip = request.client.host if request.client else "?"
        failures, locked_until = state.login_failures.get(ip, (0, 0.0))
        if locked_until > time.time():
            return HTMLResponse(LOGIN_PAGE.format(
                error="<p style='color:#b00'>Too many wrong passwords. Try again in 15 minutes.</p>"), status_code=429)
        body = (await request.body())[:4096].decode("utf-8", "replace")
        password = (parse_qs(body).get("password") or [""])[0]
        if secrets.compare_digest(password.encode(), config.password.encode()):
            state.login_failures.pop(ip, None)
            return logged_in(RedirectResponse("/", status_code=303))
        failures += 1
        lock = time.time() + LOGIN_LOCK_SECONDS if failures >= MAX_LOGIN_FAILURES else 0.0
        state.login_failures[ip] = (0 if lock else failures, lock)
        log.warning("wrong password from %s", ip)
        await asyncio.sleep(1)  # slow down guessing
        return HTMLResponse(LOGIN_PAGE.format(error="<p style='color:#b00'>Wrong password.</p>"), status_code=401)

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        origin = (ws.headers.get("origin") or "").rstrip("/").lower()
        host = ws.headers.get("host") or ""
        if not origin_allowed(origin, host) or not secrets.compare_digest(ws.cookies.get(COOKIE, ""), state.session):
            await ws.close(code=4403)
            return
        await ws.accept()
        state.hub.clients.add(ws)
        try:
            await ws.send_json(state.hello())
            while True:
                data = await ws.receive_json()
                if isinstance(data, dict):
                    await handle(data)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            state.hub.clients.discard(ws)

    async def handle(data: dict) -> None:
        kind = data.get("type")
        text = str(data.get("text") or "")[:8000]
        interaction = str(data.get("id") or "")
        if kind == "message":
            await state.send_message(text)
        elif kind == "stop":
            await state.stop()
        elif kind == "reset":
            await state.reset()
        elif kind == "settings":
            await state.update_settings(data)
        elif kind == "answer":
            state.agent.resolve(interaction, "question", {"text": text})
        elif kind == "handover_done":
            state.agent.resolve(interaction, "handover", {"text": text})
        elif kind == "approval":
            approved = data.get("approved") is True
            if state.agent.is_pending(interaction, "approval"):
                await state.hub.emit({"type": "decision", "id": interaction, "approved": approved})
                state.agent.resolve(interaction, "approval", {"approved": approved, "text": text})

    return app
