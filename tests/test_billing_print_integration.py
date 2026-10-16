"""Payment management hosts subscriptions and scoped printing wallets."""
from html.parser import HTMLParser
from pathlib import Path

import pytest
from flask import Flask

from bit import account_billing, print_wallet

ROOT = Path(__file__).resolve().parents[1]


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.attributes = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "id" in attributes:
            self.ids.append(attributes["id"])
            self.attributes[attributes["id"]] = attributes
        if tag == "body":
            self.attributes["body"] = attributes


def application(user):
    app = Flask(__name__, template_folder=str(ROOT / "bit/templates"), static_folder=str(ROOT / "bit/static"))
    app.config.update(TESTING=True)
    calls = []

    def storage(payload):
        calls.append(payload)
        return {}

    for module in (account_billing, print_wallet):
        app.register_blueprint(module.create_blueprint(
            login_required=lambda function: function, current_user=lambda: user, storage=storage,
        ))
    return app, calls


@pytest.mark.parametrize("role,billing_admin,wallet_admin", [
    ("super_admin", "true", "true"), ("enterprise_admin", "false", "true"),
    ("warehouse", "false", "false"), ("member", "false", "false"),
])
def test_payment_page_integrates_printing_without_expanding_subscription_permissions(role, billing_admin, wallet_admin):
    app, calls = application({"id": 7, "role_key": role})
    client = app.test_client()
    response = client.get("/billing?section=print-wallet")
    assert response.status_code == 200
    markup = response.get_data(as_text=True)
    assert "支付管理" in markup and "业务员打印余额" in markup
    elements = Elements()
    elements.feed(markup)
    assert len(elements.ids) == len(set(elements.ids)), "The two account modules must have distinct DOM IDs"
    assert elements.attributes["body"]["data-admin"] == billing_admin
    assert elements.attributes["print-wallet"]["data-wallet-admin"] == wallet_admin
    assert "print-wallet.js" in markup and "account-billing.js" in markup
    assert not calls, "Page rendering must not modify either account"
    assert client.get("/api/billing/accounts").status_code == (200 if role == "super_admin" else 403)


def test_old_wallet_entry_redirects_to_payment_management():
    app, calls = application({"id": 7, "role_key": "warehouse"})
    response = app.test_client().get("/print-wallet")
    assert response.status_code == 302
    assert response.location == "/billing?section=print-wallet"
    assert not calls


def test_console_has_one_payment_entry_and_order_wallet_link_targets_integrated_section():
    markup = (ROOT / "bit/templates/index.html").read_text()
    assert 'href="/print-wallet"' not in markup
    assert 'href="/billing" target="_blank" rel="noopener" data-icon="¥"><span class="nav-label">支付管理</span>' in markup
    assert 'href="/billing?section=print-wallet"' in markup
