from flask import Flask
from bit import mercado_action_center as module


class Store:
    def list_mercado_message_pack_candidates(self, token_id):
        return ['read', 'replied', 'paged']

    def execute_mercado_store_communication(self, token_id, action, data):
        if action == 'post-sale-unread':
            return {'results': [{'resource': '/packs/new'}, {'resource': '/packs/replied'}]}
        pack = data['pack_id']
        outgoing = pack == 'replied' or (pack == 'paged' and data['offset'] == 1)
        return {'seller_id': '77', 'paging': {'total': 2 if pack == 'paged' else 1},
                'messages': [{'from': {'user_id': '77' if outgoing else '88'},
                              'message_date': {'created': '2026-01-02T00:00:00Z' if outgoing else '2026-01-01T00:00:00Z',
                                               'read': '2026-01-01T00:01:00Z'}, 'text': pack}]}


def test_unanswered_includes_read_and_old_messages_excludes_replied_across_pages():
    result = module._unanswered_post_sale(Store(), [{'id': 7}])
    assert {row['pack_id'] for row in result['rows']} == {'read', 'new'}
    assert result['errors'] == []


def test_failure_is_not_reported_as_empty_success():
    class Broken(Store):
        def execute_mercado_store_communication(self, *args):
            raise ValueError('unavailable')
    result = module._unanswered_post_sale(Broken(), [{'id': 7}])
    assert result['rows'] == []
    assert 'unavailable' in result['errors'][0]


def test_post_sale_endpoint_applies_scope_and_filters(monkeypatch):
    class Scoped(Store):
        def list_mercado_store_tokens(self):
            return {'rows': [{'id': 7, 'organization_key': 'alpha', 'salesperson': 'A'},
                             {'id': 8, 'organization_key': 'beta', 'salesperson': 'A'},
                             {'id': 9, 'organization_key': 'alpha', 'salesperson': 'B'}]}
    seen = []
    monkeypatch.setattr(module, '_unanswered_post_sale', lambda store, tokens: seen.extend(tokens) or {'rows': [], 'errors': []})
    app = Flask(__name__)
    app.register_blueprint(module.create_action_center_blueprint(
        login_required=lambda fn: fn, current_user=lambda: {'organization_key': 'alpha'},
        authorized_token_ids=lambda user: [7, 8, 9], storage=Scoped()))
    response = app.test_client().get('/api/mercado/today/post-sale?salesperson=A')
    assert response.status_code == 200
    assert [row['id'] for row in seen] == [7]


def test_conversations_are_loaded_concurrently_with_a_fixed_limit():
    import threading

    barrier = threading.Barrier(6)
    lock = threading.Lock()
    active = 0
    peak = 0

    class SlowStore(Store):
        def list_mercado_message_pack_candidates(self, token_id):
            return [str(index) for index in range(12)]

        def execute_mercado_store_communication(self, token_id, action, data):
            nonlocal active, peak
            if action == 'post-sale-unread':
                return {}
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=5)
                return super().execute_mercado_store_communication(token_id, action, data)
            finally:
                with lock:
                    active -= 1

    result = module._unanswered_post_sale(SlowStore(), [{'id': 7}])
    assert result['errors'] == []
    assert len(result['rows']) == 12
    assert peak == 6


def test_one_conversation_failure_preserves_other_results():
    class Partial(Store):
        def execute_mercado_store_communication(self, token_id, action, data):
            if data.get('pack_id') == 'read':
                raise ValueError('conversation unavailable')
            return super().execute_mercado_store_communication(token_id, action, data)

    result = module._unanswered_post_sale(Partial(), [{'id': 7}])
    assert [row['pack_id'] for row in result['rows']] == ['new']
    assert len(result['errors']) == 1
    assert 'conversation unavailable' in result['errors'][0]
