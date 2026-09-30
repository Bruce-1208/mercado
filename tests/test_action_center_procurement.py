from datetime import datetime, timedelta
from flask import Flask
from bit import mercado_action_center as module


class WatchStore:
    def __init__(self, rows):
        self.rows, self.tasks, self.resolved = rows, [], []
    def list_mercado_action_center_orders(self, **kwargs):
        return self.rows
    def upsert_mercado_operator_task(self, task):
        self.tasks.append(task)
    def resolve_mercado_operator_tasks_by_keys(self, keys, reason):
        self.resolved.extend(keys)


def test_procurement_thresholds_and_completion():
    now = datetime(2026, 9, 29, 12)
    for hours, expected in [(24, set()), (25, {'purchase_order_missing_24h'}),
                            (48, {'purchase_order_missing_24h'}),
                            (49, {'purchase_order_missing_24h', 'purchase_tracking_missing_48h'}),
                            (169, {'purchase_order_missing_24h', 'purchase_tracking_missing_48h', 'order_not_in_transit_7d'})]:
        row = dict(token_id=1, order_id='123', status='paid', date_created=now-timedelta(hours=hours))
        store = WatchStore([row])
        module._refresh_order_watch_tasks(store, [1], {1: 'org'}, now=now)
        assert {t['topic'] for t in store.tasks} == expected
    for updates in [dict(status='cancelled'), dict(workflow_status='已发-在途中'), dict(workflow_status='交付')]:
        store = WatchStore([{**row, **updates}])
        module._refresh_order_watch_tasks(store, [1], {1: 'org'}, now=now)
        assert not store.tasks
        assert len(store.resolved) == 3
    store = WatchStore([{**row, 'purchase_order': 'P123', 'purchase_tracking': 'T123'}])
    module._refresh_order_watch_tasks(store, [1], {1: 'org'}, now=now)
    assert {t['topic'] for t in store.tasks} == {'order_not_in_transit_7d'}
    assert len(store.resolved) == 2


def test_filters_intersect_authorized_store_scope(monkeypatch):
    monkeypatch.setattr(module, '_refresh_order_watch_tasks', lambda *args: None)
    class Store:
        def list_mercado_store_tokens(self):
            return {'rows': [dict(id=i, organization_key='org', site_settings=[dict(salesperson=name, group_name=group)]) for i, name, group in [(1,'甲','A'),(2,'乙','B'),(3,'甲','A')]]}
        def list_mercado_operator_tasks(self, **kwargs):
            assert kwargs['token_ids'] == [1]
            return {'rows': []}
        def list_mercado_notification_events(self, **kwargs):
            assert kwargs['token_ids'] == [1]
            return {'rows': []}
    monkeypatch.setattr(module, '_rights_holder_replies', lambda store, ids: [])
    app = Flask(__name__)
    app.register_blueprint(module.create_action_center_blueprint(login_required=lambda fn: fn,
        current_user=lambda: {'organization_key': 'org'}, authorized_token_ids=lambda user: [1,2], storage=Store()))
    response = app.test_client().get('/api/mercado/today/tasks?salesperson=甲&group_name=A')
    assert response.status_code == 200
    assert response.json['data']['filter_options'] == {'salesperson':['乙','甲'], 'group_name':['A','B']}


def test_purchase_note_appends_order_remark_and_rolls_back_on_overflow(monkeypatch):
    from unittest.mock import MagicMock
    import pytest
    from bit import bit_mysql
    for previous, fails in [('原订单备注', False), ('字' * 5000, True)]:
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [dict(id=1, token_id=7, resource='123', topic='purchase_order_missing_24h'), {'order_remark': previous}]
        monkeypatch.setattr(bit_mysql.pymysql, 'connect', lambda **kwargs: connection)
        monkeypatch.setattr(bit_mysql, '_ensure_mercado_action_center_tables', lambda cursor: None)
        monkeypatch.setattr(bit_mysql, 'get_mercado_operator_task', lambda task_id: {'id': task_id})
        if fails:
            with pytest.raises(ValueError, match='5000'):
                bit_mysql.update_mercado_operator_task(1, actor='甲', note='已催供应商')
            connection.rollback.assert_called_once()
            connection.commit.assert_not_called()
        else:
            bit_mysql.update_mercado_operator_task(1, actor='甲', note='已催供应商')
            writes = [call.args for call in cursor.execute.call_args_list if 'UPDATE `mercado_synced_orders`' in call.args[0]]
            assert len(writes) == 1
            sql, params = writes[0]
            assert '`order_remark`' in sql and 'purchase_remark' not in sql
            assert params[0].startswith('原订单备注\n') and '已催供应商' in params[0]
            assert params[1:] == (7, '123')
            connection.commit.assert_called_once()
