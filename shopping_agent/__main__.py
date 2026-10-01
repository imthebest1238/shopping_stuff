"""Start the shopping agent: `python -m shopping_agent`."""

from __future__ import annotations

import logging
import os
import sys
import threading
import webbrowser

import uvicorn

from .config import Config
from .server import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = Config.from_env()

    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print(
            "Note: ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key\n"
            "      (get one at https://platform.claude.com/), unless you use `ant auth login`.\n",
            file=sys.stderr,
        )
    if config.host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"Warning: SHOP_HOST={config.host} makes the agent reachable from other computers.\n", file=sys.stderr)

    app = create_app(config)
    host = "127.0.0.1" if config.host in {"0.0.0.0", "::"} else config.host
    url = f"http://{host}:{config.port}/?token={app.state.shop.token}"
    print(
        "\n  Shopping agent is starting.\n"
        f"  Open this link in your normal browser:  {url}\n"
        "  A separate browser window will open for the agent - log in to your stores there.\n"
        "  Press Ctrl+C to quit.\n"
    )
    if config.open_ui:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning")


if __name__ == "__main__":
    main()
