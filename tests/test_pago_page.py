import pytest

from bit.pago_page import parse_pago_home
from bit import bit_pago_info as pago


def home(fraction='1,276', cents='73', pending='42.18', currency='US$', hidden=False):
    return f'''<a href="/banking/balance#from-section=menu">Your money</a>
    <a href="/banking/balance#from-section=home"><span>Money to be transferred</span>
      <div class="banking-balance__amount"><span data-andes-money-amount="true">
        <span data-andes-money-amount-currency="true">{currency}</span>
        <span data-andes-money-amount-fraction="true">{fraction}</span>
        <span data-andes-money-amount-cents="true">{cents}</span>
      </span><button><svg aria-label="{'Show' if hidden else 'Hide'} balance"></svg></button></div>
      <span>US$ {pending} to be released</span></a>
      <a href="/activities/detail/test"><span>US$ 999,888.99</span>Cash withdrawal</a>'''


def test_balance_card_excludes_activity_and_keeps_cents():
    result = parse_pago_home(home())
    assert result['released_usd'] == '1276.73'
    assert result['unreleased_usd'] == '42.18'
    assert '999,888' not in result['raw_text']


def test_zero_balance_is_not_missing():
    result = parse_pago_home(home('0', '00', '0.00'))
    assert result['released_usd'] == result['unreleased_usd'] == '0.00'
    assert result['candidate_count'] == 2


@pytest.mark.parametrize('html', [
    '<a href="/activities/detail/test">US$ 999.99</a>',
    home(hidden=True),
])
def test_missing_or_hidden_card_never_falls_back_to_activity(html):
    result = parse_pago_home(html)
    assert result['released_usd'] == result['unreleased_usd'] == ''
    assert result['raw_text']


def test_non_usd_amount_is_not_saved_as_dollars():
    assert parse_pago_home(home(currency='R$'))['released_usd'] == ''


def test_selected_fulfillment_site_overrides_country_short_code():
    assert not pago._state_matches_site({'selectedRemote': 'MLM-fulfillment', 'currentShort': 'MX', 'operatingSiteId': 'MLM'}, '墨西哥')
    assert pago._state_matches_site({'selectedRemote': 'MLM-remote', 'currentShort': 'MX'}, '墨西哥')


def test_site_wait_preserves_rate_limit(monkeypatch):
    monkeypatch.setattr(pago, '_is_pago_rate_limited_page', lambda d: True)
    with pytest.raises(pago.PagoRateLimitError, match='限频'):
        pago._wait_pago_site_options(object())


def test_site_switch_uses_menu_and_confirms_selected_site(monkeypatch):
    monkeypatch.setattr(pago, '_wait_pago_site_options', lambda *a, **kw: ('ready', {
        'selectedRemote': 'MLB-remote', 'available': [{'value': 'MLM-remote'}],
    }))
    monkeypatch.setattr(pago, '_wait_for_pago_batch_resume', lambda *a: None)
    clicked = []
    monkeypatch.setattr(pago, '_open_country_switch', lambda d: {'clicked': True})
    monkeypatch.setattr(pago, '_click_country_option', lambda d, site: clicked.append(site) or {'clicked': True})
    monkeypatch.setattr(pago, '_is_pago_rate_limited_page', lambda d: False)
    monkeypatch.setattr(pago, '_is_not_logged_in', lambda d: False)
    monkeypatch.setattr(pago, '_get_pago_site_state', lambda d: {'selectedRemote': 'MLM-remote'})
    assert pago._select_country(object(), '墨西哥')['ok']
    assert clicked == ['墨西哥']


def test_site_change_before_read_does_not_save_wrong_balance(monkeypatch):
    monkeypatch.setattr(pago, 'bind_mercado_shop_context', lambda *a: None)
    monkeypatch.setattr(pago, '_open_pago_home_with_retry', lambda *a, **k: True)
    monkeypatch.setattr(pago, '_is_not_logged_in', lambda d: False)
    monkeypatch.setattr(pago, '_select_country', lambda *a, **k: {'ok': True})
    monkeypatch.setattr(pago, '_wait_pago_home_ready', lambda *a, **k: True)
    monkeypatch.setattr(pago, '_is_pago_rate_limited_page', lambda d: False)
    monkeypatch.setattr(pago, '_get_pago_site_state', lambda d: {'selectedRemote': 'MLB-remote'})
    monkeypatch.setattr(pago, '_extract_pago_amounts', lambda d: pytest.fail('must not read wrong site'))
    row = pago.get_pago_info('window', 'shop', '墨西哥', driver=object())
    assert row[2:5] == ['', '', '站点校验失败']
