"""Settings, read from environment variables (or a .env file next to the project)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = PROJECT_DIR / ".env") -> None:
    """Minimal .env loader: KEY=value lines, # comments. Real env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in {'"', "'"}:
            value = value[1:].split(value[0], 1)[0]
        else:
            value = value.split(" #", 1)[0].strip()
        if key and key not in os.environ:
            os.environ[key] = value


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = PROJECT_DIR / "data"
    headless: bool = False
    browser_channel: str | None = None
    browser_executable: str | None = None
    max_steps: int = 150
    open_ui: bool = True

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        return cls(
            model=os.environ.get("SHOP_MODEL", cls.model),
            effort=os.environ.get("SHOP_EFFORT", cls.effort),
            host=os.environ.get("SHOP_HOST", cls.host),
            port=int(os.environ.get("SHOP_PORT", cls.port)),
            data_dir=Path(os.environ.get("SHOP_DATA_DIR", str(cls.data_dir))).expanduser(),
            headless=_env_bool("SHOP_HEADLESS", cls.headless),
            browser_channel=os.environ.get("SHOP_BROWSER_CHANNEL") or None,
            browser_executable=os.environ.get("SHOP_BROWSER_EXECUTABLE") or None,
            max_steps=int(os.environ.get("SHOP_MAX_STEPS", cls.max_steps)),
            open_ui=_env_bool("SHOP_OPEN_UI", cls.open_ui),
        )


@dataclass
class UserSettings:
    """Things the user edits from the web UI. Saved to data/settings.json."""

    max_order_total: float = 100.0
    currency: str = "USD"
    notes: str = ""

    @classmethod
    def load(cls, path: Path) -> "UserSettings":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls(
            max_order_total=float(raw.get("max_order_total", cls.max_order_total)),
            currency=str(raw.get("currency", cls.currency))[:8] or cls.currency,
            notes=str(raw.get("notes", ""))[:4000],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    def to_dict(self) -> dict:
        return {
            "max_order_total": self.max_order_total,
            "currency": self.currency,
            "notes": self.notes,
        }
