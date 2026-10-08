from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

from bit import mercado_action_center as module


NOW = datetime(2026, 10, 7, 12)


class AlertStore:
    def __init__(self, rows):
        self.rows = rows
        self.tasks = {}
        self.resolved = []
        self.pages = []

    def list_mercado_action_center_orders(self, *, token_ids, limit, offset=0):
        self.pages.append(offset)
        return [row for row in self.rows if row['token_id'] in token_ids][offset:offset + limit]

    def upsert_mercado_operator_task(self, task):
        self.tasks.setdefault(task['task_key'], {**task, 'id': len(self.tasks) + 1, 'status': 'open'})

    def resolve_mercado_operator_tasks_by_keys(self, keys, reason):
        self.resolved.extend(keys)
        for key in keys:
            if key in self.tasks:
                self.tasks[key]['status'] = 'resolved'


def order(age, **changes):
    return {
        'token_id': 7, 'order_id': '123', 'status': 'paid',
        'date_created': NOW - age - timedelta(hours=8), 'last_updated': NOW - timedelta(hours=8),
        'purchase_order': 'P123', 'purchase_tracking': 'T123', **changes,
    }


def refresh(store, now=NOW):
    return module._refresh_order_watch_tasks(store, [7], {7: 'org'}, now=now)


@pytest.mark.parametrize('age,changes,expected', [
    (timedelta(hours=48) - timedelta(seconds=1), {'purchase_tracking': '  '}, set()),
    (timedelta(hours=48), {'purchase_tracking': '  '}, {'purchase_tracking_missing_48h'}),
    (timedelta(hours=168) - timedelta(seconds=1), {}, set()),
    (timedelta(hours=168), {}, {'order_not_in_transit_7d'}),
    (timedelta(days=30) - timedelta(seconds=1), {'status': 'shipped'}, set()),
    (timedelta(days=30), {'status': 'shipped'}, {'order_not_delivered_30d'}),
    (timedelta(days=90), {'status': 'shipped'}, {'order_not_delivered_30d'}),
    (timedelta(days=90), {}, {'order_not_in_transit_7d', 'order_not_delivered_30d'}),
])
def test_alert_thresholds_include_exact_deadline_and_orders_older_than_30_days(age, changes, expected):
    store = AlertStore([order(age, **changes)])
    recent, watched = refresh(store)
    assert {task['topic'] for task in store.tasks.values()} == expected
    assert (7, '123') in watched
    assert ((7, '123') in recent) == (age <= timedelta(days=30))
    deadlines = {
        'purchase_tracking_missing_48h': timedelta(hours=48),
        'order_not_in_transit_7d': timedelta(hours=168),
        'order_not_delivered_30d': timedelta(days=30),
    }
    for task in store.tasks.values():
        assert task['due_at'] == NOW - age + deadlines[task['topic']]
        assert task['organization_key'] == 'org'


@pytest.mark.parametrize('changes', [
    {'status': 'delivered'}, {'workflow_status': '交付'},
    {'raw': {'_shipment_status': 'delivered'}}, {'raw': {'shipping': {'status': 'delivered'}}},
    {'status': 'cancelled'}, {'workflow_status': '取消-发货后'},
    {'raw': {'_shipment_status': 'cancelled'}},
])
def test_delivery_or_cancellation_resolves_existing_old_alerts(changes):
    store = AlertStore([order(timedelta(days=45), purchase_tracking='')])
    refresh(store)
    assert {task['topic'] for task in store.tasks.values()} == {
        'purchase_tracking_missing_48h', 'order_not_in_transit_7d', 'order_not_delivered_30d',
    }
    store.rows[0].update(changes)
    refresh(store)
    assert all(task['status'] == 'resolved' for task in store.tasks.values())


@pytest.mark.parametrize('changes', [
    {'workflow_status': '已入库'}, {'status': 'not_delivered'},
    {'status': 'partially_refunded'},
    {'status': 'shipped', 'workflow_status': '已发-异常'},
    {'workflow_status': '问题-物流退件', 'raw': {'_shipment_status': 'shipped'}},
])
def test_warehouse_receipt_and_delivery_failure_do_not_hide_unfulfilled_alerts(changes):
    store = AlertStore([order(timedelta(days=31), **changes)])
    refresh(store)
    assert {task['topic'] for task in store.tasks.values()} == {
        'order_not_in_transit_7d', 'order_not_delivered_30d',
    }


def test_tracking_and_transit_resolve_only_the_corresponding_alerts():
    store = AlertStore([order(timedelta(days=45), purchase_tracking='')])
    refresh(store)
    store.rows[0].update(purchase_tracking='T123', workflow_status='已发-在途中')
    refresh(store)
    assert {task['topic'] for task in store.tasks.values() if task['status'] == 'open'} == {
        'order_not_delivered_30d',
    }
    assert len(store.tasks) == 3


def test_transit_does_not_hide_missing_tracking_and_order_updates_do_not_reset_clock():
    store = AlertStore([order(timedelta(hours=48), status='shipped', purchase_tracking='')])
    refresh(store)
    assert {task['topic'] for task in store.tasks.values()} == {'purchase_tracking_missing_48h'}


def test_scan_paginates_all_authorized_orders_and_resolves_in_bounded_batches():
    rows = [order(timedelta(days=31), order_id=str(i)) for i in range(1001)]
    rows.append(order(timedelta(days=31), token_id=8, order_id='outside-scope'))
    store = AlertStore(rows)
    refresh(store)
    assert store.pages == [0, 1000]
    assert len(store.tasks) == 2002
    assert all(task['token_id'] == 7 for task in store.tasks.values())
    store.rows = [{**row, 'status': 'delivered'} for row in rows]
    refresh(store)
    assert all(task['status'] == 'resolved' for task in store.tasks.values())


def test_threshold_clock_normalizes_utc_dates_to_beijing_time():
    store = AlertStore([order(timedelta(hours=48), purchase_tracking='', date_created='2026-10-05T04:00:00Z')])
    refresh(store, now=NOW.replace(tzinfo=timezone(timedelta(hours=8))))
    task = next(iter(store.tasks.values()))
    assert task['topic'] == 'purchase_tracking_missing_48h'
    assert task['due_at'] == NOW


def test_procurement_tracking_checks_keep_their_local_timestamp_clock():
    store = AlertStore([order(
        timedelta(days=3), tracking={'status': 'not_found'},
        tracking_checked_at=NOW - timedelta(hours=48),
    )])
    refresh(store)
    task = next(iter(store.tasks.values()))
    assert task['topic'] == 'procurement_tracking_stalled'
    assert task['due_at'] == NOW


@pytest.mark.parametrize('threshold,changes,topic', [
    (timedelta(hours=48), {'purchase_tracking': ''}, 'purchase_tracking_missing_48h'),
    (timedelta(hours=168), {}, 'order_not_in_transit_7d'),
    (timedelta(days=30), {'status': 'shipped'}, 'order_not_delivered_30d'),
])
def test_real_database_utc_timestamps_do_not_trigger_eight_hours_early(threshold, changes, topic):
    from bit import bit_mysql
    created = (NOW - timedelta(hours=8) - threshold + timedelta(hours=6)).replace(tzinfo=timezone.utc)
    stored = bit_mysql._mercado_order_datetime(created.isoformat())
    store = AlertStore([order(threshold, date_created=stored, **changes)])
    refresh(store)
    assert topic not in {task['topic'] for task in store.tasks.values()}
    refresh(store, NOW + timedelta(hours=6))
    task = next(task for task in store.tasks.values() if task['topic'] == topic)
    assert task['due_at'] == NOW + timedelta(hours=6)
    assert '北京时间' in task['details']


@pytest.mark.parametrize('threshold,changes,topic,source', [
    (timedelta(hours=48), {'purchase_tracking': ''}, 'purchase_tracking_missing_48h', 'procurement_watch'),
    (timedelta(hours=168), {}, 'order_not_in_transit_7d', 'order_age_watch'),
    (timedelta(days=30), {'status': 'shipped'}, 'order_not_delivered_30d', 'order_age_watch'),
])
def test_preexisting_alerts_from_the_old_clock_are_resolved_until_the_real_threshold(threshold, changes, topic, source):
    store = AlertStore([order(threshold - timedelta(hours=6), **changes)])
    key = module._task_key(source, 'org', 7, topic, '123')
    store.tasks[key] = {'task_key': key, 'topic': topic, 'status': 'open', 'due_at': NOW - timedelta(hours=2)}
    refresh(store)
    assert store.tasks[key]['status'] == 'resolved'
    assert key in store.resolved


def test_tasks_endpoint_keeps_old_fulfillment_alerts_but_limits_order_notifications(monkeypatch):
    class ScopedStore(AlertStore):
        def list_mercado_store_tokens(self):
            return {'rows': [{'id': 7, 'organization_key': 'org'}]}

        def list_mercado_operator_tasks(self, **kwargs):
            assert kwargs['token_ids'] == [7]
            return {'rows': list(self.tasks.values()) + [
                {'token_id': 7, 'topic': 'marketplace_orders', 'resource': '/orders/123', 'status': 'open'},
                {'token_id': 7, 'topic': 'marketplace_orders', 'resource': '/orders/recent', 'status': 'open'},
            ]}

        def list_mercado_notification_events(self, **kwargs):
            return {'rows': []}

    store = ScopedStore([order(timedelta(days=45)), order(timedelta(hours=1), order_id='recent')])
    original = module._refresh_order_watch_tasks
    monkeypatch.setattr(module, '_refresh_order_watch_tasks', lambda *args: original(*args, now=NOW))
    monkeypatch.setattr(module, '_rights_holder_replies', lambda *args: [])
    app = Flask(__name__)
    app.register_blueprint(module.create_action_center_blueprint(
        login_required=lambda fn: fn, current_user=lambda: {'organization_key': 'org'},
        authorized_token_ids=lambda user: [7], storage=store,
    ))
    response = app.test_client().get('/api/mercado/today/tasks')
    assert response.status_code == 200
    rows = response.json['data']['rows']
    assert {row['topic'] for row in rows} == {
        'order_not_in_transit_7d', 'order_not_delivered_30d', 'marketplace_orders',
    }
    assert [row['resource'] for row in rows if row['topic'] == 'marketplace_orders'] == ['/orders/recent']
    assert response.json['data']['total'] == 3


def test_tasks_endpoint_reads_all_pages_before_filtering_old_notifications(monkeypatch):
    class ScopedStore(AlertStore):
        def list_mercado_store_tokens(self):
            return {'rows': [{'id': 7, 'organization_key': 'org'}]}

        def list_mercado_operator_tasks(self, *, token_ids, organization_key, include_closed, limit, offset):
            assert token_ids == [7] and organization_key == 'org'
            self.task_pages.append(offset)
            notifications = [
                {'token_id': 7, 'topic': 'marketplace_orders', 'resource': f'/orders/old-{i}', 'status': 'open'}
                for i in range(1001)
            ]
            rows = notifications + list(self.tasks.values())
            return {'rows': rows[offset:offset + limit]}

        def list_mercado_notification_events(self, **kwargs):
            return {'rows': []}

    store = ScopedStore([order(timedelta(days=45))])
    store.task_pages = []
    original = module._refresh_order_watch_tasks
    monkeypatch.setattr(module, '_refresh_order_watch_tasks', lambda *args: original(*args, now=NOW))
    monkeypatch.setattr(module, '_rights_holder_replies', lambda *args: [])
    app = Flask(__name__)
    app.register_blueprint(module.create_action_center_blueprint(
        login_required=lambda fn: fn, current_user=lambda: {'organization_key': 'org'},
        authorized_token_ids=lambda user: [7], storage=store,
    ))
    response = app.test_client().get('/api/mercado/today/tasks')
    assert response.status_code == 200
    assert store.task_pages == [0, 1000]
    assert {row['topic'] for row in response.json['data']['rows']} == {
        'order_not_in_transit_7d', 'order_not_delivered_30d',
    }
    assert response.json['data']['total'] == response.json['data']['open'] == 2
