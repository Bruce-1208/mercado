import json
import pytest
from erp import ai_original_products as products
from erp import ai_original_category_catalog as catalog


@pytest.fixture
def taxonomy(tmp_path, monkeypatch):
    path = tmp_path / 'CBT.json'
    root = {'id': 'CBT1', 'name': 'Computers'}
    path.write_text(json.dumps({'expires_at': 0, 'paths': {
        'CBT1': [root],
        'CBT2': [root, {'id': 'CBT2', 'name': 'Keyboard Accessories'}],
        'CBT3': [root, {'id': 'CBT3', 'name': 'Keyboards'}],
        'MLM4': [{'id': 'MLM4', 'name': 'Wrong site'}],
    }}))
    monkeypatch.setattr(catalog, 'CATALOG_PATH', path)


@pytest.mark.parametrize('failure', ['error', 'empty', 'invalid', 'refined_error', 'forbidden'])
def test_discovery_fallback_continues_live_attributes(taxonomy, failure):
    calls = []
    selected = []
    def chat(messages, **kwargs):
        prompt = messages[0]['content']
        if '已知的跨境 CBT' in prompt:
            data = json.loads(prompt.split('\n', 1)[1])
            ids = [row['id'] for row in data['candidates']]
            assert all(i.startswith('CBT') for i in ids)
            if ids == ['CBT1']:
                assert data['candidates'][0]['includes'] == ['Keyboard Accessories', 'Keyboards']
            return json.dumps({'category_id': 'CBT2' if 'CBT2' in ids else 'CBT1'})
        if '选择与原始商品实体' in prompt:
            return '{"category_id":"", "search_query":"keyboard accessories"}'
        return '{"attributes":[{"id":"MATERIAL","value_name":"Plastic"}]}'
    class Client:
        def request(self, method, path, **kwargs):
            calls.append(path)
            if 'domain_discovery' in path:
                if failure == 'empty': return []
                if failure == 'invalid': return {'error': 'invalid response'}
                if failure == 'refined_error' and len(calls) == 1:
                    return [{'category_id': 'CBT8', 'category_name': 'Other'},
                            {'category_id': 'CBT9', 'category_name': 'Parts'}]
                error = RuntimeError('discovery unavailable')
                if failure == 'forbidden':
                    error.status_code = 403
                raise error
            assert path == '/categories/CBT2/attributes'
            return [{'id': 'MATERIAL', 'name': 'Material', 'value_type': 'string', 'tags': {}}]
    result = products.complete_marketplace_category(
        {'title': '键帽', 'properties': {'material': 'Plastic'}},
        {'product_type_en': 'keycaps'}, Client(), chat=chat, on_category_ready=selected.append)
    assert result['category_id'] == 'CBT2'
    assert result['attributes'][0]['value_name'] == 'Plastic'
    assert selected[0]['category_selection_source'] == 'known_cbt_catalog'
    assert calls[-1] == '/categories/CBT2/attributes'


def test_rejects_invented_ids(taxonomy):
    with pytest.raises(ValueError, match='有效分类'):
        catalog.select_known_cbt_category({'title': 'keys'}, 'keys',
                                         lambda *a, **k: '{"category_id":"CBT999"}')


def test_model_account_failure_is_not_swallowed(taxonomy):
    def chat(*args, **kwargs):
        raise products.OriginalModelAccountError('AI account unavailable')
    with pytest.raises(products.OriginalModelAccountError):
        products._select_marketplace_category({}, 'keys', [], object(), chat=chat)
