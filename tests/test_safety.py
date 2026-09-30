import time

import pytest

from shopping_agent import safety
from shopping_agent.agent import ToolInputError, check_url_allowed, validate_tool_input


@pytest.mark.parametrize("info", [
    {"text": "Place your order"},
    {"text": "Place order"},
    {"text": "Buy Now"},
    {"text": "Pay now"},
    {"text": "Pay $42.10"},
    {"text": "Complete purchase"},
    {"text": "Confirm and pay"},
    {"text": "Submit order"},
    {"text": "Start your free trial"},
    {"text": "Buy with Google Pay"},
    {"ariaLabel": "Buy now with 1-Click"},
    {"tag": "input", "type": "submit", "value": "Place your order"},
    {"tag": "input", "type": "submit", "name": "placeYourOrder1"},
    {"text": "Order total: $10\nPay"},
])
def test_purchase_buttons_are_detected(info):
    assert safety.purchase_reason(info)


@pytest.mark.parametrize("info", [
    {"text": "Add to cart"},
    {"text": "Add to Basket"},
    {"text": "Proceed to checkout"},
    {"text": "Continue"},
    {"text": "Payment options"},
    {"text": "Subscribe & Save"},
    {"text": "Buy again"},
    {"text": "Save for later"},
    {"text": "Apply"},
    {"tag": "input", "type": "text", "value": "pay now"},  # a text field's value is not a label
])
def test_ordinary_buttons_are_not_purchases(info):
    assert safety.purchase_reason(info) is None


def test_host_and_label_control_are_checked_on_click():
    assert safety.click_purchase_reason({"text": "", "host": {"text": "Place your order"}})
    assert safety.click_purchase_reason({"text": "Add to cart"}) is None


@pytest.mark.parametrize("info", [
    {"type": "password"},
    {"autocomplete": "cc-number"},
    {"autocomplete": "billing cc-csc"},
    {"autocomplete": "one-time-code"},
    {"name": "cardNumber"},
    {"name": "cvv"},
    {"placeholder": "Security code"},
    {"labelText": "Expiration date (MM/YY)"},
    {"ariaLabel": "Enter the verification code"},
])
def test_sensitive_fields(info):
    assert safety.sensitive_field_reason(info)


@pytest.mark.parametrize("info", [
    {"name": "q", "placeholder": "Search Amazon"},
    {"name": "promo", "placeholder": "Promo code"},
    {"name": "zip", "labelText": "ZIP code"},
    {"labelText": "PIN code"},  # Indian postal code
    {"name": "fullName", "autocomplete": "name"},
    {"autocomplete": "shipping street-address"},
])
def test_normal_fields_are_not_sensitive(info):
    assert safety.sensitive_field_reason(info) is None


def test_enter_in_order_form_is_detected():
    assert safety.enter_submits_purchase({"formSubmitLabels": ["Apply", "Place your order"]})
    assert safety.enter_submits_purchase({"formSubmitLabels": ["Search"]}) is None


def test_approval_is_bound_to_site_and_time():
    approval = safety.PurchaseApproval(site="amazon.com", total=10, summary="x", granted_at=time.time())
    assert approval.valid_for("https://www.amazon.com/checkout/place-order")
    assert not approval.valid_for("https://www.ebay.com/checkout")
    expired = safety.PurchaseApproval(site="amazon.com", total=10, summary="x", granted_at=time.time() - 3600)
    assert not expired.valid_for("https://www.amazon.com/")
    assert safety.site_of("https://www.amazon.co.uk/x") == "amazon.co.uk"


@pytest.mark.parametrize("url", ["https://www.amazon.com", "target.com/s?searchTerm=soap", "http://93.184.216.34/"])
def test_public_urls_are_allowed(url):
    assert check_url_allowed(url) is None


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "http://localhost:8000/",
    "http://127.0.0.1:8000/?token=x",
    "http://192.168.1.1/",
    "http://10.0.0.5/admin",
    "http://printer.local/",
    "http://[::1]:8000/",
    "chrome://settings",
])
def test_local_and_non_web_urls_are_blocked(url):
    assert check_url_allowed(url)


def test_tool_input_validation():
    assert validate_tool_input("click", {"ref": 12}) == {"ref": "12"}
    assert validate_tool_input("type_text", {"ref": "3", "text": "soap", "press_enter": True})["press_enter"] is True
    with pytest.raises(ToolInputError):
        validate_tool_input("type_text", {"ref": "3", "text": "soap"})  # missing press_enter
    with pytest.raises(ToolInputError):
        validate_tool_input("scroll", {"direction": "sideways"})
    with pytest.raises(ToolInputError):
        validate_tool_input("request_purchase_approval", {
            "store": "x", "items": "y", "total": "12", "currency": "USD", "details": "z"})
    with pytest.raises(ToolInputError):
        validate_tool_input("click", {"ref": "1", "force": True})
    with pytest.raises(ToolInputError):
        validate_tool_input("no_such_tool", {})
