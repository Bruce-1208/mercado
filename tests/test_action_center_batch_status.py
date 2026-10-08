from flask import Flask
from bit import mercado_action_center as module


class Store:
    def __init__(self):
        self.tasks = {1: dict(id=1, token_id=7, organization_key='org', topic='claims', status='open'),
                      2: dict(id=2, token_id=8, organization_key='other', status='open')}
        self.writes = []
    def list_mercado_store_tokens(self):
        return {'rows': [dict(id=7, organization_key='org'), dict(id=8, organization_key='other')]}
    def list_mercado_operator_tasks(self, **kwargs):
        return {'rows': [t for t in self.tasks.values() if t['token_id'] in kwargs['token_ids']]}
    def list_mercado_today_work_states(self, **kwargs):
        return [t for t in self.tasks.values() if t['token_id'] in kwargs['token_ids'] and t.get('source') == 'today_work_status']
    def list_mercado_notification_events(self, **kwargs):
        return {'rows': [dict(id=10, token_id=7, status='done')]}
    def list_mercado_prohibited_listings(self, **kwargs):
        return {'rows': [dict(token_id=7, infraction_id='case')], 'total': 1}
    def get_mercado_operator_task(self, task_id):
        return self.tasks.get(task_id)
    def upsert_mercado_operator_task(self, row):
        for task in self.tasks.values():
            if task.get('task_key') == row['task_key']:
                return task['id']
        task_id = len(self.tasks)+1
        self.tasks[task_id] = dict(row, id=task_id, status='open')
        return task_id
    def update_mercado_operator_task(self, task_id, **kwargs):
        self.writes.append(task_id)
        self.tasks[task_id]['status'] = kwargs['status']
        return self.tasks[task_id]


def setup(monkeypatch):
    store = Store()
    allowed = [7]
    permission = [True]
    monkeypatch.setattr(module, '_refresh_order_watch_tasks', lambda *args: (set(), set()))
    app = Flask(__name__)
    app.secret_key = 'test-only'
    app.register_blueprint(module.create_action_center_blueprint(
        login_required=lambda fn: fn, current_user=lambda: {'organization_key': 'org'},
        authorized_token_ids=lambda user: allowed, storage=store,
        has_permission=lambda user, key: permission[0]))
    return app.test_client(), store, allowed, permission


def test_batch_validates_entire_scope_before_writes(monkeypatch):
    client, store, _, permission = setup(monkeypatch)
    url = '/api/mercado/today/batch-status'
    assert client.patch(url, json={'items':[{'id':1},{'id':2}], 'status':'resolved'}).status_code == 403
    assert not store.writes
    assert client.patch(url, json={'items':[{'id':1}], 'status':'wrong'}).status_code == 400
    assert client.patch(url, json={'items':[{'id':1}], 'status':'resolved'}).json['data']['results'][0]['task']['status'] == 'resolved'
    permission[0] = False
    assert client.patch(url, json={'items':[{'id':1}], 'status':'open'}).status_code == 403


def test_external_status_persists_without_changing_platform_state(monkeypatch):
    client, store, allowed, _ = setup(monkeypatch)
    data = client.get('/api/mercado/today/tasks').json['data']
    for field in ['rights_holder_replies', 'events']:
        ref = data[field][0]['work_ref']
        assert client.patch('/api/mercado/today/batch-status', json={'items':[{'ref':ref}], 'status':'resolved'}).json['data']['errors'] == []
    reread = client.get('/api/mercado/today/tasks').json['data']
    assert reread['rights_holder_replies'][0]['work_status'] == 'resolved'
    assert reread['events'][0]['work_status'] == 'resolved'
    assert reread['events'][0]['status'] == 'done'
    assert len(reread['rows']) == 1  # No duplicate local handling tasks in operations.
    ref = data['rights_holder_replies'][0]['work_ref']
    assert client.patch('/api/mercado/today/batch-status', json={'items':[{'ref':ref+'tampered'}], 'status':'open'}).status_code == 400
    allowed.clear()
    assert client.patch('/api/mercado/today/batch-status', json={'items':[{'ref':ref}], 'status':'open'}).status_code == 403


def test_new_buyer_message_reopens_handling(monkeypatch):
    client, _, _, _ = setup(monkeypatch)
    stamp = ['2026-10-09T01:00:00Z']
    monkeypatch.setattr(module, '_unanswered_post_sale', lambda *args: {'rows':[dict(token_id=7, pack_id='P',latest_at=stamp[0])], 'errors':[]})
    row = client.get('/api/mercado/today/post-sale').json['data']['rows'][0]
    client.patch('/api/mercado/today/batch-status', json={'items':[{'ref':row['work_ref']}], 'status':'resolved'})
    assert client.get('/api/mercado/today/post-sale').json['data']['rows'][0]['work_status'] == 'resolved'
    stamp[0] = '2026-10-09T02:00:00Z'
    assert client.get('/api/mercado/today/post-sale?refresh=1').json['data']['rows'][0]['work_status'] == 'open'


def test_batch_reports_partial_failure(monkeypatch):
    client, store, _, _ = setup(monkeypatch)
    store.tasks[3] = dict(store.tasks[1], id=3)
    original = store.update_mercado_operator_task
    def update(task_id, **kwargs):
        if task_id == 3:
            raise ValueError('offline')
        return original(task_id, **kwargs)
    store.update_mercado_operator_task = update
    result = client.patch('/api/mercado/today/batch-status', json={'items':[{'id':1},{'id':3}], 'status':'in_progress'}).json['data']
    assert len(result['results']) == len(result['errors']) == 1
    assert result['errors'][0]['item'] == {'id':3}
