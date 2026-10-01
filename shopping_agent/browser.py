"""A real Chromium window (Playwright) that the agent drives and the user can watch.

The page is shown to Claude as a text "snapshot": readable page text, with every
clickable / typeable element tagged as [ref]<tag>label. Tools refer to elements
by that ref.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import (
    BrowserContext,
    Frame,
    Locator,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

log = logging.getLogger(__name__)

# Runs inside the page. Walks the DOM (including open shadow roots) from just above
# the current scroll position downward, and returns text with interactive elements
# tagged. Refs are stored as data-agent-ref attributes so they stay stable across
# snapshots of the same document.
SNAPSHOT_JS = r"""
({prefix, maxChars}) => {
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','CANVAS','IFRAME','FRAME',
                        'OBJECT','EMBED','HEAD','META','LINK','PATH','IMG','PICTURE','VIDEO','AUDIO','MAP']);
  const STRONG_TAGS = new Set(['A','BUTTON','INPUT','SELECT','TEXTAREA','SUMMARY']);
  const ROLES = new Set(['button','link','checkbox','radio','tab','menuitem','menuitemcheckbox',
                         'menuitemradio','option','switch','combobox','textbox','searchbox',
                         'spinbutton','slider','listbox','treeitem']);
  const NESTED = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link]';
  const vh = window.innerHeight || document.documentElement.clientHeight || 800;
  const minBottom = -0.3 * vh;
  if (typeof window.__agentRefCounter !== 'number') window.__agentRefCounter = 0;

  const out = [];
  let size = 0, line = '', truncated = false, visited = 0, refs = 0;
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const cut = (s, n) => s.length > n ? s.slice(0, n - 1) + '…' : s;
  const q = (s) => '"' + cut(clean(s), 80).replace(/"/g, "'") + '"';
  const push = (s) => { out.push(s); size += s.length + 1; if (size > maxChars) truncated = true; };
  const flush = () => { const t = line.trim(); line = ''; if (t) push(t); };
  const addText = (t) => { line += (line && !line.endsWith(' ') ? ' ' : '') + t; if (line.length > 400) flush(); };

  const sensitive = (el) => {
    const t = (el.getAttribute('type') || '').toLowerCase();
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    return t === 'password' || ac.startsWith('cc-') || ac.includes('password') || ac.includes('one-time-code');
  };
  const refOf = (el) => {
    let r = el.getAttribute('data-agent-ref');
    if (!r || !r.startsWith(prefix)) {
      window.__agentRefCounter += 1;
      r = prefix + window.__agentRefCounter;
      el.setAttribute('data-agent-ref', r);
    }
    refs += 1;
    return r;
  };
  const hasBox = (el) => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const labelOf = (el) => {
    let t = clean(el.innerText || el.textContent);
    if (!t) t = clean(el.getAttribute('aria-label'));
    if (!t) {
      const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
      t = clean(ids.map(id => (document.getElementById(id) || {}).innerText || '').join(' '));
    }
    if (!t) t = clean(el.getAttribute('title'));
    if (!t) { const img = el.querySelector && el.querySelector('img[alt]'); if (img) t = clean(img.alt); }
    if (!t && el.value && typeof el.value === 'string') t = clean(el.value);
    return cut(t || '(no label)', 160);
  };
  const controlLabel = (el) => {
    let t = '';
    if (el.labels && el.labels.length) t = clean(Array.from(el.labels).map(l => l.innerText).join(' '));
    if (!t) t = clean(el.getAttribute('aria-label'));
    return cut(t, 100);
  };
  const describe = (el, tag, role) => {
    const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true' ? ' disabled' : '';
    if (tag === 'INPUT') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox' || type === 'radio') {
        return `<input type=${type}${el.checked ? ' checked' : ''}${disabled}>` + controlLabel(el);
      }
      if (['submit','button','reset','image'].includes(type)) return `<input type=${type}${disabled}>` + labelOf(el);
      let s = `<input type=${type}`;
      const lbl = controlLabel(el); if (lbl) s += ` label=${q(lbl)}`;
      const ph = el.getAttribute('placeholder'); if (ph) s += ` placeholder=${q(ph)}`;
      if (el.value) s += ` value=${sensitive(el) ? '"••••"' : q(el.value)}`;
      return s + disabled + '>';
    }
    if (tag === 'TEXTAREA') {
      let s = '<textarea';
      const lbl = controlLabel(el) || clean(el.getAttribute('placeholder')); if (lbl) s += ` label=${q(lbl)}`;
      if (el.value) s += ` value=${q(el.value)}`;
      return s + disabled + '>';
    }
    if (tag === 'SELECT') {
      const opts = Array.from(el.options || []);
      const sel = opts[el.selectedIndex];
      const names = opts.slice(0, 15).map(o => cut(clean(o.text), 40));
      const more = opts.length > 15 ? ` | …(+${opts.length - 15} more)` : '';
      const lbl = controlLabel(el);
      return `<select${lbl ? ' label=' + q(lbl) : ''} selected=${q(sel ? sel.text : '')}${disabled}> options: ` + names.join(' | ') + more;
    }
    if (el.isContentEditable) return `<textbox value=${q(el.innerText)}>`;
    const kind = tag === 'A' ? 'a' : tag === 'BUTTON' ? 'button' : (role || tag.toLowerCase());
    let extra = '';
    const checked = el.getAttribute('aria-checked') || el.getAttribute('aria-selected') || el.getAttribute('aria-pressed');
    if (checked === 'true') extra += ' checked';
    if (el.getAttribute('aria-expanded')) extra += ` expanded=${el.getAttribute('aria-expanded')}`;
    return `<${kind}${extra}${disabled}>` + labelOf(el);
  };

  function walkChildren(node, inPointer) {
    // Open shadow roots replace the light DOM, which renders through <slot>s.
    let kids = node.childNodes;
    if (node.shadowRoot) kids = node.shadowRoot.childNodes;
    else if (node.tagName === 'SLOT') {
      const assigned = node.assignedNodes({flatten: true});
      if (assigned.length) kids = assigned;
    }
    for (const c of kids) { if (truncated) return; walk(c, inPointer); }
  }

  function walk(node, inPointer) {
    if (truncated || ++visited > 25000) { truncated = true; return; }
    if (node.nodeType === Node.TEXT_NODE) { const t = clean(node.textContent); if (t) addText(t); return; }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node, tag = el.tagName.toUpperCase();
    if (SKIP.has(tag) || el.getAttribute('data-agent-ignore') !== null) return;
    if (tag === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'hidden') return;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return;
    const pos = cs.position;
    const r = el.getBoundingClientRect();
    if ((r.width || r.height) && r.bottom < minBottom && pos !== 'fixed' && pos !== 'sticky') return;

    const block = !cs.display.startsWith('inline') && cs.display !== 'contents';
    if (block) flush();

    const role = (el.getAttribute('role') || '').toLowerCase();
    let strong = STRONG_TAGS.has(tag) || ROLES.has(role) || el.hasAttribute('onclick') ||
                 (el.isContentEditable && el.hasAttribute('contenteditable'));
    if (tag === 'A' && !el.hasAttribute('href') && !role && !el.hasAttribute('onclick')) strong = cs.cursor === 'pointer';
    // <label> for a hidden custom checkbox/radio: make the label the clickable thing.
    let labelFor = null;
    if (tag === 'LABEL' && el.control && ['checkbox','radio'].includes(el.control.type)) {
      const cr = el.control.getBoundingClientRect();
      const ccs = getComputedStyle(el.control);
      if (cr.width <= 1 || cr.height <= 1 || ccs.opacity === '0' || ccs.visibility === 'hidden' || ccs.display === 'none') labelFor = el.control;
    }
    const pointer = cs.cursor === 'pointer';
    const weak = !strong && !labelFor && pointer && !inPointer && hasBox(el);

    if ((strong || labelFor || weak) && hasBox(el)) {
      const ref = refOf(el);
      flush();
      if (labelFor) {
        push(`[${ref}]<label for ${labelFor.type}${labelFor.checked ? ' checked' : ''}>` + labelOf(el));
      } else if (weak || (tag !== 'SELECT' && el.querySelector(NESTED))) {
        push(`[${ref}]<${weak ? 'clickable' : describe(el, tag, role).slice(1).split('>')[0]}>`);
        walkChildren(el, true);
        flush();
      } else {
        push(`[${ref}]` + describe(el, tag, role));
      }
      return;
    }
    walkChildren(el, inPointer || pointer);
    if (block) flush();
  }

  try { walk(document.body || document.documentElement, false); } catch (e) { push('[snapshot error: ' + e + ']'); }
  flush();
  const se = document.scrollingElement || document.documentElement;
  return {
    text: out.join('\n'), truncated, refs,
    scrollY: Math.round(window.scrollY), scrollHeight: Math.round(se.scrollHeight), viewport: vh,
  };
}
"""

# Everything the safety checks need to know about an element (and its form).
ELEMENT_INFO_JS = r"""
(el) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const info = (e) => {
    const ids = (e.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    const labelledBy = ids.map(id => (document.getElementById(id) || {}).innerText || '').join(' ');
    return {
      tag: e.tagName.toLowerCase(),
      type: (e.getAttribute('type') || (e.tagName === 'BUTTON' ? 'submit' : '')).toLowerCase(),
      text: clean(e.innerText || e.textContent).slice(0, 300),
      ariaLabel: clean((e.getAttribute('aria-label') || '') + ' ' + labelledBy),
      title: e.getAttribute('title') || '',
      alt: e.getAttribute('alt') || '',
      value: typeof e.value === 'string' ? e.value.slice(0, 200) : '',
      id: e.id || '',
      name: e.getAttribute('name') || '',
      placeholder: e.getAttribute('placeholder') || '',
      autocomplete: e.getAttribute('autocomplete') || '',
      labelText: e.labels ? clean(Array.from(e.labels).map(l => l.innerText).join(' ')) : '',
    };
  };
  const self = info(el);
  const host = el.closest('button, a, [role=button], input[type=submit], input[type=button], input[type=image]');
  const form = el.form || el.closest('form');
  let submits = [];
  if (form) {
    submits = Array.from(form.querySelectorAll('button, input[type=submit], input[type=image]'))
      .filter(b => (b.type || '').toLowerCase() === 'submit' || b.type === 'image')
      .map(b => clean(b.innerText || b.value || b.getAttribute('aria-label') || b.name || b.id))
      .slice(0, 10);
  }
  self.formSubmitLabels = submits;
  self.host = host && host !== el ? info(host) : null;
  if (el.control) self.control = info(el.control);
  return self;
}
"""


class BrowserError(Exception):
    """An action failed in a way the agent should hear about."""


class BrowserSession:
    def __init__(
        self,
        profile_dir: Path,
        *,
        headless: bool = False,
        channel: str | None = None,
        executable_path: str | None = None,
        blocked_origins: set[str] | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        # e.g. this app's own UI: the agent's browser must never be able to open it.
        self.blocked_origins = {o.rstrip("/").lower() for o in (blocked_origins or set())}
        self.headless = headless
        self.channel = channel
        self.executable_path = executable_path
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._frames: dict[str, Frame] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

    @property
    def is_running(self) -> bool:
        return self._context is not None

    async def start(self) -> None:
        async with self._lock:
            if self._context is not None:
                return
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            if self._pw is None:
                self._pw = await async_playwright().start()
            kwargs: dict = {
                "user_data_dir": str(self.profile_dir),
                "headless": self.headless,
            }
            if self.headless:
                kwargs["viewport"] = {"width": 1280, "height": 860}
            else:
                kwargs["no_viewport"] = True  # page follows the real window size
                kwargs["args"] = ["--window-size=1280,900"]
            if self.channel:
                kwargs["channel"] = self.channel
            if self.executable_path:
                kwargs["executable_path"] = self.executable_path
            context = await self._pw.chromium.launch_persistent_context(**kwargs)
            if self.blocked_origins:
                await context.route(self._is_blocked, lambda route: route.abort("blockedbyclient"))
            context.on("page", self._on_new_page)
            context.on("close", self._on_context_closed)
            self._context = context
            self._page = context.pages[0] if context.pages else await context.new_page()
            self._watch_page(self._page)

    async def close(self) -> None:
        async with self._lock:
            if self._context is not None:
                try:
                    await self._context.close()
                except PlaywrightError:
                    pass
            self._context = None
            self._page = None
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None

    def _is_blocked(self, url: str) -> bool:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}".lower() in self.blocked_origins

    def _on_context_closed(self, _context: BrowserContext) -> None:
        # The user closed the browser window; start a fresh one on next use.
        self._context = None
        self._page = None

    def _on_new_page(self, page: Page) -> None:
        # Links that open a new tab: follow them.
        self._page = page
        self._watch_page(page)

    def _watch_page(self, page: Page) -> None:
        page.on("close", self._on_page_closed)

    def _on_page_closed(self, page: Page) -> None:
        if self._page is page and self._context is not None:
            remaining = [p for p in self._context.pages if not p.is_closed()]
            self._page = remaining[-1] if remaining else None

    async def page(self) -> Page:
        if self._context is None:
            await self.start()
        assert self._context is not None
        if self._page is None or self._page.is_closed():
            open_pages = [p for p in self._context.pages if not p.is_closed()]
            self._page = open_pages[-1] if open_pages else await self._context.new_page()
        return self._page

    # ------------------------------------------------------------------ reading

    async def snapshot(self, max_chars: int = 12000) -> str:
        page = await self.page()
        try:
            main = await page.main_frame.evaluate(SNAPSHOT_JS, {"prefix": "", "maxChars": max_chars})
        except PlaywrightError as exc:
            # Page is mid-navigation; give it a moment and retry once.
            await self._settle(page)
            try:
                main = await page.main_frame.evaluate(SNAPSHOT_JS, {"prefix": "", "maxChars": max_chars})
            except PlaywrightError:
                raise BrowserError(f"could not read the page: {_first_line(exc)}") from exc

        self._frames = {}
        frame_sections = await self._snapshot_frames(page)

        tabs = [p for p in page.context.pages if not p.is_closed()]
        tab_info = f"Tab {tabs.index(page) + 1} of {len(tabs)}" if page in tabs else "Tab"
        title = await _safe_title(page)
        scroll_y, height, viewport = main["scrollY"], main["scrollHeight"], main["viewport"]
        lines = [
            f"{tab_info} | URL: {page.url}",
            f"Title: {title}",
        ]
        if height > viewport * 1.2:
            below = max(0, height - scroll_y - viewport)
            where = "at the top" if scroll_y < 10 else f"scrolled down {scroll_y}px"
            more = f"about {below}px more below" if below > 20 else "reached the bottom"
            lines.append(f"Scroll: {where} of a {height}px-tall page ({more}).")
        lines.append("--- page content: [ref]<element> can be used with click / type_text / select_option ---")
        lines.append(main["text"] or "(page has no visible text)")
        if main["truncated"]:
            lines.append("[... more content below was cut off. Use scroll to see more of the page.]")
        lines.extend(frame_sections)
        return "\n".join(lines)

    async def _snapshot_frames(self, page: Page) -> list[str]:
        sections: list[str] = []
        index = 0
        for frame in page.frames[1:]:
            if index >= 5 or frame.is_detached():
                continue
            try:
                element = await frame.frame_element()
                box = await element.bounding_box()
                if not box or box["width"] < 80 or box["height"] < 40:
                    continue
                index += 1
                prefix = f"f{index}-"
                data = await frame.evaluate(SNAPSHOT_JS, {"prefix": prefix, "maxChars": 2500})
            except PlaywrightError:
                continue
            if not data["text"].strip():
                continue
            self._frames[prefix] = frame
            sections.append(f"--- embedded frame {index} ({_short_url(frame.url)}) ---")
            sections.append(data["text"])
        return sections

    async def screenshot(self) -> bytes:
        page = await self.page()
        return await page.screenshot(type="jpeg", quality=70, scale="css", timeout=15000)

    async def current_url(self) -> str:
        return (await self.page()).url

    async def current_title(self) -> str:
        return await _safe_title(await self.page())

    async def element_info(self, ref: str) -> dict:
        locator = await self._locate(ref)
        try:
            return await locator.evaluate(ELEMENT_INFO_JS, timeout=5000)
        except PlaywrightError as exc:
            raise BrowserError(f"element [{ref}] is no longer on the page: {_first_line(exc)}") from exc

    async def focused_element_info(self) -> dict | None:
        page = await self.page()
        try:
            handle = await page.evaluate_handle("() => document.activeElement")
            element = handle.as_element()
            if element is None:
                return None
            return await element.evaluate(ELEMENT_INFO_JS)
        except PlaywrightError:
            return None

    # ------------------------------------------------------------------ acting

    async def navigate(self, url: str) -> None:
        url = url.strip()
        if "://" not in url and not url.startswith("about:"):
            url = "https://" + url
        page = await self.page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except PlaywrightTimeout:
            pass  # slow page; show whatever loaded
        except PlaywrightError as exc:
            raise BrowserError(f"could not open {url}: {_first_line(exc)}") from exc
        await self._settle(page)

    async def click(self, ref: str) -> None:
        locator = await self._locate(ref)
        try:
            await locator.scroll_into_view_if_needed(timeout=5000)
            await locator.click(timeout=8000)
        except PlaywrightTimeout as exc:
            raise BrowserError(
                f"could not click [{ref}] - it may be covered by a popup or not clickable. "
                f"Close any popup/cookie banner first, or take a screenshot. ({_first_line(exc)})"
            ) from exc
        except PlaywrightError as exc:
            raise BrowserError(f"could not click [{ref}]: {_first_line(exc)}") from exc
        await self._settle(await self.page())  # may be a new tab the click opened

    async def type_text(self, ref: str, text: str, press_enter: bool) -> None:
        locator = await self._locate(ref)
        try:
            await locator.scroll_into_view_if_needed(timeout=5000)
            editable = await locator.evaluate("e => e.isContentEditable && !('value' in e)")
            if editable:
                await locator.click(timeout=5000)
                await locator.press("ControlOrMeta+a")
                await locator.press_sequentially(text, delay=15)
            else:
                await locator.fill(text, timeout=8000)
            if press_enter:
                await locator.press("Enter")
        except PlaywrightError as exc:
            raise BrowserError(f"could not type into [{ref}]: {_first_line(exc)}") from exc
        await self._settle(await self.page())

    async def select_option(self, ref: str, option: str) -> str:
        locator = await self._locate(ref)
        try:
            tag = await locator.evaluate("e => e.tagName")
        except PlaywrightError as exc:
            raise BrowserError(f"element [{ref}] is gone: {_first_line(exc)}") from exc
        if tag != "SELECT":
            raise BrowserError(f"[{ref}] is not a <select> dropdown. Click it to open it, then click the option.")
        for attempt in ({"label": option}, {"value": option}):
            try:
                chosen = await locator.select_option(**attempt, timeout=5000)
                await self._settle(await self.page())
                return ", ".join(chosen)
            except PlaywrightError:
                continue
        # Fall back to a case-insensitive partial match on the visible text.
        options = await locator.evaluate("e => Array.from(e.options).map(o => o.text.trim())")
        wanted = option.strip().lower()
        for text in options:
            if wanted in text.lower():
                chosen = await locator.select_option(label=text, timeout=5000)
                await self._settle(await self.page())
                return ", ".join(chosen)
        raise BrowserError(f"no option matching {option!r}. Options are: {' | '.join(options[:30])}")

    async def press_key(self, key: str) -> None:
        page = await self.page()
        try:
            await page.keyboard.press(key)
        except PlaywrightError as exc:
            raise BrowserError(f"could not press {key}: {_first_line(exc)}") from exc
        await self._settle(page)

    async def scroll(self, direction: str) -> None:
        page = await self.page()
        sign = -1 if direction == "up" else 1
        try:
            await page.evaluate("(s) => window.scrollBy(0, s * window.innerHeight * 0.85)", sign)
        except PlaywrightError as exc:
            raise BrowserError(f"could not scroll: {_first_line(exc)}") from exc
        await asyncio.sleep(0.4)

    async def go_back(self) -> None:
        page = await self.page()
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=20000)
        except PlaywrightTimeout:
            pass
        except PlaywrightError as exc:
            raise BrowserError(f"could not go back: {_first_line(exc)}") from exc
        await self._settle(page)

    # ------------------------------------------------------------------ helpers

    async def _locate(self, ref: str) -> Locator:
        ref = str(ref).strip().strip("[]")
        if not ref:
            raise BrowserError("empty element ref")
        page = await self.page()
        frame: Frame = page.main_frame
        if ref.startswith("f") and "-" in ref:
            prefix = ref.split("-", 1)[0] + "-"
            frame = self._frames.get(prefix)
            if frame is None or frame.is_detached():
                raise BrowserError(f"[{ref}] is from an old snapshot. Call read_page to get fresh refs.")
        locator = frame.locator(f'[data-agent-ref="{_css_escape(ref)}"]')
        try:
            count = await locator.count()
        except PlaywrightError as exc:
            raise BrowserError(f"page changed while looking for [{ref}]; call read_page again.") from exc
        if count == 0:
            raise BrowserError(f"element [{ref}] is not on the page anymore. Call read_page to get fresh refs.")
        return locator.first

    async def _settle(self, page: Page) -> None:
        """Give clicks/typing a moment to trigger navigation or re-rendering."""
        await asyncio.sleep(0.6)
        for state, timeout in (("domcontentloaded", 15000), ("networkidle", 2500)):
            try:
                await page.wait_for_load_state(state, timeout=timeout)
            except PlaywrightError:
                pass


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return text[0][:300] if text else exc.__class__.__name__


def _short_url(url: str) -> str:
    return url if len(url) <= 80 else url[:77] + "..."


def _css_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def _safe_title(page: Page) -> str:
    try:
        return await page.title()
    except PlaywrightError:
        return ""
