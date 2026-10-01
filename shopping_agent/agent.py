"""The shopping agent: a Claude tool-use loop over a real browser.

The conversation history is append-only (thinking blocks are passed back exactly
as received), the system prompt and tool list are frozen for a conversation, and
settings changes mid-conversation are sent as a mid-conversation system message.
That keeps the prompt cache warm and replayed thinking blocks valid.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import anthropic

from . import safety
from .browser import BrowserError, BrowserSession
from .config import Config, UserSettings
from .memory import Memory
from .prompts import KEEP_RESULTS_TOOLS, TOOL_SCHEMAS, TOOLS, build_system_prompt, settings_text

log = logging.getLogger(__name__)

Emit = Callable[[dict], Awaitable[None]]

BETAS = [
    "server-side-fallback-2026-07-01",  # fallbacks="default": retry a declined request on another model
    "thinking-display-updates-2026-08-18",  # progress notes between tool calls come back as text
    "context-management-2025-06-27",  # clear old page snapshots once the context gets large
]

# Old page snapshots are the bulk of the context; drop the old ones once it grows.
CONTEXT_MANAGEMENT = {
    "edits": [
        {
            "type": "clear_tool_uses_20250919",
            "trigger": {"type": "input_tokens", "value": 60000},
            "keep": {"type": "tool_uses", "value": 8},
            "clear_at_least": {"type": "input_tokens", "value": 15000},
            "exclude_tools": KEEP_RESULTS_TOOLS,
        }
    ]
}

MAX_TOKENS = 64000
SNAPSHOT_CHARS = 12000

# USD per million tokens: input, output, cache read, cache write (5 min). For the cost estimate only.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
}


class ToolInputError(ValueError):
    pass


def validate_tool_input(name: str, raw: Any) -> dict:
    """Check a tool call's input against its schema (inputs are streamed, so not pre-validated)."""
    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        raise ToolInputError(f"unknown tool {name!r}")
    if not isinstance(raw, dict):
        raise ToolInputError("input must be a JSON object")
    clean: dict = {}
    for key in schema.get("required", []):
        if key not in raw:
            raise ToolInputError(f"missing required field {key!r}")
    for key, value in raw.items():
        spec = schema["properties"].get(key)
        if spec is None:
            raise ToolInputError(f"unexpected field {key!r}")
        kind = spec["type"]
        if kind == "string":
            if key == "ref" and isinstance(value, int) and not isinstance(value, bool):
                value = str(value)
            if not isinstance(value, str):
                raise ToolInputError(f"{key!r} must be a string")
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ToolInputError(f"{key!r} must be true or false")
        elif kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ToolInputError(f"{key!r} must be a number")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolInputError(f"{key!r} must be one of {spec['enum']}")
        clean[key] = value
    return clean


def check_url_allowed(url: str) -> str | None:
    """The agent may only visit public http(s) sites - never local files or the local network."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return f"only http(s) websites can be opened, not {parsed.scheme}: URLs"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return "that URL has no host name"
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return "local network addresses are not allowed"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
        return "local network addresses are not allowed"
    return None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0

    def add(self, usage: Any) -> None:
        self.requests += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

    def cost(self, model: str) -> float | None:
        prices = PRICES.get(model)
        if prices is None:
            return None
        p_in, p_out, p_read, p_write = prices
        return (
            self.input_tokens * p_in
            + self.output_tokens * p_out
            + self.cache_read_tokens * p_read
            + self.cache_write_tokens * p_write
        ) / 1_000_000


@dataclass
class _Pending:
    kind: str
    future: asyncio.Future
    event: dict = field(default_factory=dict)


class ShoppingAgent:
    def __init__(
        self,
        config: Config,
        browser: BrowserSession,
        emit: Emit,
        settings: UserSettings,
        client: anthropic.AsyncAnthropic | None = None,
        memory: Memory | None = None,
    ) -> None:
        self.config = config
        self.browser = browser
        self.emit = emit
        self.settings = settings
        self.client = client or anthropic.AsyncAnthropic()
        self.memory = memory or Memory()
        self.messages: list[dict] = []
        self.system_prompt: str | None = None
        self._conversation_settings: str | None = None
        self._approval: safety.PurchaseApproval | None = None
        self._approval_order: dict | None = None  # store/items/currency of the approved order, for memory
        self._pending: dict[str, _Pending] = {}
        self.usage = Usage()

    # ------------------------------------------------------------------ public API

    def reset(self) -> None:
        """Start a new conversation (the browser and its logins stay as they are)."""
        self.cancel_pending()
        self.messages = []
        self.system_prompt = None
        self._conversation_settings = None
        self._approval = None
        self._approval_order = None
        self.usage = Usage()

    def pending_events(self) -> list[dict]:
        return [p.event for p in self._pending.values()]

    def is_pending(self, interaction_id: str, kind: str) -> bool:
        pending = self._pending.get(interaction_id)
        return pending is not None and pending.kind == kind and not pending.future.done()

    def resolve(self, interaction_id: str, kind: str, data: dict) -> bool:
        """The user answered a question / finished a hand-over / decided on an approval."""
        pending = self._pending.get(interaction_id)
        if pending is None or pending.kind != kind or pending.future.done():
            return False
        pending.future.set_result(data)
        return True

    def cancel_pending(self) -> None:
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.cancel()
        self._pending.clear()

    async def run(self, user_text: str) -> None:
        """Handle one user message: work until Claude has nothing left to do."""
        settings_now = settings_text(self.settings)
        if self.system_prompt is None:
            self.system_prompt = build_system_prompt(self.settings, memory_text=self.memory.prompt_text(),
                                                     remote_view=bool(self.config.browser_view_port))
            self._conversation_settings = settings_now
        if self.messages and self.messages[-1]["role"] == "system":
            # The last request failed before Claude replied to this note. A system message
            # can't be followed by a user message, so send it again after the new one.
            self.messages.pop()
            self._conversation_settings = None
        self.messages.append({"role": "user", "content": user_text})
        if settings_now != self._conversation_settings:
            # Append instead of editing the system prompt, so history stays byte-identical.
            self.messages.append({"role": "system", "content": "The user updated their settings.\n" + settings_now})
            self._conversation_settings = settings_now

        await self.emit({"type": "status", "state": "working"})
        try:
            await self._loop()
        except asyncio.CancelledError:
            self._close_open_tool_calls("Stopped by the user before this finished.")
            await self.emit({"type": "notice", "text": "Stopped."})
        except anthropic.APIError as exc:
            self._close_open_tool_calls("Interrupted by an error.")
            await self.emit({"type": "error", "text": _describe_api_error(exc, self.config.model)})
        except Exception as exc:  # noqa: BLE001 - surface anything unexpected in the UI
            self._close_open_tool_calls("Interrupted by an error.")
            if isinstance(exc, TypeError) and "authentication" in str(exc).lower():
                message = NO_API_KEY  # the SDK found no API key at all
            else:
                log.exception("agent run failed")
                message = f"Something went wrong: {exc}"
            await self.emit({"type": "error", "text": message})
        finally:
            self.cancel_pending()
            await self.emit({"type": "status", "state": "idle"})

    # ------------------------------------------------------------------ the loop

    async def _loop(self) -> None:
        for _ in range(self.config.max_steps):
            response = await self._call_model()
            self.usage.add(response.usage)
            await self._emit_usage()

            if response.stop_reason == "refusal":
                # Don't keep the partial turn: it may hold a half-written tool call.
                category = getattr(getattr(response, "stop_details", None), "category", None)
                why = f" (category: {category})" if category else ""
                await self.emit({"type": "error", "text": f"Claude declined to continue this request{why}."})
                return
            if response.stop_reason == "max_tokens":
                await self.emit({"type": "error", "text": "Claude's reply was cut off (too long). Try a simpler request."})
                return

            self.messages.append({"role": "assistant", "content": response.content})
            await self._emit_blocks(response.content)

            if response.stop_reason == "pause_turn":
                continue
            tool_calls = [block for block in response.content if block.type == "tool_use"]
            if not tool_calls:
                return
            results = []
            for call in tool_calls:  # one browser: run them in order
                results.append(await self._run_tool(call))
            self.messages.append({"role": "user", "content": results})

        await self.emit({
            "type": "notice",
            "text": f"Paused after {self.config.max_steps} steps. Send a message to let it continue.",
        })

    async def _call_model(self):
        last_error: Exception | None = None
        for _attempt in range(3):
            try:
                async with self.client.beta.messages.stream(
                    model=self.config.model,
                    max_tokens=MAX_TOKENS,
                    system=[{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}],
                    tools=TOOLS,
                    messages=self.messages,
                    thinking={"type": "adaptive", "display": "updates"},
                    output_config={"effort": self.config.effort},
                    cache_control={"type": "ephemeral"},  # also cache the growing conversation
                    context_management=CONTEXT_MANAGEMENT,
                    fallbacks="default",
                    betas=BETAS,
                ) as stream:
                    return await stream.get_final_message()
            except ValueError as exc:
                # A streamed tool input that isn't parseable JSON; ask again.
                log.warning("unparseable streamed tool input, retrying: %s", exc)
                last_error = exc
        raise RuntimeError(f"Claude's tool call could not be read ({last_error})")

    async def _emit_blocks(self, content: list) -> None:
        for block in content:
            if block.type == "thinking" and (block.thinking or "").strip():
                await self.emit({"type": "progress", "text": block.thinking.strip()})
            elif block.type == "text" and block.text.strip():
                await self.emit({"type": "assistant", "text": block.text.strip()})
            elif block.type == "fallback":
                await self.emit({"type": "notice", "text": "Switched to a fallback model for this step."})

    async def _emit_usage(self) -> None:
        cost = self.usage.cost(self.config.model)
        await self.emit({
            "type": "usage",
            "input_tokens": self.usage.input_tokens + self.usage.cache_read_tokens + self.usage.cache_write_tokens,
            "output_tokens": self.usage.output_tokens,
            "cost_usd": round(cost, 4) if cost is not None else None,
        })

    def _close_open_tool_calls(self, reason: str) -> None:
        """If we stopped mid-step, answer the unanswered tool calls so the history stays valid."""
        if not self.messages or self.messages[-1]["role"] != "assistant":
            return
        calls = [b for b in self.messages[-1]["content"] if getattr(b, "type", None) == "tool_use"]
        if calls:
            self.messages.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call.id, "content": reason, "is_error": True}
                    for call in calls
                ],
            })

    # ------------------------------------------------------------------ tools

    async def _run_tool(self, call) -> dict:
        action_id = uuid.uuid4().hex[:10]
        try:
            args = validate_tool_input(call.name, call.input)
        except ToolInputError as exc:
            payload = json.dumps({"INVALID_INPUT": str(exc), "received": call.input}, default=str)
            return {"type": "tool_result", "tool_use_id": call.id, "content": payload, "is_error": True}

        handler = getattr(self, f"_tool_{call.name}")
        label = await self._element_label(args["ref"]) if "ref" in args else ""
        await self.emit({
            "type": "action", "id": action_id, "tool": call.name, "text": _describe_action(call.name, args, label),
        })
        try:
            content = await handler(args)
        except (BrowserError, ToolInputError) as exc:
            await self.emit({"type": "action_done", "id": action_id, "ok": False, "error": str(exc)})
            return {"type": "tool_result", "tool_use_id": call.id, "content": f"Error: {exc}", "is_error": True}
        except _Blocked as exc:
            await self.emit({"type": "action_done", "id": action_id, "ok": False, "error": exc.short})
            return {"type": "tool_result", "tool_use_id": call.id, "content": str(exc), "is_error": True}
        except Exception as exc:  # noqa: BLE001 - let Claude see it and try something else
            log.exception("tool %s failed", call.name)
            await self.emit({"type": "action_done", "id": action_id, "ok": False, "error": str(exc)[:200]})
            return {"type": "tool_result", "tool_use_id": call.id, "content": f"Error: {exc}", "is_error": True}
        await self.emit({"type": "action_done", "id": action_id, "ok": True})
        return {"type": "tool_result", "tool_use_id": call.id, "content": content}

    async def _element_label(self, ref: str) -> str:
        """A short human name for an element, for the step list in the UI."""
        try:
            info = await self.browser.element_info(ref)
        except BrowserError:
            return ""
        for key in ("text", "ariaLabel", "labelText", "placeholder", "title", "alt", "value"):
            value = " ".join(str(info.get(key) or "").split())
            if value and not (key == "value" and safety.sensitive_field_reason(info)):
                return value
        return ""

    async def _page(self, prefix: str = "") -> str:
        snapshot = await self.browser.snapshot(max_chars=SNAPSHOT_CHARS)
        return f"{prefix}\n\n{snapshot}" if prefix else snapshot

    async def _tool_navigate(self, args: dict) -> str:
        problem = check_url_allowed(args["url"])
        if problem:
            raise ToolInputError(problem)
        await self.browser.navigate(args["url"])
        return await self._page()

    async def _tool_read_page(self, _args: dict) -> str:
        return await self._page()

    async def _tool_click(self, args: dict) -> str:
        ref = args["ref"]
        info = await self.browser.element_info(ref)
        reason = safety.click_purchase_reason(info)
        if reason:
            approval = await self._require_approval(reason)
            await self.browser.click(ref)
            self._approval = None  # one approval, one order
            if self._approval_order:
                self.memory.record_order(total=approval.total, **self._approval_order)
                self._approval_order = None
            await self.emit({"type": "notice", "text": f"Clicked the order button you approved ({approval.total:.2f})."})
        else:
            await self.browser.click(ref)
        return await self._page(f"Clicked [{ref}].")

    async def _tool_type_text(self, args: dict) -> str:
        ref = args["ref"]
        info = await self.browser.element_info(ref)
        sensitive = safety.sensitive_field_reason(info)
        if sensitive:
            raise _Blocked(
                f"Blocked: [{ref}] is a {sensitive}. You must not type passwords, card details, security "
                "codes or one-time codes. Call hand_over_to_user so the user can fill it in.",
                short="Sensitive field - left for you to fill in",
            )
        if args["press_enter"]:
            submits = safety.enter_submits_purchase(info)
            if submits:
                raise _Blocked(
                    f"Blocked: pressing Enter in [{ref}] would submit the order form ('{submits}'). Type "
                    "without press_enter, then click the specific button you need (e.g. 'Apply').",
                    short="Enter would submit the order - blocked",
                )
        await self.browser.type_text(ref, args["text"], args["press_enter"])
        return await self._page(f"Typed into [{ref}].")

    async def _tool_select_option(self, args: dict) -> str:
        chosen = await self.browser.select_option(args["ref"], args["option"])
        return await self._page(f"Selected {chosen!r} in [{args['ref']}].")

    async def _tool_press_key(self, args: dict) -> str:
        key = args["key"]
        if key in {"Enter", "Space"}:
            focused = await self.browser.focused_element_info()
            if focused:
                reason = safety.purchase_reason(focused) or (key == "Enter" and safety.enter_submits_purchase(focused))
                if reason:
                    raise _Blocked(
                        f"Blocked: pressing {key} here would activate '{reason}'. Use click on the specific "
                        "button instead (order buttons need request_purchase_approval first).",
                        short="Key press would place the order - blocked",
                    )
        await self.browser.press_key(" " if key == "Space" else key)
        return await self._page(f"Pressed {key}.")

    async def _tool_scroll(self, args: dict) -> str:
        await self.browser.scroll(args["direction"])
        return await self._page()

    async def _tool_go_back(self, _args: dict) -> str:
        await self.browser.go_back()
        return await self._page()

    async def _tool_screenshot(self, _args: dict) -> list:
        image = await self.browser.screenshot()
        url = await self.browser.current_url()
        return [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(image).decode("ascii")}},
            {"type": "text", "text": f"Screenshot of {url}"},
        ]

    async def _tool_ask_user(self, args: dict) -> str:
        answer = await self._wait_for_user("question", {"question": args["question"]})
        text = (answer.get("text") or "").strip()
        return f"The user answered: {text}" if text else "The user didn't answer; use your best judgment."

    async def _tool_hand_over_to_user(self, args: dict) -> str:
        done = await self._wait_for_user("handover", {"reason": args["reason"]})
        note = (done.get("text") or "").strip()
        prefix = "The user says they're done" + (f": {note}" if note else ".")
        return await self._page(prefix)

    async def _tool_request_purchase_approval(self, args: dict) -> str:
        total = float(args["total"])
        currency = args["currency"].strip().upper()[:8]
        limit = self.settings.max_order_total
        url = await self.browser.current_url()
        title = await self.browser.current_title()
        if total > limit:
            await self.emit({
                "type": "notice",
                "text": f"Declined automatically: {total:.2f} {currency} is over your limit of "
                        f"{limit:.2f} {self.settings.currency}.",
            })
            return (
                f"DECLINED automatically: the total {total:.2f} {currency} is over the user's limit of "
                f"{limit:.2f} {self.settings.currency}. Do not place this order. Tell the user and ask "
                "how to proceed (a cheaper option, or they can raise the limit in settings)."
            )
        try:
            shot = base64.standard_b64encode(await self.browser.screenshot()).decode("ascii")
        except Exception:  # noqa: BLE001 - the screenshot is a nice-to-have
            shot = None
        decision = await self._wait_for_user("approval", {
            "store": args["store"],
            "items": args["items"],
            "total": total,
            "currency": currency,
            "details": args["details"],
            "url": url,
            "page_title": title,
            "screenshot": shot,
            "limit": limit,
            "limit_currency": self.settings.currency,
            "currency_mismatch": currency != self.settings.currency.upper(),
        })
        comment = (decision.get("text") or "").strip()
        comment_line = f"\nThe user added: {comment}" if comment else ""
        if decision.get("approved") is True:
            self._approval = safety.PurchaseApproval(
                site=safety.site_of(url), total=total, summary=args["items"], granted_at=time.time()
            )
            self._approval_order = {"store": args["store"], "items": args["items"], "currency": currency}
            return (
                "APPROVED by the user. You may now click the button that places this order (one click, "
                "on this site, within 15 minutes). Then check the confirmation page and report the "
                f"order number and delivery date.{comment_line}"
            )
        self._approval = None
        self._approval_order = None
        return f"The user DECLINED this purchase. Do not place the order.{comment_line}"

    async def _tool_remember(self, args: dict) -> str:
        try:
            fact = self.memory.remember(args["text"])
        except ValueError as exc:
            raise ToolInputError(str(exc)) from exc
        await self.emit({"type": "notice", "text": f"Remembered: {fact['text']}"})
        return f"Saved to memory as [{fact['id']}]."

    async def _tool_forget(self, args: dict) -> str:
        fact = self.memory.forget(args["id"])
        if fact is None:
            raise ToolInputError(f"there is no memory with id {args['id']!r}")
        await self.emit({"type": "notice", "text": f"Forgot: {fact['text']}"})
        return f"Deleted [{fact['id']}] from memory."

    async def _require_approval(self, reason: str) -> safety.PurchaseApproval:
        url = await self.browser.current_url()
        approval = self._approval
        if approval is None or not approval.valid_for(url):
            raise _Blocked(
                f"Blocked: this button ('{reason}') would place an order or make a payment. First check the "
                "order review page, then call request_purchase_approval with the exact items and total. "
                "If this button isn't meant to buy anything, find another way (e.g. a different link).",
                short="Purchase button - needs your approval first",
            )
        return approval

    async def _wait_for_user(self, kind: str, payload: dict) -> dict:
        interaction_id = uuid.uuid4().hex[:12]
        event = {"type": kind, "id": interaction_id, **payload}
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[interaction_id] = _Pending(kind, future, event)
        await self.emit(event)
        await self.emit({"type": "status", "state": "waiting"})
        try:
            return await future
        finally:
            self._pending.pop(interaction_id, None)
            await self.emit({"type": "resolved", "id": interaction_id})
            await self.emit({"type": "status", "state": "working"})


class _Blocked(Exception):
    """A safety rule stopped the action. The message is for Claude, `short` is for the UI."""

    def __init__(self, message: str, short: str) -> None:
        super().__init__(message)
        self.short = short


def _describe_action(name: str, args: dict, label: str = "") -> str:
    def clip(text: str, n: int = 60) -> str:
        text = " ".join(str(text).split())
        return text if len(text) <= n else text[: n - 1] + "…"

    target = f"“{clip(label)}”" if label else f"[{args.get('ref')}]"
    match name:
        case "navigate":
            return f"Opening {clip(args['url'], 80)}"
        case "read_page":
            return "Reading the page"
        case "click":
            return f"Clicking {target}"
        case "type_text":
            where = f" into {target}" if label else ""
            return f"Typing “{clip(args['text'])}”{where}" + (" + Enter" if args["press_enter"] else "")
        case "select_option":
            return f"Choosing “{clip(args['option'])}” in {target}"
        case "press_key":
            return f"Pressing {args['key']}"
        case "scroll":
            return f"Scrolling {args['direction']}"
        case "go_back":
            return "Going back"
        case "screenshot":
            return "Taking a screenshot"
        case "ask_user":
            return "Asking you a question"
        case "hand_over_to_user":
            return "Handing the browser over to you"
        case "remember":
            return f"Remembering “{clip(args['text'])}”"
        case "forget":
            return f"Forgetting memory {args['id']}"
        case "request_purchase_approval":
            return f"Asking for your approval ({args['total']} {args['currency']})"
    return name


NO_API_KEY = ("Your Anthropic API key is missing or invalid. Put ANTHROPIC_API_KEY=... in the .env file "
              "in the project folder (see .env.example), then restart the agent.")


def _describe_api_error(exc: anthropic.APIError, model: str) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return NO_API_KEY
    if isinstance(exc, anthropic.PermissionDeniedError):
        return f"Your API key isn't allowed to do this: {exc.message}"
    if isinstance(exc, anthropic.NotFoundError):
        return f"Model {model!r} wasn't found. Check SHOP_MODEL in your .env file."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the Anthropic API. Wait a minute, then send a message to continue."
    if isinstance(exc, anthropic.BadRequestError):
        return f"The Anthropic API rejected the request: {exc.message}. If this keeps happening, start a new chat."
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic API error {exc.status_code}: {exc.message}. Send a message to retry."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the Anthropic API. Check your internet connection and try again."
    return f"Anthropic API error: {exc}"
