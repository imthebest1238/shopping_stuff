"""Long-term memory: facts the user told the agent, and the orders they approved.

Saved to data/memory.json and shown to Claude at the start of each conversation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

MAX_FACTS = 100
MAX_FACT_CHARS = 300
MAX_ORDERS = 50
ORDERS_IN_PROMPT = 10


@dataclass
class Memory:
    path: Path | None = None  # None: keep in memory only (used by tests)
    facts: list[dict] = field(default_factory=list)  # {"id": "m3", "text": ..., "added": "2026-10-01"}
    orders: list[dict] = field(default_factory=list)  # {"date", "store", "items", "total", "currency"}
    next_id: int = 1

    @classmethod
    def load(cls, path: Path) -> "Memory":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path)
        except (OSError, ValueError):
            log.warning("could not read %s; starting with an empty memory", path)
            return cls(path)
        facts = [f for f in raw.get("facts", []) if isinstance(f, dict) and f.get("id") and f.get("text")]
        orders = [o for o in raw.get("orders", []) if isinstance(o, dict)]
        next_id = max([int(str(f["id"]).lstrip("m") or 0) for f in facts if str(f["id"]).lstrip("m").isdigit()],
                      default=0) + 1
        return cls(path, facts[-MAX_FACTS:], orders[-MAX_ORDERS:], next_id)

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"facts": self.facts, "orders": self.orders}
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def remember(self, text: str) -> dict:
        text = " ".join(text.split())[:MAX_FACT_CHARS]
        if not text:
            raise ValueError("nothing to remember")
        for fact in self.facts:
            if fact["text"].lower() == text.lower():
                return fact
        fact = {"id": f"m{self.next_id}", "text": text, "added": date.today().isoformat()}
        self.next_id += 1
        self.facts = (self.facts + [fact])[-MAX_FACTS:]
        self.save()
        return fact

    def forget(self, fact_id: str) -> dict | None:
        fact_id = fact_id.strip().strip("[]")
        for i, fact in enumerate(self.facts):
            if fact["id"] == fact_id:
                del self.facts[i]
                self.save()
                return fact
        return None

    def record_order(self, store: str, items: str, total: float, currency: str) -> None:
        order = {"date": date.today().isoformat(), "store": store, "items": items, "total": total,
                 "currency": currency}
        self.orders = (self.orders + [order])[-MAX_ORDERS:]
        self.save()

    def prompt_text(self) -> str:
        if self.facts:
            facts = "\n".join(f"[{f['id']}] {f['text']} (saved {f['added']})" for f in self.facts)
        else:
            facts = "(nothing yet)"
        recent = self.orders[-ORDERS_IN_PROMPT:]
        if recent:
            orders = "\n".join(
                f"- {o['date']} {o['store']}, total {float(o['total']):.2f} {o['currency']}:\n  "
                + "\n  ".join(str(o["items"]).splitlines())
                for o in reversed(recent)
            )
        else:
            orders = "(none yet)"
        return (
            "## Your memory from earlier chats\n"
            "Things you saved with the remember tool. They come from the user, so you can use them; "
            "if the user now says something different, follow the user and update the memory.\n"
            f"<memory>\n{facts}\n</memory>\n"
            "Orders the user approved (newest first), useful for reorders and typical quantities:\n"
            f"<past_orders>\n{orders}\n</past_orders>"
        )
