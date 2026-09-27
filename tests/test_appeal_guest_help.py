"""Guest Help pages must enter bounded login recovery, not iframe retries."""
import json
from types import SimpleNamespace

import pytest

from bit import bit_mercado_limit as limit, bit_mercado_login as login


def help_state(*, guest=True, user_id="-1", url=None):
    context = {"appProps": {"pageProps": {
        "siteId": "CBT", "isGuest": guest, "userId": user_id,
    }}}
    return {
        "current_url": url or "https://global-selling.mercadolibre.com/help/v2",
        "title": "Help",
        "page_text": "How can we help you? 如果手机丢失或被盗，我该如何访问我的账户？",
        "page_source": '<script id="__NORDIC_RENDERING_CTX__">_n.ctx.r='
        + json.dumps(context) + ';_n.ctx.r.assets={};</script>',
    }


@pytest.mark.parametrize("path", ["/help", "/help/v2", "/help/v2/?source=test"])
def test_guest_help_is_logged_out_even_without_login_form(path):
    state = help_state(url="https://global-selling.mercadolibre.com" + path)
    assert limit.get_mercado_backend_status(state=state) == "logged_out"


@pytest.mark.parametrize("changes", [
    {"guest": False, "user_id": "123"},
    {"guest": True, "user_id": "123"},
    {"guest": "true"},
    {"url": "https://example.org/help"},
    {"url": "https://global-selling.mercadolibre.com/orders"},
])
def test_guest_detection_requires_explicit_help_identity(changes):
    assert not limit.is_mercado_guest_help_state(help_state(**changes))


@pytest.mark.parametrize("source", [
    'Article: "isGuest":true, "userId":"-1"',
    '<script id="translations">_n.ctx.r={"isGuest":true}</script>',
    '<script id="__NORDIC_RENDERING_CTX__">_n.ctx.r={broken</script>',
])
def test_guest_detection_does_not_guess_from_article_or_broken_script(source):
    state = help_state()
    state["page_source"] = source
    assert not limit.is_mercado_guest_help_state(state)


@pytest.mark.parametrize("recovered", [True, False])
def test_guest_help_uses_one_login_attempt_then_reopens_or_stops(monkeypatch, recovered):
    driver = SimpleNamespace(_bit_appeal_login_attempts=0, _bit_appeal_login_max_attempts=1)
    states = iter([help_state(), help_state(guest=not recovered, user_id="123" if recovered else "-1")])
    navigations, attempts = [], []
    monkeypatch.setattr(login, "is_mercado_login_page", lambda _: False)
    monkeypatch.setattr(login, "bind_mercado_shop_context", lambda *a: None)
    monkeypatch.setattr(login, "try_record_login_anomaly", lambda *a, **kw: False)
    result = login.open_mercado_backend_page(
        driver, "https://global-selling.mercadolibre.com/help", "测试店铺",
        settle_seconds=0, navigate=navigations.append,
        state_reader=lambda _: next(states),
        login_handler=lambda *a: attempts.append(1) or {"ok": True},
    )
    assert attempts == [1]
    assert len(navigations) == 2
    assert driver._bit_appeal_login_attempts == 1
    assert result["status"] == ("ready" if recovered else "logged_out")
    assert result["ok"] is recovered
