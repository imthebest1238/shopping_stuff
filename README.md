# 🛒 Shopping Agent

An AI agent that runs on your own computer and does your online shopping in a real browser window.
You tell it what you want on a chat page (`http://127.0.0.1:8000`). It searches stores, compares
options, fills the cart and goes to checkout, and **it asks you before it buys anything.**

It uses Claude (Anthropic's AI) to decide what to do, and [Playwright](https://playwright.dev) to
control the browser.

## How it keeps you safe

These rules are enforced in code, so they hold even if the AI makes a mistake:

- **Nothing is bought without your approval.** Buttons like "Place order", "Buy now" and "Pay now"
  stay locked until you press **Approve & buy** in the chat. The approval card shows the items, the
  total, delivery details and a screenshot of the real order page. One approval covers one order
  on one site, for 15 minutes.
- **Spending limit.** Orders above your limit (default 100 USD, change it in Settings) are declined
  automatically.
- **It never types passwords, card numbers, security codes (CVV) or one-time codes.** Those fields are
  locked. When a store needs them, the agent hands the browser to you ("Your turn in the browser
  window"), you type them yourself, then click "I'm done".
- **It ignores instructions written on web pages**, and it can't open files on your computer or
  devices on your home network.
- **Only you can use the chat page.** It only works in the browser that opened the secret link
  printed in the terminal, and only on this computer.

Your store logins are kept in the agent's own browser profile in `data/browser-profile` on your
computer. What does leave your computer: the text of the pages the agent visits (and occasional
screenshots) is sent to the Anthropic API so Claude can read them.

## Setup

You need:

- **Python 3.10 or newer** ([download](https://www.python.org/downloads/))
- **An Anthropic API key**: create one at [platform.claude.com](https://platform.claude.com/) (API keys).
  Using the API costs money; see [Cost](#cost).

### The easy way

- **Mac / Linux:** open a terminal in this folder and run `./start.sh`
- **Windows:** double-click `start.bat`

The first run installs everything (a few minutes), then asks you to paste your API key into the
`.env` file. Run it again. Two windows open:

1. **The chat page** in your normal browser, where you talk to the agent.
2. **The agent's browser window**, which the agent controls. You can watch it work.

### Manual setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env               # then put your key after ANTHROPIC_API_KEY=
python -m shopping_agent
```

### Docker

The agent's browser runs on a virtual screen inside the container. You watch and control it from
your own browser with noVNC (for logins, CAPTCHAs, card details and checking orders).

```bash
cp .env.example .env               # then put your key after ANTHROPIC_API_KEY=
docker compose up -d --build
docker compose logs shopping-agent # shows the secret chat link
```

- **Chat page:** the `?token=` link from the logs (`http://127.0.0.1:8000/?token=...`)
- **Agent's browser window:** `http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale`.
  The password is in `data/vnc_password`.

Both are published on `127.0.0.1` only, so other computers can't reach them. `data/` is mounted
into the container, so settings, memory and store logins are kept. `.env` and `data/` are never
copied into the image. Stop it with `docker compose down`.

**Prebuilt image / Portainer:** GitHub Actions (`.github/workflows/docker-image.yml`) runs the tests and
pushes the image to `ghcr.io/<owner>/<repo>` on every push (`:latest` on the default branch, plus a
tag per branch and per commit). Deploy it with `docker-compose.portainer.yml` as a Portainer stack,
setting `ANTHROPIC_API_KEY` and `SHOPPING_AGENT_IMAGE` as stack environment variables. The chat link
and VNC password are in the container logs. Reach it from another computer through an SSH tunnel:
`ssh -L 8000:127.0.0.1:8000 -L 6080:127.0.0.1:6080 user@docker-host`.

## Using it

1. **Log in to your stores once** in the agent's browser window (Amazon, Target, Walmart…). It
   remembers the logins for next time.
2. **Open Settings** on the chat page. Set your spending limit and add notes, for example:
   *"Prefer Amazon, then Target. Shoe size 42 EU. Ship to my home address. Standard shipping is fine."*
3. **Ask for something:**
   - "Find the best-rated 24-pack of AA batteries under $20 on Amazon and buy it"
   - "Compare 3 robot vacuums under $300. Don't buy yet."
   - "Reorder the dish soap from my last Target order"
4. **Watch and help when asked.** The agent may ask a question (size, color…) or hand you the browser
   to log in, solve a CAPTCHA or enter card details. Do that in the agent's window, then click
   **I'm done**.
5. **Approve or decline.** When it reaches the final order page, an approval card appears. Check it
   and the browser window, then click **Approve & buy** or **Decline**.

Press **Stop** at any time. **New chat** starts a fresh conversation (you stay logged in to your stores).

## Cost

The agent uses Claude Opus 5.5 by default ($4 per million input tokens, $20 per million output
tokens; repeated context is cached and billed at a fraction of that). A typical shopping task that
visits 20–40 pages costs very roughly $0.50–$2. The chat page shows a running estimate at the top.

To spend less, set `SHOP_MODEL=claude-sonnet-5-5` or `SHOP_EFFORT=low` in `.env`.

## Settings

Put these in the `.env` file (see `.env.example`):

| Setting | Default | What it does |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | (required) | Your Anthropic API key |
| `SHOP_MODEL` | `claude-opus-5-5` | Claude model. `claude-sonnet-5-5` is cheaper |
| `SHOP_EFFORT` | `medium` | How hard Claude thinks each step: `low`, `medium`, `high` |
| `SHOP_BROWSER_CHANNEL` | (bundled Chromium) | `chrome` or `msedge` to use your installed browser |
| `SHOP_PORT` | `8000` | Port of the chat page |
| `SHOP_MAX_STEPS` | `150` | Claude calls per message before the agent pauses |
| `SHOP_OPEN_UI` | `true` | Open the chat page automatically |
| `SHOP_DATA_DIR` | `./data` | Where settings and the browser profile are kept |
| `SHOP_HEADLESS` | `false` | Hide the agent's browser window (not recommended) |

The spending limit and your notes are set on the chat page (Settings) and saved in `data/settings.json`.

## Good to know

- Some stores detect automated browsers and show CAPTCHAs or block them. The agent hands those over
  to you. Using your installed Chrome (`SHOP_BROWSER_CHANNEL=chrome`) sometimes helps.
- The agent reads pages as text, plus screenshots when needed. Very unusual sites can confuse it.
- You are responsible for what you approve. Always read the approval card, and check each store's
  terms about automated shopping.
- Keep the `data/` folder private: it holds your store logins.

## How it works

```
chat page (your browser) ⇄ WebSocket ⇄ local server ⇄ agent loop ⇄ Claude API
                                                   ⇣
                                  agent's browser window (Playwright)
```

| File | Purpose |
| --- | --- |
| `shopping_agent/agent.py` | The Claude tool-use loop, tool handlers and approval gate |
| `shopping_agent/prompts.py` | System prompt and tool definitions |
| `shopping_agent/browser.py` | Browser control and the page-to-text snapshot (`[ref]<element>` labels) |
| `shopping_agent/safety.py` | Detects order buttons and sensitive fields; approval tokens |
| `shopping_agent/server.py` | FastAPI server, WebSocket, secret-link login |
| `shopping_agent/static/` | The chat page |

Claude API details: Claude Opus 5.5 with adaptive thinking (`display: "updates"` so progress notes
show up in the chat), an explicit effort level, server-side refusal fallbacks (`fallbacks: "default"`),
context editing that clears old page snapshots, and prompt caching. The conversation history is
append-only, and settings changes mid-chat are sent as a mid-conversation system message, so the
cache stays warm and earlier thinking stays valid.

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The browser and end-to-end tests launch a headless Chromium against a local test shop
(`tests/fixtures/shop.html`) and use a scripted stand-in for Claude, so they need no API key. Set
`SHOP_BROWSER_EXECUTABLE` to use a specific Chromium binary.
