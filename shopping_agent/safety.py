"""Code-level guard rails that do not depend on the model behaving.

1. Purchase buttons ("Place order", "Buy now", "Pay now", ...) can only be
   clicked after the user approved the order in the web UI.
2. The agent may never type into password / card / CVV / one-time-code fields.
   The user types those into the browser window themselves.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

# Phrases on buttons that commit money or a subscription.
# "Proceed to checkout" / "Add to cart" are deliberately NOT here: they are reversible.
_PURCHASE_PATTERNS = [
    r"place (your |my )?order",
    r"place order and pay",
    r"buy (it )?now",
    r"buy with\b",
    r"complete (your |my )?(purchase|order|checkout|payment)",
    r"confirm (and pay|& pay|purchase|order|payment)",
    r"submit (your |my )?(order|payment)",
    r"(finish|finalize|finalise) (your )?(purchase|order)",
    r"pay now",
    r"^pay\b",
    r"\bpay [$€£¥₹]",
    r"make (a )?payment",
    r"authori[sz]e (the )?payment",
    r"order now",
    r"purchase now",
    r"^purchase\b",
    r"\b(1|one)[- ]?click\b",
    r"start (my |your )?(free )?(trial|subscription|membership)",
    r"subscribe now",
    r"\b(apple|google|shop|amazon) ?pay\b",
    r"check ?out with (paypal|apple pay|google pay|shop pay)",
]
_PURCHASE_RE = re.compile("|".join(f"(?:{p})" for p in _PURCHASE_PATTERNS), re.IGNORECASE)

_SENSITIVE_AUTOCOMPLETE = re.compile(
    r"^(cc-|current-password|new-password|one-time-code)", re.IGNORECASE
)
_SENSITIVE_WORDS = re.compile(
    r"card ?number|cardnumber|card ?no\b|credit ?card|debit ?card|\bccnum|\bcc-?number|"
    r"\bcvv|\bcvc|\bcsc\b|security ?code|verification ?code|card ?verification|"
    r"expir|\bexp ?date|\bexp ?month|\bexp ?year|"
    r"\biban\b|routing ?number|account ?number|sort ?code|\bbsb\b|"
    r"\bssn\b|social ?security|passcode|password|one[- ]?time|\botp\b|2fa|two[- ]?factor",
    re.IGNORECASE,
)


def _humanize(text: str) -> str:
    """'placeYourOrder1' -> 'place Your Order1', 'buy_now-btn' -> 'buy now btn'."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    return re.sub(r"[_\-]+", " ", text)


def _label_text(info: dict) -> str:
    parts = [
        info.get("text", ""),
        info.get("ariaLabel", ""),
        info.get("title", ""),
        info.get("alt", ""),
    ]
    if info.get("tag") == "input" and info.get("type") in {"submit", "button", "image"}:
        parts.append(info.get("value", ""))
    return " ".join(p.strip() for p in parts if p).strip()


def purchase_reason(info: dict) -> str | None:
    """If clicking this element would likely spend money, return the matched phrase."""
    candidates = [_label_text(info), _humanize(info.get("id", "")), _humanize(info.get("name", ""))]
    for candidate in candidates:
        # Also match each line separately so '^pay' anchors work for multi-line labels.
        for line in [candidate, *candidate.splitlines()]:
            line = " ".join(line.split())
            match = _PURCHASE_RE.search(line) if line else None
            if match:
                return match.group(0)
    return None


def click_purchase_reason(info: dict) -> str | None:
    """Check the clicked element, the button/link around it, and a label's control."""
    for candidate in (info, info.get("host"), info.get("control")):
        if candidate:
            reason = purchase_reason(candidate)
            if reason:
                return reason
    return None


def enter_submits_purchase(info: dict) -> str | None:
    """Pressing Enter in a field submits its form - is that form's submit button a purchase?"""
    for label in info.get("formSubmitLabels", []) or []:
        reason = purchase_reason({"text": label})
        if reason:
            return reason
    return None


def sensitive_field_reason(info: dict) -> str | None:
    """If this is a field only the human should type into, say why."""
    if (info.get("type") or "").lower() == "password":
        return "password field"
    autocomplete = (info.get("autocomplete") or "").strip()
    for token in autocomplete.split():
        if _SENSITIVE_AUTOCOMPLETE.match(token):
            return f"autocomplete={token}"
    haystack = " ".join(
        _humanize(info.get(key, "") or "")
        for key in ("name", "id", "placeholder", "ariaLabel", "labelText")
    )
    match = _SENSITIVE_WORDS.search(haystack)
    if match:
        return f"looks like a '{match.group(0).strip()}' field"
    return None


def site_of(url: str) -> str:
    """Registrable-ish host for binding approvals: 'www.amazon.com' -> 'amazon.com'."""
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    if len(parts) > 2 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"}:
        return ".".join(parts[-3:])  # amazon.co.uk
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


@dataclass
class PurchaseApproval:
    """A one-time permission to click one purchase button on one site."""

    site: str
    total: float
    summary: str
    granted_at: float
    ttl_seconds: float = 15 * 60

    def valid_for(self, url: str) -> bool:
        return site_of(url) == self.site and (time.time() - self.granted_at) < self.ttl_seconds
