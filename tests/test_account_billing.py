import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from flask import Flask
from bit import account_billing as billing


def test_prices_calendar_and_expiry():
    assert billing.quote('personal', 10) == 29900
    assert billing.quote('personal', 11) == 29900
    assert billing.quote('personal', 10000) == 29900
    assert billing.quote('enterprise', 30) == 59900
    assert billing.quote('enterprise', 31) == 60900
    assert billing.quote('enterprise', 45) == 74900
    assert billing.add_months(datetime(2024, 1, 31, 12), 1) == datetime(2024, 2, 29, 12)
    assert billing.add_months(datetime(2025, 12, 31), 2) == datetime(2026, 2, 28)
    now = datetime(2026, 10, 5, 12)
    account = {'expires_at': now, 'plan': 'personal'}
    assert billing.public_account(account, now)['status'] == 'expired'
    account['expires_at'] = datetime(2026, 10, 5, 12, 0, 1)
    assert billing.public_account(account, now)['days_remaining'] == 1
    account['actual_sites'] = 11
    assert billing.public_account(account, now)['status'] == 'over_limit'
    account['site_count'] = 15
    result = billing.public_account(account, now)
    assert result['capacity'] == 15
    assert result['status'] == 'active'
    account['actual_sites'] = 16
    assert billing.public_account(account, now)['status'] == 'over_limit'


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / 'billing.db'
    raw = sqlite3.connect(path)
    raw.executescript('''
      CREATE TABLE workbench_users (id INTEGER PRIMARY KEY, role_key TEXT, is_active INTEGER,
        username TEXT, display_name TEXT, organization_key TEXT);
      INSERT INTO workbench_users VALUES (1,'super_admin',1,'admin','管理员','alpha'),
        (2,'member',1,'sales','业务员','alpha'), (3,'enterprise_admin',1,'boss','老板','alpha'),
        (4,'member',1,'other','业务员','beta');
      CREATE TABLE mercado_store_tokens (id INTEGER PRIMARY KEY, organization_key TEXT);
      INSERT INTO mercado_store_tokens VALUES (10,'alpha'),(20,'beta');
      CREATE TABLE mercado_store_site_settings (token_id INTEGER, site_id TEXT, salesperson TEXT);
      INSERT INTO mercado_store_site_settings VALUES (10,'MLM','业务员'),(20,'MLM','业务员'),(20,'MLB','业务员');
      CREATE TABLE workbench_subscriptions (user_id INTEGER PRIMARY KEY, plan TEXT, site_count INTEGER,
        expires_at TEXT, revision INTEGER, updated_at TEXT);
      CREATE TABLE workbench_billing_records (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        request_id TEXT, kind TEXT, amount_cents INTEGER, months INTEGER, note TEXT, actor_id INTEGER,
        actor_name TEXT, before_json TEXT, after_json TEXT, created_at TEXT, UNIQUE(user_id,request_id));
    ''')
    raw.close()
    monkeypatch.setattr(billing, 'ensure_tables', lambda cursor: None)
    monkeypatch.setattr(billing, 'now_cn', lambda: datetime(2026, 10, 5, 12))

    class Cursor:
        def __init__(self, raw): self.raw = raw.cursor()
        def __enter__(self): return self
        def __exit__(self, *args): self.raw.close()
        def execute(self, sql, params=()):
            self.raw.execute(sql.replace('%s', '?').replace(' FOR UPDATE', ''), tuple(value.isoformat(' ') if isinstance(value, datetime) else value for value in params))
        def fetchone(self):
            row = self.raw.fetchone()
            return dict(row) if row else None
        def fetchall(self): return [dict(row) for row in self.raw.fetchall()]
    class Connection:
        def __init__(self):
            self.raw = sqlite3.connect(path)
            self.raw.row_factory = sqlite3.Row
        def cursor(self): return Cursor(self.raw)
        def commit(self): self.raw.commit()
        def rollback(self): self.raw.rollback()
        def close(self): self.raw.close()
    def call(action, actor=1, target=2, **data):
        return billing.billing_local(
            {'action':action, 'actor_id':actor, 'user_id':target, 'data':data}, connect=Connection)
    call.db_path = path
    return call


def recharge(store, **changes):
    data = dict(plan='personal', site_count=1, revision=0, months=1, amount='299',
                request_id='payment-001', note='银行转账 123')
    data.update(changes)
    return store('recharge', **data)


def test_recharge_renew_adjust_audit_and_idempotency(store):
    initial = store('get')
    assert initial['status'] == 'unpaid'
    assert initial['site_count'] == initial['capacity'] == 10
    assert initial['actual_sites'] == 1  # Same name in another enterprise is isolated.
    first = recharge(store)
    assert first['expires_at'] == '2026-11-05T12:00:00'
    assert recharge(store) == {'duplicate':True}
    second = recharge(store, request_id='payment-002', revision=1, months=2)
    assert second['expires_at'] == '2027-01-05T12:00:00'
    with pytest.raises(ValueError, match='其他管理员'):
        recharge(store, request_id='payment-003', revision=1)
    adjusted = store('adjust', request_id='adjust-001', revision=2, plan='enterprise',
                     site_count=35, expires_at='2026-10-01T00:00:00', note='人工更正')
    assert adjusted['monthly_cents'] == 64900
    assert store('get')['status'] == 'expired'
    renewed = recharge(store, request_id='payment-004', revision=3)
    assert renewed['expires_at'] == '2026-11-05T12:00:00'
    records = store('history')
    assert len(records) == 4
    assert records[1]['kind'] == 'adjust'
    assert records[1]['amount_cents'] == 0
    assert records[1]['before']['expires_at'] == '2027-01-05T12:00:00'
    assert records[0]['actor_name'] == '管理员'
    assert records[-1]['amount_cents'] == 29900


@pytest.mark.parametrize('amount', ['NaN','Infinity','-1','0','1.001','10000001','oops'])
def test_invalid_money_rolls_back(store, amount):
    with pytest.raises(ValueError): recharge(store, amount=amount)
    assert store('history') == []
    assert store('get')['expires_at'] is None



def test_personal_custom_capacity_controls_actual_usage(store):
    with sqlite3.connect(store.db_path) as connection:
        connection.executemany(
            'INSERT INTO mercado_store_site_settings VALUES (10, ?, ?)',
            [(f'site-{index}', '业务员') for index in range(11)],
        )
    assert store('get')['actual_sites'] == 12
    recharge(store, site_count=15)
    account = store('get')
    assert account['capacity'] == 15
    assert account['status'] == 'active'
    with pytest.raises(ValueError, match='额度不足'):
        store('adjust', request_id='adjust-too-small', revision=1, plan='personal',
              site_count=11, expires_at=account['expires_at'], note='降低额度')
    assert store('get')['site_count'] == 15
    assert len(store('history')) == 1


def test_permissions_and_input_limits(store):
    for action in ('list','adjust','recharge'):
        with pytest.raises(PermissionError): store(action, actor=3)
    with pytest.raises(PermissionError): store('get', actor=2, target=4)
    assert store('get', actor=2)['user_id'] == 2
    with pytest.raises(ValueError): recharge(store, months=0)
    with pytest.raises(ValueError): recharge(store, months=True)
    with pytest.raises(ValueError): recharge(store, plan='personal', site_count=10001)
    with pytest.raises(ValueError): recharge(store, note='')
    assert store('history') == []


def test_blueprint_permission_and_page(store):
    user = {'id':2, 'role_key':'member', 'username':'sales'}
    app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'bit/templates'))
    def storage(payload):
        return store(payload['action'], actor=payload['actor_id'], target=payload['user_id'], **payload['data'])
    app.register_blueprint(billing.create_blueprint(login_required=lambda fn: fn,
        current_user=lambda:user, storage=storage))
    client = app.test_client()
    page = client.get('/billing')
    assert page.status_code == 200
    assert 'account-billing.js?v=20261008-personal-sites' in page.get_data(as_text=True)
    assert client.get('/api/billing/me').json['data']['user_id'] == 2
    assert client.get('/api/billing/accounts').status_code == 403
    assert client.get('/api/billing/accounts/4/records').status_code == 403
    assert client.put('/api/billing/accounts/2', json={}).status_code == 403
    user.update(id=1, role_key='super_admin')
    assert len(client.get('/api/billing/accounts').json['data']) == 3
    assert client.get('/billing').status_code == 200


def test_real_access_gate():
    # Execute the actual gate without importing the unrelated MySQL startup services.
    import ast
    from types import SimpleNamespace
    from flask import g, request, jsonify, redirect
    source = Path('bit/bit_interface.py').read_text()
    tree = ast.parse(source)
    gate = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_workbench_billing_blocked')
    state = {'status':'unpaid'}
    namespace = dict(g=g, request=request, jsonify=jsonify, redirect=redirect,
        bit_db_api=SimpleNamespace(account_billing=lambda payload:state), logging=__import__('logging'))
    exec(compile(ast.Module(body=[gate], type_ignores=[]), 'billing-gate', 'exec'), namespace)
    blocked = namespace['_workbench_billing_blocked']
    app = Flask(__name__)
    user = {'id':2, 'role_key':'member'}
    with app.test_request_context('/api/orders'):
        assert blocked(user)[1] == 402
    with app.test_request_context('/'):
        assert blocked(user).location == '/billing'
    with app.test_request_context('/billing'):
        assert blocked(user) is None
    with app.test_request_context('/api/billing/me'):
        assert blocked(user) is None
    with app.test_request_context('/api/orders'):
        assert blocked({'id':1, 'role_key':'super_admin'}) is None
    for status in ('expired','over_limit'):
        state['status'] = status
        with app.test_request_context('/api/orders'):
            assert blocked(user)[1] == 402
    state['status'] = 'active'
    with app.test_request_context('/api/orders'):
        assert blocked(user) is None


def test_browser_admin_flow(store, tmp_path):
    from threading import Thread
    from werkzeug.serving import make_server
    from playwright.sync_api import sync_playwright
    root = Path(__file__).resolve().parents[1]
    app = Flask(__name__, template_folder=str(root/'bit/templates'), static_folder=str(root/'bit/static'))
    def storage(payload):
        return store(payload['action'], actor=payload['actor_id'], target=payload['user_id'], **payload['data'])
    app.register_blueprint(billing.create_blueprint(login_required=lambda fn:fn,
        current_user=lambda:{'id':1, 'role_key':'super_admin'}, storage=storage))
    server = make_server('127.0.0.1', 0, app)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width':1440,'height':1000})
            errors = []
            page.on('pageerror', lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}/billing')
            row = page.locator('#accounts tr').filter(has_text='sales')
            row.get_by_role('button', name='登记充值').click()
            assert page.locator('[name=site_count]').input_value() == '10'
            page.locator('[name=site_count]').fill('15')
            assert page.locator('[name=site_count]').evaluate('(input) => input.checkValidity()')
            assert '额度 15 个站点' in page.locator('#quote').inner_text()
            assert '¥299.00' in page.locator('#quote').inner_text()
            page.locator('[name=note]').fill('个人版增加站点')
            page.get_by_role('button', name='确认保存').click()
            page.wait_for_function("!document.querySelector('#editor').open")
            assert store('get')['capacity'] == 15
            row.get_by_role('button', name='登记充值').click()
            assert page.locator('[name=site_count]').input_value() == '15'
            page.locator('[name=plan]').select_option('enterprise')
            page.locator('[name=site_count]').fill('35')
            assert '¥649.00' in page.locator('#quote').inner_text()
            page.locator('[name=note]').fill('测试收款凭证 001')
            page.get_by_role('button', name='确认保存').click()
            page.wait_for_function("document.querySelector('#message').textContent.includes('已保存')")
            assert store('get')['monthly_cents'] == 64900
            assert store('history')[0]['amount_cents'] == 64900
            row.get_by_role('button', name='调整', exact=True).click()
            page.locator('[name=expires_at]').fill('2026-10-01T12:00')
            page.locator('[name=note]').fill('测试手动更正')
            page.get_by_role('button', name='确认保存').click()
            page.wait_for_function("!document.querySelector('#editor').open")
            page.wait_for_function("document.querySelector('#records').textContent.includes('测试手动更正')")
            assert store('get')['status'] == 'expired'
            page.locator('#search').fill('sales')
            assert page.locator('#accounts tr').count() == 1
            page.screenshot(path=str(tmp_path/'billing-desktop.png'), full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(tmp_path/'billing-mobile.png'), full_page=True)
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        worker.join(timeout=3)
