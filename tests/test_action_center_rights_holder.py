from flask import Flask

from bit import mercado_action_center as module


def test_rights_cases_empty_scope_does_not_query():
    assert module._rights_holder_replies(object(), []) == []


def test_rights_cases_read_all_pages_in_deadline_order():
    class Store:
        def list_mercado_prohibited_listings(self, **kwargs):
            assert kwargs['token_ids'] == [7]
            assert kwargs['risk_type'] == 'rights_holder_reply'
            return {'total': 2, 'rows': [{'due_at': '2026-10-02' if kwargs['page'] == 1 else '2026-10-01'}]}
    rows = module._rights_holder_replies(Store(), [7])
    assert [row['due_at'] for row in rows] == ['2026-10-01', '2026-10-02']


def test_today_rights_cases_use_authorized_organization_scope(monkeypatch):
    monkeypatch.setattr(module, '_refresh_order_watch_tasks', lambda *args: None)
    class Store:
        def list_mercado_store_tokens(self):
            return {'rows': [{'id': 7, 'organization_key': 'alpha'},
                             {'id': 8, 'organization_key': 'beta'},
                             {'id': 9, 'organization_key': 'alpha'}]}
        def list_mercado_operator_tasks(self, **kwargs):
            return {'rows': []}
        def list_mercado_notification_events(self, **kwargs):
            return {'rows': []}
        def list_mercado_prohibited_listings(self, **kwargs):
            assert kwargs['token_ids'] == [7]
            return {'total': 1, 'rows': [{'infraction_id': 'case-7'}]}
    app = Flask(__name__)
    app.register_blueprint(module.create_action_center_blueprint(
        login_required=lambda fn: fn,
        current_user=lambda: {'organization_key': 'alpha'},
        authorized_token_ids=lambda user: [7, 8], storage=Store(),
    ))
    response = app.test_client().get('/api/mercado/today/tasks')
    assert response.status_code == 200
    assert response.json['data']['rights_holder_replies'] == [{'infraction_id': 'case-7'}]


def test_recorded_reply_attempts_stay_out_of_inbox_after_reload(monkeypatch, tmp_path):
    import json
    from bit import bit_runtime_lock
    monkeypatch.setattr(bit_runtime_lock, 'RUNTIME_LOCK_DIR', tmp_path)
    for case_id, state in [('1', 'accepted'), ('2', 'unknown'), ('3', 'sending'), ('4', 'confirmed')]:
        (tmp_path / f'rights-reply-7-{case_id}.json').write_text(json.dumps({'state': state}))
    class Store:
        def list_mercado_prohibited_listings(self, **kwargs):
            return {'total': 5, 'rows': [
                {'token_id': 7, 'infraction_id': str(i)} for i in range(1, 6)
            ]}
    assert [row['infraction_id'] for row in module._rights_holder_replies(Store(), [7])] == ['5']
    assert len(module._rights_holder_replies(Store(), [7], include_replied=True)) == 5
