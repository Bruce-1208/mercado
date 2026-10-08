"""Offline coverage of central prepaid print accounting and PDF release rules."""
import sqlite3
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

from bit import print_wallet as wallet


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / 'print-wallet.db'
    raw = sqlite3.connect(path)
    raw.executescript('''
      CREATE TABLE workbench_organizations (organization_key TEXT PRIMARY KEY, is_active INTEGER);
      INSERT INTO workbench_organizations VALUES ('alpha',1),('beta',1);
      CREATE TABLE workbench_roles (role_key TEXT PRIMARY KEY, permissions_json TEXT, own_store_only INTEGER);
      INSERT INTO workbench_roles VALUES
        ('super_admin','["*"]',0), ('enterprise_admin','["*"]',0),
        ('warehouse','["order_print.view","order_print.execute"]',0),
        ('member','["order_print.view","order_print.execute"]',1),
        ('viewer','["order_print.view"]',0);
      CREATE TABLE workbench_users (id INTEGER PRIMARY KEY, role_key TEXT, is_active INTEGER,
        username TEXT, display_name TEXT, organization_key TEXT);
      INSERT INTO workbench_users VALUES
        (1,'super_admin',1,'admin','管理员','alpha'),
        (2,'member',1,'sales-a','Sales A','alpha'),
        (3,'warehouse',1,'warehouse-a','仓库 A','alpha'),
        (4,'enterprise_admin',1,'boss','企业管理员','alpha'),
        (5,'member',1,'sales-b','Sales B','alpha'),
        (6,'member',1,'sales-beta','Sales A','beta'),
        (7,'warehouse',1,'warehouse-beta','仓库 B','beta'),
        (8,'viewer',1,'viewer','只读','alpha'),
        (9,'warehouse',0,'inactive','停用仓库','alpha');
      ALTER TABLE workbench_users ADD COLUMN warehouse_key TEXT;
      UPDATE workbench_users SET warehouse_key='shahu' WHERE role_key='warehouse';
      CREATE TABLE mercado_store_tokens (id INTEGER PRIMARY KEY, organization_key TEXT, enabled INTEGER,
        display_name TEXT, nickname TEXT);
      INSERT INTO mercado_store_tokens VALUES (10,'alpha',1,'A 店铺','alpha-store'),
        (11,'alpha',1,'B 店铺','beta-store'),(20,'beta',1,'企业 B 店铺','foreign-store');
      CREATE TABLE mercado_store_site_settings (token_id INTEGER, site_id TEXT, salesperson TEXT);
      INSERT INTO mercado_store_site_settings VALUES (10,'MLM','Sales A'),(11,'MLM','Sales B'),(20,'MLM','Sales A');
      CREATE TABLE mercado_synced_orders (order_id TEXT PRIMARY KEY, token_id INTEGER, site_id TEXT, shop_name TEXT);
      INSERT INTO mercado_synced_orders VALUES ('1001',10,'MLM','旧 A 店铺'),('1002',10,'MLM','旧 A 店铺'),
        ('1101',11,'MLM','旧 B 店铺'),('2001',20,'MLM','旧企业 B 店铺');
      ALTER TABLE mercado_synced_orders ADD COLUMN transfer_warehouse TEXT NOT NULL DEFAULT 'shahu';
      CREATE TABLE mercado_order_operation_logs (order_id TEXT, action_type TEXT);
      CREATE TABLE workbench_print_wallets (user_id INTEGER PRIMARY KEY, organization_key TEXT,
        balance_cents INTEGER, total_recharge_cents INTEGER, total_charge_cents INTEGER, updated_at TEXT);
      CREATE TABLE workbench_print_wallet_records (id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, organization_key TEXT, request_id TEXT, kind TEXT, order_id TEXT DEFAULT '',
        amount_cents INTEGER, balance_after_cents INTEGER, actor_id INTEGER, actor_name TEXT,
        note TEXT, created_at TEXT, actor_username TEXT DEFAULT '', actor_role_key TEXT DEFAULT '',
        UNIQUE(user_id, request_id, kind, order_id));
      CREATE TABLE workbench_print_requests (request_id TEXT PRIMARY KEY, actor_id INTEGER,
        order_ids_json TEXT, status TEXT, reserved_cents INTEGER, charge_cents INTEGER DEFAULT 0,
        refund_cents INTEGER DEFAULT 0, successful_order_ids_json TEXT, created_at TEXT, settled_at TEXT);
      CREATE TABLE workbench_print_request_orders (request_id TEXT, order_id TEXT,
        user_id INTEGER, organization_key TEXT, amount_cents INTEGER, token_id INTEGER,
        shop_name TEXT DEFAULT '', site_id TEXT DEFAULT '', salesperson_name TEXT DEFAULT '',
        salesperson_username TEXT DEFAULT '', printed_at TEXT, print_time_source TEXT DEFAULT '',
        PRIMARY KEY(request_id,order_id));
    ''')
    raw.close()
    monkeypatch.setattr(wallet, 'ensure_tables', lambda cursor: None)
    monkeypatch.setattr(wallet, 'now_cn', lambda: datetime(2026, 10, 6, 12))
    local = wallet.wallet_local
    automatic_local = wallet._automatic_wallet_local
    fault = {'sql': None}
    events = []

    class Cursor:
        def __init__(self, raw): self.raw = raw.cursor()
        def __enter__(self): return self
        def __exit__(self, *args): self.raw.close()
        def execute(self, sql, params=()):
            events.append(sql)
            if fault['sql'] and fault['sql'] in sql:
                raise RuntimeError('simulated database write failure')
            if sql == 'SET TRANSACTION ISOLATION LEVEL READ COMMITTED':
                if self.raw.connection.in_transaction:
                    raise RuntimeError(1568, "Transaction characteristics can't be changed while a transaction is in progress")
                return
            self.raw.execute(sql.replace('%s', '?').replace(' FOR UPDATE', ''),
                tuple(value.isoformat(' ') if isinstance(value, datetime) else value for value in params))
        def fetchone(self):
            row = self.raw.fetchone()
            return dict(row) if row else None
        def fetchall(self): return [dict(row) for row in self.raw.fetchall()]
        @property
        def lastrowid(self): return self.raw.lastrowid
        @property
        def rowcount(self): return self.raw.rowcount

    class Connection:
        def __init__(self):
            self.raw = sqlite3.connect(path)
            self.raw.row_factory = sqlite3.Row
        def cursor(self): return Cursor(self.raw)
        def begin(self):
            events.append('BEGIN')
            self.raw.execute('BEGIN')
        def commit(self):
            events.append('COMMIT')
            self.raw.commit()
        def rollback(self): self.raw.rollback()
        def close(self): self.raw.close()

    class Store:
        def __call__(self, action, actor=1, target=2, **data):
            payload = {'action': action, 'actor_id': actor, 'user_id': target, 'data': data}
            if 'organization_key' in data:
                payload['organization_key'] = data.pop('organization_key')
            return self.run(payload)
        def run(self, payload): return local(payload, connect=Connection)
        def run_auto(self, action, data): return automatic_local(action, data, connect=Connection)
        def execute(self, sql, params=()):
            with sqlite3.connect(path) as db:
                db.execute(sql, params)
        def rows(self, sql, params=()):
            with sqlite3.connect(path) as db:
                db.row_factory = sqlite3.Row
                return [dict(row) for row in db.execute(sql, params)]
        def fail_on(self, sql): fault['sql'] = sql
        @property
        def events(self): return events

    return Store()


def recharge(store, amount='9.00', target=2, request_id='recharge-001', **changes):
    return store('recharge', target=target, amount=amount,
        request_id=request_id, note='已收到银行转账 001', **changes)


def reserve(store, orders=('1001',), request_id='print-request-001', actor=3, **data):
    return store('reserve', actor=actor, order_ids=list(orders), request_id=request_id, **data)


def settle(store, successful=('1001',), request_id='print-request-001', actor=3):
    return store('settle', actor=actor, successful_order_ids=list(successful), request_id=request_id)


def test_initial_zero_and_integer_cents_recharge(store):
    account = store('get', actor=2)
    assert account['balance_cents'] == 0
    assert account['printable_orders'] == 0
    assert store('history', actor=2) == []
    recharge(store, '0.10')
    recharge(store, '0.20', request_id='recharge-002')
    account = store('get', actor=2)
    assert account['balance_cents'] == 30
    assert account['total_recharge_cents'] == 30
    assert account['printable_orders'] == 0
    assert [row['amount_cents'] for row in store('history')] == [20, 10]


@pytest.mark.parametrize('amount', ['NaN', 'Infinity', '-1', '0', '1.001', '10000001', 'oops'])
def test_invalid_recharge_does_not_change_balance_or_records(store, amount):
    with pytest.raises(ValueError): recharge(store, amount)
    assert store('get')['balance_cents'] == 0
    assert store('history') == []


def test_permissions_rechecked_and_enterprise_isolation(store):
    assert len(store('list')) >= 6
    assert [account['user_id'] for account in store('list', actor=2)] == [2]
    assert {account['organization_key'] for account in store('list', actor=3)} == {'alpha'}
    assert {account['organization_key'] for account in store('list', actor=7)} == {'beta'}
    for actor in (2, 3, 7, 8):
        with pytest.raises(PermissionError): recharge(store, actor=actor)
    recharge(store, actor=4)
    with pytest.raises(PermissionError): recharge(store, actor=4, target=6)
    assert store('get', actor=2)['user_id'] == 2
    with pytest.raises(PermissionError): store('get', actor=2, target=5)
    with pytest.raises(PermissionError): store('history', actor=2, target=6)
    with pytest.raises(PermissionError): reserve(store, actor=8)
    with pytest.raises(PermissionError): reserve(store, actor=9)
    recharge(store, target=6)
    with pytest.raises(PermissionError): reserve(store, orders=('2001',))
    with pytest.raises(PermissionError): reserve(store, actor=2, orders=('1101',))
    assert store('get', target=6)['balance_cents'] == 900
    store.execute("UPDATE workbench_organizations SET is_active=0 WHERE organization_key='alpha'")
    with pytest.raises(PermissionError): reserve(store)


def test_exact_three_yuan_and_new_reprint_charge(store):
    recharge(store, '6')
    reserved = reserve(store, orders=('1001', '1001'))
    assert reserved['reserved_cents'] == 300
    assert reserved['order_ids'] == ['1001']
    assert store('get')['balance_cents'] == 300
    result = settle(store)
    assert result['charge_cents'] == 300
    assert result['refund_cents'] == 0
    reserve(store, request_id='print-request-002')
    settle(store, request_id='print-request-002')
    account = store('get')
    assert account['balance_cents'] == 0
    assert account['total_charge_cents'] == 600
    assert sum(row['amount_cents'] for row in store('history') if row['kind'] == 'print') == -600


def test_insufficient_whole_batch_rolls_back_every_owner(store):
    recharge(store, '9')
    recharge(store, '2.99', target=5, request_id='recharge-002')
    before = store('history')
    with pytest.raises(wallet.WalletInsufficientFunds):
        reserve(store, orders=('1001', '1101'))
    assert store('get')['balance_cents'] == 900
    assert store('get', target=5)['balance_cents'] == 299
    assert store('history') == before
    assert store.rows('SELECT * FROM workbench_print_requests') == []


def test_multi_owner_batch_partial_failure_refunds_correct_owner(store):
    recharge(store, '9')
    recharge(store, '3', target=5, request_id='recharge-002')
    reserve(store, orders=('1001', '1002', '1101'))
    assert store('get')['balance_cents'] == 300
    assert store('get', target=5)['balance_cents'] == 0
    result = settle(store, successful=('1002',))
    assert result['charge_cents'] == 300
    assert result['refund_cents'] == 600
    assert store('get')['balance_cents'] == 600
    assert store('get', target=5)['balance_cents'] == 300
    assert store('get')['total_charge_cents'] == 300
    assert store('get', target=5)['total_charge_cents'] == 0
    assert sum(row['amount_cents'] for row in store('history') if row['kind'] == 'refund') == 300
    assert sum(row['amount_cents'] for row in store('history', target=5) if row['kind'] == 'refund') == 300


def test_all_failed_settlement_and_retry_refund_once(store):
    recharge(store, '6')
    reserve(store, orders=('1001', '1002'))
    first = settle(store, successful=())
    assert first['charge_cents'] == 0
    assert first['refund_cents'] == 600
    history = store('history')
    second = settle(store, successful=())
    assert second['refund_cents'] == 600
    assert store('history') == history
    assert store('get')['balance_cents'] == 600
    with pytest.raises(wallet.WalletConflict): settle(store, successful=('1001',))


def test_recharge_idempotency_and_reservation_conflict(store):
    recharge(store)
    recharge(store)
    assert store('get')['balance_cents'] == 900
    assert len(store('history')) == 1
    reserve(store)
    history = store('history')
    with pytest.raises(wallet.WalletConflict): reserve(store)
    with pytest.raises(wallet.WalletConflict): reserve(store, orders=('1002',))
    with pytest.raises(PermissionError): settle(store, actor=7)
    assert store('get')['balance_cents'] == 600
    assert store('history') == history


@pytest.mark.parametrize('kind', ['missing', 'ambiguous', 'inactive'])
def test_unresolved_salesperson_blocks_batch_without_charge(store, kind):
    recharge(store)
    if kind == 'missing':
        store.execute("UPDATE mercado_store_site_settings SET salesperson='不存在' WHERE token_id=10")
    elif kind == 'ambiguous':
        store.execute("INSERT INTO workbench_users (id,role_key,is_active,username,display_name,organization_key) VALUES (10,'member',1,'other-a','Sales A','alpha')")
    else:
        store.execute('UPDATE workbench_users SET is_active=0 WHERE id=2')
    with pytest.raises(ValueError): reserve(store)
    assert store('get')['balance_cents'] == 900
    assert len(store('history')) == 1


def test_write_failure_rolls_back_recharge_reserve_and_settle(store):
    store.fail_on('INSERT INTO workbench_print_wallet_records')
    with pytest.raises(RuntimeError, match='simulated'): recharge(store)
    store.fail_on(None)
    assert store('get')['balance_cents'] == 0
    assert store('history') == []
    recharge(store, '6')
    store.fail_on('INSERT INTO workbench_print_request_orders')
    with pytest.raises(RuntimeError, match='simulated'): reserve(store, orders=('1001', '1002'))
    store.fail_on(None)
    assert store('get')['balance_cents'] == 600
    assert store.rows('SELECT * FROM workbench_print_requests') == []
    reserve(store, orders=('1001', '1002'))
    before = store('history')
    store.fail_on('INSERT INTO workbench_print_wallet_records')
    with pytest.raises(RuntimeError, match='simulated'): settle(store, successful=())
    store.fail_on(None)
    assert store('get')['balance_cents'] == 0
    assert store('history') == before
    assert store.rows('SELECT status FROM workbench_print_requests')[0]['status'] == 'reserved'
    assert settle(store, successful=())['refund_cents'] == 600


def install_downloader(monkeypatch, store, downloader):
    # The real accounting runs against SQLite; platform access is fully isolated.
    monkeypatch.setattr(wallet, 'wallet_local', store.run)
    monkeypatch.setattr(wallet, '_automatic_wallet_local', store.run_auto)
    monkeypatch.setitem(sys.modules, 'bit.bit_order_labels',
        SimpleNamespace(download_order_labels=downloader))


def test_insufficient_batch_never_starts_label_download(store, monkeypatch):
    recharge(store, '9')
    recharge(store, '2.99', target=5, request_id='recharge-002')
    calls = []
    install_downloader(monkeypatch, store, lambda orders: calls.append(orders))
    with pytest.raises(wallet.WalletInsufficientFunds):
        wallet.download_paid_labels(['1001', '1101'], 3, 'download-request-001')
    assert calls == []
    assert store('get')['balance_cents'] == 900
    assert store('get', target=5)['balance_cents'] == 299


def test_pdf_is_returned_after_partial_settlement_and_duplicate_request_is_blocked(store, monkeypatch):
    recharge(store, '6')
    calls = []

    def downloader(orders):
        calls.append(orders)
        assert store('get')['balance_cents'] == 0  # Reserve precedes platform access.
        assert store.rows('SELECT status FROM workbench_print_requests')[0]['status'] == 'reserved'
        return {'content': b'%PDF-valid-test-content', 'order_ids': ['1002'],
            'failed_order_ids': ['1001']}

    install_downloader(monkeypatch, store, downloader)
    result = wallet.download_paid_labels(['1001', '1002'], 3, 'download-request-001')
    assert result['content'] == b'%PDF-valid-test-content'
    assert result['wallet_charge_cents'] == 300
    assert result['wallet_request_id'] == 'download-request-001'
    assert store('get')['balance_cents'] == 300
    assert store.rows('SELECT status FROM workbench_print_requests')[0]['status'] == 'settled'
    with pytest.raises(wallet.WalletConflict):
        wallet.download_paid_labels(['1001', '1002'], 3, 'download-request-001')
    assert calls == [['1001', '1002']]
    assert store('get')['balance_cents'] == 300


def test_download_failure_refunds_all_before_returning_error(store, monkeypatch):
    recharge(store, '6')

    def downloader(orders):
        assert store('get')['balance_cents'] == 0
        raise RuntimeError('platform cannot generate labels')

    install_downloader(monkeypatch, store, downloader)
    with pytest.raises(RuntimeError, match='platform cannot generate labels'):
        wallet.download_paid_labels(['1001', '1002'], 3, 'download-request-001')
    assert store('get')['balance_cents'] == 600
    assert store('get')['total_charge_cents'] == 0
    request = store.rows('SELECT * FROM workbench_print_requests')[0]
    assert request['status'] == 'settled'
    assert request['refund_cents'] == 600


@pytest.mark.parametrize('result, expected', [
    ({'content': b'%PDF-test', 'order_ids': []}, wallet.WalletConflict),
    ({'content': b'%PDF-test', 'order_ids': ['2001']}, wallet.WalletConflict),
    ({'content': b'not a PDF', 'order_ids': ['1001']}, ValueError),
])
def test_empty_unexpected_or_invalid_generated_labels_refund_without_pdf_release(store, monkeypatch, result, expected):
    recharge(store, '3')
    install_downloader(monkeypatch, store, lambda orders: dict(result))
    with pytest.raises(expected):
        wallet.download_paid_labels(['1001'], 3, 'download-request-001')
    assert store('get')['balance_cents'] == 300
    assert store('get')['total_charge_cents'] == 0
    assert store.rows('SELECT status FROM workbench_print_requests')[0]['status'] == 'settled'


def test_settlement_failure_preserves_reservation_and_does_not_release_pdf(store, monkeypatch):
    recharge(store, '6')
    calls = []

    def downloader(orders):
        calls.append(orders)
        store.fail_on('UPDATE workbench_print_requests')
        return {'content': b'%PDF-valid-test-content', 'order_ids': ['1002']}

    install_downloader(monkeypatch, store, downloader)
    with pytest.raises(wallet.WalletConflict, match='尚未确认'):
        wallet.download_paid_labels(['1001', '1002'], 3, 'download-request-001')
    store.fail_on(None)
    assert store('get')['balance_cents'] == 0
    assert store('get')['total_charge_cents'] == 600
    assert not any(row['kind'] == 'refund' for row in store('history'))
    assert store.rows('SELECT status FROM workbench_print_requests')[0]['status'] == 'reserved'
    with pytest.raises(wallet.WalletConflict):
        wallet.download_paid_labels(['1001', '1002'], 3, 'download-request-001')
    assert calls == [['1001', '1002']]
    assert settle(store, successful=('1002',), request_id='download-request-001')['refund_cents'] == 300
    assert settle(store, successful=('1002',), request_id='download-request-001')['duplicate'] is True
    assert store('get')['balance_cents'] == 300


def test_failed_generation_with_failed_refund_keeps_uncertain_request_for_review(store, monkeypatch):
    recharge(store, '3')

    def downloader(orders):
        store.fail_on('INSERT INTO workbench_print_wallet_records')
        raise RuntimeError('platform cannot generate label')

    install_downloader(monkeypatch, store, downloader)
    with pytest.raises(wallet.WalletConflict, match='退款结果尚未确认'):
        wallet.download_paid_labels(['1001'], 3, 'download-request-001')
    store.fail_on(None)
    assert store('get')['balance_cents'] == 0
    assert store.rows('SELECT status FROM workbench_print_requests')[0]['status'] == 'reserved'
    assert settle(store, successful=(), request_id='download-request-001')['refund_cents'] == 300
    assert store('get')['balance_cents'] == 300


@pytest.mark.parametrize('orders', [[], [True], [['1001']], [''], [str(value) for value in range(101)]])
def test_invalid_order_selection_cannot_create_charge(store, orders):
    recharge(store)
    with pytest.raises(ValueError): reserve(store, orders=orders)
    assert store('get')['balance_cents'] == 900
    assert len(store('history')) == 1


def test_username_matching_and_member_own_order_are_supported(store):
    recharge(store, '3')
    store.execute("UPDATE mercado_store_site_settings SET salesperson='  SALES-A  ' WHERE token_id=10")
    reserve(store, actor=2)
    settle(store, actor=2)
    assert store('get')['balance_cents'] == 0


def test_disabled_store_and_explicit_foreign_scope_cannot_print(store):
    recharge(store)
    store.execute('UPDATE mercado_store_tokens SET enabled=0 WHERE id=10')
    with pytest.raises(PermissionError): reserve(store)
    store.execute('UPDATE mercado_store_tokens SET enabled=1 WHERE id=10')
    with pytest.raises(PermissionError): reserve(store, organization_key='beta')
    assert store('get')['balance_cents'] == 900


def test_duplicate_recharge_with_changed_amount_conflicts(store):
    recharge(store)
    with pytest.raises(wallet.WalletConflict): recharge(store, '10')
    assert store('get')['balance_cents'] == 900
    assert len(store('history')) == 1


def test_integer_cent_recharge_and_audit_actor(store):
    result = recharge(store, amount=None, amount_cents=303, actor=4)
    assert result['account']['balance_cents'] == 303
    assert result['account']['printable_orders'] == 1
    record = store('history')[0]
    assert record['actor_id'] == 4
    assert record['actor_name'] == '企业管理员'
    assert record['amount_cents'] == 303
    assert record['balance_after_cents'] == 303
    assert record['note'] == '已收到银行转账 001'


def test_admin_cancels_abandoned_request_once_and_late_settlement_is_blocked(store):
    recharge(store, '6')
    reserve(store, orders=('1001', '1002'))
    result = store('cancel', actor=4, request_id='print-request-001', note='浏览器关闭，已确认未交付面单')
    assert result['status'] == 'cancelled'
    assert result['refund_cents'] == 600
    assert store('get')['balance_cents'] == 600
    history = store('history')
    refunds = [row for row in history if row['kind'] == 'refund']
    assert len(refunds) == 2
    assert all(row['actor_id'] == 4 and row['request_status'] == 'cancelled' for row in refunds)
    repeated = store('cancel', actor=4, request_id='print-request-001', note='重复确认取消')
    assert repeated['duplicate'] is True
    assert store('history') == history
    with pytest.raises(wallet.WalletConflict): settle(store)
    assert store('get')['total_charge_cents'] == 0


def test_cancel_requires_admin_same_enterprise_and_reason(store):
    recharge(store, '3')
    reserve(store)
    for actor in (2, 3, 8):
        with pytest.raises(PermissionError):
            store('cancel', actor=actor, request_id='print-request-001', note='取消')
    store.execute("INSERT INTO workbench_users (id,role_key,is_active,username,display_name,organization_key) VALUES (10,'enterprise_admin',1,'boss-beta','企业 B 管理员','beta')")
    with pytest.raises(PermissionError):
        store('cancel', actor=10, request_id='print-request-001', note='取消其他企业')
    with pytest.raises(ValueError):
        store('cancel', actor=4, request_id='print-request-001', note='')
    assert store('get')['balance_cents'] == 0
    assert store.rows('SELECT status FROM workbench_print_requests')[0]['status'] == 'reserved'


def test_already_settled_request_cannot_be_cancelled(store):
    recharge(store, '3')
    reserve(store)
    settle(store)
    with pytest.raises(wallet.WalletConflict):
        store('cancel', actor=4, request_id='print-request-001', note='不能退已交付面单')
    assert store('get')['balance_cents'] == 0
    assert store('get')['total_charge_cents'] == 300


def test_cancel_while_download_in_progress_prevents_pdf_release(store, monkeypatch):
    recharge(store, '3')

    def downloader(orders):
        store('cancel', actor=4, request_id='download-request-001', note='中断任务')
        return {'content': b'%PDF-valid-test-content', 'order_ids': ['1001']}

    install_downloader(monkeypatch, store, downloader)
    with pytest.raises(wallet.WalletConflict, match='尚未确认'):
        wallet.download_paid_labels(['1001'], 3, 'download-request-001')
    assert store('get')['balance_cents'] == 300
    assert store('get')['total_charge_cents'] == 0
    assert store.rows('SELECT status FROM workbench_print_requests')[0]['status'] == 'cancelled'
    assert len([row for row in store('history') if row['kind'] == 'refund']) == 1


@pytest.mark.parametrize('change', ['disabled', 'moved'])
def test_refund_returns_to_original_wallet_after_owner_disabled_or_moved(store, change):
    recharge(store, '3')
    reserve(store)
    if change == 'disabled':
        store.execute('UPDATE workbench_users SET is_active=0 WHERE id=2')
    else:
        store.execute("UPDATE workbench_users SET organization_key='beta' WHERE id=2")
    result = settle(store, successful=())
    assert result['refund_cents'] == 300
    original = store.rows('SELECT * FROM workbench_print_wallets WHERE user_id=2')[0]
    assert original['organization_key'] == 'alpha'
    assert original['balance_cents'] == 300
    assert original['total_charge_cents'] == 0
    if change == 'moved':
        assert store('get', actor=7)['balance_cents'] == 0
        assert store('history', actor=7) == []
        with pytest.raises(wallet.WalletConflict): recharge(store, request_id='recharge-002')


def test_admin_cancel_write_failure_rolls_back_refund(store):
    recharge(store, '3')
    reserve(store)
    store.fail_on('UPDATE workbench_print_requests')
    with pytest.raises(RuntimeError, match='simulated'):
        store('cancel', actor=4, request_id='print-request-001', note='确认中止')
    store.fail_on(None)
    assert store('get')['balance_cents'] == 0
    assert not any(row['kind'] == 'refund' for row in store('history'))
    assert store.rows('SELECT status FROM workbench_print_requests')[0]['status'] == 'reserved'


def test_print_details_keep_original_order_and_printer_snapshots(store, monkeypatch):
    recharge(store, '3')
    reserve(store)
    pending = store('records', actor=2)['records'][0]
    assert pending['print_result'] == 'pending'
    assert pending['requested_at'] == '2026-10-06T12:00:00'
    assert pending['printed_at'] is None
    assert pending['printer_id'] == 3
    assert pending['printer_name'] == '仓库 A'
    assert pending['printer_username'] == 'warehouse-a'
    assert pending['printer_role_key'] == 'warehouse'
    assert pending['shop_name'] == 'A 店铺'
    assert pending['token_id'] == 10
    assert pending['site_id'] == 'MLM'
    assert pending['salesperson_name'] == 'Sales A'
    assert pending['salesperson_username'] == 'sales-a'

    store.execute("UPDATE workbench_users SET display_name='新仓库名',username='new-name',role_key='viewer' WHERE id=3")
    store.execute("UPDATE workbench_users SET display_name='新业务员名' WHERE id=2")
    store.execute("UPDATE mercado_store_tokens SET display_name='新店铺名' WHERE id=10")
    store.execute("UPDATE mercado_store_site_settings SET salesperson='Sales B' WHERE token_id=10")
    monkeypatch.setattr(wallet, 'now_cn', lambda: datetime(2026, 10, 6, 12, 7))
    store('settle', actor=3, request_id='print-request-001', successful_order_ids=['1001'],
          generated_at='2026-10-06T12:05:00')
    printed = store('records', actor=2)['records'][0]
    assert printed['print_result'] == 'completed'
    assert printed['request_status'] == 'settled'
    assert printed['created_at'] == '2026-10-06T12:00:00'
    assert printed['requested_at'] == '2026-10-06T12:00:00'
    assert printed['printed_at'] == '2026-10-06T12:05:00'
    assert printed['settled_at'] == '2026-10-06T12:07:00'
    assert printed['print_time_source'] == 'label_generated'
    assert printed['printer_name'] == '仓库 A'
    assert printed['printer_username'] == 'warehouse-a'
    assert printed['printer_role_key'] == 'warehouse'
    assert printed['shop_name'] == 'A 店铺'
    assert printed['salesperson_name'] == 'Sales A'
    assert printed['salesperson_username'] == 'sales-a'


def test_batch_records_use_each_order_result_without_revealing_other_owner_orders(store):
    recharge(store, '6')
    recharge(store, '3', target=5, request_id='recharge-002')
    reserve(store, orders=('1001', '1002', '1101'))
    settle(store, successful=('1002', '1101'))
    records = store('records', actor=2)['records']
    assert len([row for row in records if row['kind'] == 'print']) == 2
    by_order = {row['order_id']: row for row in records if row['kind'] == 'print'}
    assert by_order['1001']['print_result'] == 'refunded'
    assert by_order['1001']['printed_at'] is None
    assert by_order['1002']['print_result'] == 'completed'
    assert by_order['1002']['print_time_source'] == 'settled'
    assert all('successful_order_ids_json' not in row for row in records)
    assert all(row['order_id'] != '1101' for row in records)
    assert all(row['salesperson_name'] != 'Sales B' for row in records)
    with pytest.raises(PermissionError):
        store('records', actor=2, target=5)
    with pytest.raises(PermissionError):
        store('records', actor=7, target=2)


def test_refund_filter_tracks_original_printer_and_preserves_refund_operator(store, monkeypatch):
    recharge(store, '3')
    reserve(store)
    monkeypatch.setattr(wallet, 'now_cn', lambda: datetime(2026, 10, 6, 12, 10))
    store('cancel', actor=4, request_id='print-request-001', note='已确认中断')
    records = store('records', actor=2, kind='refund', printer='仓库 A')['records']
    assert len(records) == 1
    refunded = records[0]
    assert refunded['print_result'] == 'cancelled'
    assert refunded['printer_id'] == 3
    assert refunded['printer_name'] == '仓库 A'
    assert refunded['actor_id'] == 4
    assert refunded['actor_name'] == '企业管理员'
    assert refunded['actor_role_key'] == 'enterprise_admin'
    assert refunded['requested_at'] == '2026-10-06T12:00:00'
    assert refunded['created_at'] == '2026-10-06T12:10:00'
    assert refunded['printed_at'] is None
    assert store('records', actor=2, printer='warehouse-a')['total'] == 2
    assert store('records', actor=2, printer='企业管理员')['total'] == 0
    assert store('records', actor=2, printer='仓库 A', kind='recharge')['total'] == 0


def test_old_print_records_show_preserved_name_and_only_confirmed_completion_time(store):
    recharge(store, '6')
    reserve(store, orders=('1001', '1002'))
    settle(store, successful=('1002',))
    store.execute("UPDATE workbench_print_wallet_records SET actor_username='',actor_role_key='' WHERE kind='print'")
    store.execute("UPDATE workbench_print_request_orders SET token_id=NULL,shop_name='',site_id='',printed_at=NULL,print_time_source='' ")
    legacy = {row['order_id']: row for row in store('records', actor=2, kind='print')['records']}
    assert legacy['1002']['printer_name'] == '仓库 A'
    assert legacy['1002']['printer_role_key'] == ''
    assert legacy['1002']['printed_at'] == '2026-10-06T12:00:00'
    assert legacy['1002']['print_time_source'] == 'settled'
    assert legacy['1002']['shop_name'] == ''
    assert legacy['1001']['printed_at'] is None
    assert legacy['1001']['print_result'] == 'refunded'
    store.execute('DELETE FROM workbench_print_requests')
    missing = store('records', actor=2, kind='print')['records']
    assert all(row['print_result'] == 'unknown' and row['printed_at'] is None for row in missing)
    assert all(row['requested_at'] == row['created_at'] for row in missing)


def test_records_filter_by_order_date_kind_and_page(store, monkeypatch):
    recharge(store, '6')
    reserve(store, orders=('1001', '1002'))
    settle(store, successful=('1002',))
    monkeypatch.setattr(wallet, 'now_cn', lambda: datetime(2026, 10, 7, 0))
    recharge(store, '3', request_id='recharge-next-day')
    filtered = store('records', actor=2, order_id='100', date_from='2026-10-06', date_to='2026-10-06', kind='print', limit='1', offset='1')
    assert filtered['total'] == 2
    assert filtered['limit'] == 1
    assert filtered['offset'] == 1
    assert len(filtered['records']) == 1
    assert filtered['records'][0]['order_id'] == '1001'
    assert store('records', actor=2, date_from='2026-10-06', date_to='2026-10-06')['total'] == 4
    assert store('records', actor=2, date_from='2026-10-07')['total'] == 1
    assert store('records', actor=2, order_id='%')['total'] == 0
    assert store('records', actor=2, offset=999)['records'] == []


def test_pagination_can_retrieve_records_beyond_old_500_row_cap(store):
    for index in range(520):
        store.execute('''INSERT INTO workbench_print_wallet_records
          (user_id,organization_key,request_id,kind,order_id,amount_cents,
           balance_after_cents,actor_id,actor_name,note,created_at)
          VALUES (2,'alpha',?,'recharge','',100,100,1,'管理员','历史充值','2026-10-06 12:00:00')''',
          (f'historical-{index:04d}',))
    first = store('records', actor=2, limit=100)
    last = store('records', actor=2, limit=100, offset=500)
    assert first['total'] == last['total'] == 520
    assert len(first['records']) == 100
    assert len(last['records']) == 20
    assert not set(row['id'] for row in first['records']) & set(row['id'] for row in last['records'])
    assert len(store('history', actor=2)) == 500
    assert all(row['printer_id'] is None and row['printer_name'] == '' and row['printed_at'] is None
               for row in last['records'])


@pytest.mark.parametrize('filters', [
    {'limit': 0}, {'limit': 101}, {'offset': -1}, {'offset': True},
    {'limit': '0 OR 1=1'}, {'kind': 'any'}, {'order_id': 'a' * 65}, {'printer': ['仓库']},
    {'date_from': '2026-10-6'}, {'date_to': '2026-02-30'},
    {'date_from': '2026-10-07', 'date_to': '2026-10-06'},
])
def test_invalid_record_filters_are_rejected(store, filters):
    with pytest.raises(ValueError):
        store('records', actor=2, **filters)


def test_label_generation_time_is_captured_before_settlement(store, monkeypatch):
    recharge(store, '3')
    observed_times = iter([datetime(2026, 10, 6, 12), datetime(2026, 10, 6, 12, 5),
                           datetime(2026, 10, 6, 12, 7), datetime(2026, 10, 6, 12, 8)])
    monkeypatch.setattr(wallet, 'now_cn', lambda: next(observed_times))
    install_downloader(monkeypatch, store, lambda orders: {'content': b'%PDF-valid-test', 'order_ids': ['1001']})
    wallet.download_paid_labels(['1001'], 3, 'download-timed-001')
    row = store('records', actor=2, kind='print')['records'][0]
    assert row['requested_at'] == '2026-10-06T12:00:00'
    assert row['printed_at'] == '2026-10-06T12:05:00'
    assert row['settled_at'] == '2026-10-06T12:07:00'
    assert row['print_time_source'] == 'label_generated'


@pytest.mark.parametrize('value', ['bad-time', '2026-10-06T11:59:59', '2026-10-06T12:00:01'])
def test_invalid_generation_timestamp_preserves_pending_request(store, value):
    recharge(store, '3')
    reserve(store)
    with pytest.raises(ValueError):
        store('settle', actor=3, request_id='print-request-001', successful_order_ids=['1001'], generated_at=value)
    row = store('records', actor=2, kind='print')['records'][0]
    assert row['print_result'] == 'pending'
    assert row['printed_at'] is None
    assert store('get')['balance_cents'] == 0


def test_schema_migration_only_adds_missing_audit_columns():
    class Cursor:
        def __init__(self):
            self.statements = []
            self.table = None
        def execute(self, sql, params=()):
            self.statements.append(sql)
            if sql.startswith('SHOW COLUMNS'):
                self.table = sql
        def fetchall(self):
            if 'wallet_records' in self.table:
                return [{'Field': field} for field in ('actor_name', 'actor_username', 'created_at')]
            return [{'Field': 'printed_at'}]
    cursor = Cursor()
    wallet.ensure_tables(cursor)
    alters = [sql for sql in cursor.statements if sql.startswith('ALTER TABLE')]
    assert len(alters) == 7
    assert any('actor_role_key' in sql for sql in alters)
    assert not any('`actor_username`' in sql or '`actor_name`' in sql or '`printed_at`' in sql for sql in alters)
    assert all(sql.startswith(('CREATE TABLE', 'SHOW COLUMNS', 'ALTER TABLE')) for sql in cursor.statements)


def test_concurrent_additive_migration_accepts_duplicate_column_only():
    class Cursor:
        def __init__(self, code): self.code = code
        def execute(self, sql, params=()):
            if sql.startswith('ALTER TABLE'):
                raise RuntimeError(self.code, 'column migration')
        def fetchall(self): return []
    wallet._ensure_columns(Cursor(1060), 'workbench_print_wallet_records', {'actor_role_key': 'VARCHAR(64)'})
    with pytest.raises(RuntimeError):
        wallet._ensure_columns(Cursor(1142), 'workbench_print_wallet_records', {'actor_role_key': 'VARCHAR(64)'})


def test_automatic_print_uses_fixed_system_audit_and_refunds_each_actual_owner(store, monkeypatch):
    recharge(store, '6')
    recharge(store, '3', target=6, request_id='recharge-beta')
    downloads, persisted = [], []

    def downloader(orders):
        downloads.append(orders)
        assert store('get')['balance_cents'] == 0
        assert store('get', target=6)['balance_cents'] == 0
        return {'content': b'%PDF-valid-test', 'order_ids': ['1002', '2001'],
                'failed_order_ids': ['1001']}

    def persist(result):
        assert store.rows('SELECT status FROM workbench_print_requests')[0]['status'] == 'reserved'
        assert all(row['printed_at'] is None for row in store.rows('SELECT * FROM workbench_print_request_orders'))
        persisted.append(dict(result))

    install_downloader(monkeypatch, store, downloader)
    result = wallet.download_automatic_paid_labels(['1001', '1002', '2001'],
        'automatic-request-001', persist_result=persist)
    assert result['wallet_charge_cents'] == 600
    assert result['wallet_request_id'] == 'automatic-request-001'
    assert persisted[0]['content'] == b'%PDF-valid-test'
    assert persisted[0]['order_ids'] == ['1002', '2001']
    assert store('get')['balance_cents'] == 300
    assert store('get', target=6)['balance_cents'] == 0
    request = store.rows('SELECT * FROM workbench_print_requests')[0]
    assert request['actor_id'] == 0
    assert request['charge_cents'] == 600
    assert request['refund_cents'] == 300
    records = store.rows("SELECT * FROM workbench_print_wallet_records WHERE kind IN ('print','refund')")
    assert {row['user_id'] for row in records} == {2, 6}
    assert all(row['actor_id'] == 0 and row['actor_name'] == '系统自动打印'
               and row['actor_username'] == 'system:auto-print' and row['actor_role_key'] == 'system'
               for row in records)
    with pytest.raises(wallet.WalletConflict):
        wallet.download_automatic_paid_labels(['1001', '1002', '2001'],
            'automatic-request-001', persist_result=persist)
    assert downloads == [['1001', '1002', '2001']]
    assert len(persisted) == 1
    assert store('get')['balance_cents'] == 300


def test_automatic_insufficient_funds_prevents_download_and_persistence(store, monkeypatch):
    recharge(store, '2.99')
    calls = []
    install_downloader(monkeypatch, store, lambda orders: calls.append('download'))
    with pytest.raises(wallet.WalletInsufficientFunds):
        wallet.download_automatic_paid_labels(['1001'], 'automatic-request-001',
            persist_result=lambda result: calls.append('persist'))
    assert calls == []
    assert store('get')['balance_cents'] == 299
    assert store.rows('SELECT * FROM workbench_print_requests') == []


def test_automatic_persistence_failure_refunds_all_without_completed_orders(store, monkeypatch):
    recharge(store, '6')
    install_downloader(monkeypatch, store, lambda orders: {'content': b'%PDF-valid-test', 'order_ids': ['1002']})

    def persist(result):
        assert result['content'].startswith(b'%PDF')
        raise OSError('disk full')

    with pytest.raises(OSError, match='disk full'):
        wallet.download_automatic_paid_labels(['1001', '1002'], 'automatic-request-001', persist_result=persist)
    assert store('get')['balance_cents'] == 600
    assert store('get')['total_charge_cents'] == 0
    request = store.rows('SELECT * FROM workbench_print_requests')[0]
    assert request['status'] == 'settled'
    assert request['charge_cents'] == 0
    assert request['refund_cents'] == 600
    assert request['successful_order_ids_json'] == '[]'
    assert all(row['printed_at'] is None for row in store.rows('SELECT * FROM workbench_print_request_orders'))
    assert all(row['actor_id'] == 0 for row in store.rows("SELECT * FROM workbench_print_wallet_records WHERE kind='refund'"))


@pytest.mark.parametrize('change, expected', [
    ("UPDATE mercado_store_tokens SET enabled=0 WHERE id=10", PermissionError),
    ("UPDATE workbench_users SET is_active=0 WHERE id=2", ValueError),
    ("UPDATE workbench_organizations SET is_active=0 WHERE organization_key='alpha'", ValueError),
    ("UPDATE mercado_store_site_settings SET salesperson='missing' WHERE token_id=10", ValueError),
    ("UPDATE workbench_print_wallets SET organization_key='beta' WHERE user_id=2", wallet.WalletConflict),
])
def test_automatic_print_preserves_active_owner_store_and_wallet_organization_checks(store, monkeypatch, change, expected):
    recharge(store, '3')
    store.execute(change)
    before = store.rows('SELECT * FROM workbench_print_wallets')
    calls = []
    install_downloader(monkeypatch, store, lambda orders: calls.append(orders))
    with pytest.raises(expected):
        wallet.download_automatic_paid_labels(['1001'], 'automatic-request-001')
    assert calls == []
    assert store.rows('SELECT * FROM workbench_print_wallets') == before
    assert store.rows('SELECT * FROM workbench_print_requests') == []


def test_external_payload_cannot_select_system_identity_or_permissions(store):
    recharge(store, '3')
    for actor_id in (0, '0'):
        with pytest.raises(ValueError):
            store.run({'action': 'reserve', 'actor_id': actor_id, 'system': True,
                       'role_key': 'super_admin', 'permissions_json': ['*'],
                       'data': {'order_ids': ['1001'], 'request_id': 'automatic-request-001'}})
    with pytest.raises(PermissionError):
        store.run({'action': 'reserve', 'actor_id': 8, 'system': True,
                   'role_key': 'system', 'permissions_json': ['*'],
                   'data': {'order_ids': ['1001'], 'request_id': 'automatic-request-001'}})
    for action in ('recharge', 'cancel', 'list'):
        with pytest.raises(ValueError):
            store.run_auto(action, {})
    assert store('get')['balance_cents'] == 300
    assert store.rows('SELECT * FROM workbench_print_requests') == []


def test_automatic_settlement_failure_keeps_reservation_and_blocks_redownload(store, monkeypatch):
    recharge(store, '3')
    downloads, persisted = [], []

    def downloader(orders):
        downloads.append(orders)
        return {'content': b'%PDF-valid-test', 'order_ids': ['1001']}

    def persist(result):
        persisted.append(result['content'])
        store.fail_on('UPDATE workbench_print_requests')

    install_downloader(monkeypatch, store, downloader)
    with pytest.raises(wallet.WalletConflict, match='尚未确认'):
        wallet.download_automatic_paid_labels(['1001'], 'automatic-request-001', persist_result=persist)
    store.fail_on(None)
    assert persisted == [b'%PDF-valid-test']
    assert store.rows('SELECT status FROM workbench_print_requests')[0]['status'] == 'reserved'
    assert store('get')['balance_cents'] == 0
    with pytest.raises(PermissionError):
        settle(store, request_id='automatic-request-001', actor=3)
    with pytest.raises(wallet.WalletConflict):
        wallet.download_automatic_paid_labels(['1001'], 'automatic-request-001', persist_result=persist)
    assert downloads == [['1001']]
    result = store.run_auto('settle', {'request_id': 'automatic-request-001', 'successful_order_ids': ['1001']})
    assert result['charge_cents'] == 300
    assert store.run_auto('settle', {'request_id': 'automatic-request-001', 'successful_order_ids': ['1001']})['duplicate'] is True


@pytest.mark.parametrize('previous', ['reserved', 'settled', 'legacy_log'])
def test_automatic_reservation_rechecks_manual_print_after_candidate_scan(store, monkeypatch, previous):
    recharge(store, '9')
    if previous == 'legacy_log':
        store.execute("INSERT INTO mercado_order_operation_logs VALUES ('1001','label_printed')")
    else:
        reserve(store)
        if previous == 'settled':
            settle(store)
    before = store.rows('SELECT * FROM workbench_print_wallets')
    history = store('history')
    requests = store.rows('SELECT * FROM workbench_print_requests')
    calls = []
    install_downloader(monkeypatch, store, lambda orders: calls.append(orders))
    with pytest.raises(wallet.WalletConflict, match='自动任务不会重复扣费'):
        wallet.download_automatic_paid_labels(['1001', '1002'], 'automatic-request-001')
    assert calls == []
    assert store.rows('SELECT * FROM workbench_print_wallets') == before
    assert store.rows('SELECT * FROM workbench_print_requests') == requests
    assert store('history') == history
    # Manual reprints stay available; browser fields cannot opt into the
    # automatic guard or choose a system identity.
    assert reserve(store, request_id='manual-reprint-001', avoid_duplicate_orders=True)['reserved_cents'] == 300


@pytest.mark.parametrize('previous', ['refunded', 'cancelled'])
def test_automatic_can_retry_orders_with_confirmed_refund(store, monkeypatch, previous):
    recharge(store, '3')
    reserve(store)
    if previous == 'cancelled':
        store('cancel', actor=4, request_id='print-request-001', note='已确认没有打印')
    else:
        settle(store, successful=())
    install_downloader(monkeypatch, store, lambda orders: {'content': b'%PDF-valid-test', 'order_ids': ['1001']})
    result = wallet.download_automatic_paid_labels(['1001'], 'automatic-request-001')
    assert result['wallet_charge_cents'] == 300
    assert store('get')['balance_cents'] == 0


def test_automatic_checks_after_owner_locks_without_locking_other_requests(store):
    recharge(store, '3')
    assert not any(sql.startswith('SET TRANSACTION') for sql in store.events)
    store.events.clear()
    store.run_auto('reserve', {'request_id': 'automatic-request-001', 'order_ids': ['1001']})
    assert store.events[:3] == ['COMMIT', 'SET TRANSACTION ISOLATION LEVEL READ COMMITTED', 'BEGIN']
    owner_lock = next(index for index, sql in enumerate(store.events)
                      if 'FROM workbench_users WHERE id = %s FOR UPDATE' in sql)
    duplicate_check = next(index for index, sql in enumerate(store.events)
                           if 'SELECT details.order_id' in sql)
    assert owner_lock < duplicate_check
    assert 'FOR UPDATE' not in store.events[duplicate_check]
    assert 'FOR UPDATE' not in next(sql for sql in store.events if 'SELECT order_id FROM mercado_order_operation_logs' in sql)
    assert store.events[-1] == 'COMMIT'
    assert store.events.count('COMMIT') == 2


def test_automatic_finishes_schema_transaction_before_setting_accounting_isolation(store, monkeypatch):
    recharge(store, '3')

    def schema_inspection(cursor):
        # Model a pooled connection/schema read already inside a transaction;
        # the adapter rejects SET TRANSACTION until that phase is committed.
        cursor.execute('BEGIN')
        cursor.execute('SELECT COUNT(*) FROM workbench_print_wallet_records')

    monkeypatch.setattr(wallet, 'ensure_tables', schema_inspection)
    install_downloader(monkeypatch, store,
        lambda orders: {'content': b'%PDF-valid-test', 'order_ids': ['1001']})
    store.events.clear()
    result = wallet.download_automatic_paid_labels(['1001'], 'automatic-request-001')
    assert result['wallet_charge_cents'] == 300
    assert store.events[:5] == ['BEGIN', 'SELECT COUNT(*) FROM workbench_print_wallet_records',
                               'COMMIT', 'SET TRANSACTION ISOLATION LEVEL READ COMMITTED', 'BEGIN']
    assert store.rows('SELECT balance_cents FROM workbench_print_wallets WHERE user_id=2')[0]['balance_cents'] == 0
    assert store.rows('SELECT status FROM workbench_print_requests')[0]['status'] == 'settled'


@pytest.mark.parametrize('binding', [None, 'dongguan'])
def test_warehouse_reservation_checks_binding_without_charging(store, binding):
    recharge(store, '9')
    store.execute('UPDATE workbench_users SET warehouse_key=? WHERE id=3', (binding,))
    with pytest.raises(PermissionError, match='绑定仓库'):
        reserve(store)
    assert store('get', actor=2)['balance_cents'] == 900
    assert store.rows('SELECT * FROM workbench_print_requests') == []


def test_warehouse_print_history_only_contains_current_warehouse_orders(store):
    recharge(store, '9')
    reserve(store, orders=('1001', '1002'))
    settle(store, successful=('1001', '1002'))
    store.execute("UPDATE mercado_synced_orders SET transfer_warehouse='yiwu' WHERE order_id='1002'")
    records = store('records', actor=3)
    assert [r['order_id'] for r in records['records'] if r['kind'] == 'print'] == ['1001']
    assert records['total'] == 2  # Recharge entry plus the remaining warehouse order.
    assert [r['order_id'] for r in store('history', actor=3) if r['kind'] == 'print'] == ['1001']
    assert len([r for r in store('history', actor=1) if r['kind'] == 'print']) == 2
