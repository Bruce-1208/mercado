"""Ensure pagination and source filters survive split-service request forwarding."""
from bit import bit_db_api, bit_interface


def test_public_knowledge_listing_forwards_page_and_source(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_interface, 'get_current_workbench_user', lambda: {
        'id': 1, 'username': 'reviewer', 'permissions': ['infringement_knowledge.view'], 'access_version': 1,
    })
    monkeypatch.setattr(bit_interface, 'db_list_infringement_knowledge',
                        lambda **kwargs: calls.append(kwargs) or {'rows': [], 'filtered_total': 51, 'pages': 2})
    response = bit_interface.app.test_client().get(
        '/api/infringement-knowledge?list_type=pending&source_type=wipo&search=Brand&page=2&page_size=50')
    assert response.status_code == 200
    assert calls == [{'list_type': 'pending', 'source_type': 'wipo', 'search': 'Brand',
                      'limit': 2000, 'page': '2', 'page_size': '50'}]
    assert response.get_json()['data']['filtered_total'] == 51


def test_internal_knowledge_listing_preserves_filters(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_interface, 'USE_DB_API', False)
    monkeypatch.setenv('BIT_DB_API_TOKEN', 'knowledge-test-token')
    monkeypatch.setattr(bit_interface, 'db_list_infringement_knowledge',
                        lambda **kwargs: calls.append(kwargs) or {'rows': []})
    response = bit_interface.app.test_client().get(
        '/api/db/infringement-knowledge?source_type=wikidata&page=2&page_size=50',
        headers={'X-Internal-Token': 'knowledge-test-token'},
        environ_overrides={'REMOTE_ADDR': '10.0.0.20'})
    assert response.status_code == 200
    assert calls[0]['source_type'] == 'wikidata'
    assert calls[0]['page'] == '2'
    assert calls[0]['page_size'] == '50'


def test_db_client_page_mode_and_cursor_mode_remain_distinct(monkeypatch):
    calls = []
    monkeypatch.setattr(bit_db_api, 'DB_MODE', 'api')
    monkeypatch.setattr(bit_db_api, '_request', lambda *args, **kwargs: calls.append(kwargs) or {})
    bit_db_api.list_infringement_knowledge(list_type='pending', source_type='wipo', page=2, page_size=50)
    bit_db_api.list_infringement_knowledge(limit=5000, after_id=10)
    assert calls[0]['params'] == {'list_type': 'pending', 'source_type': 'wipo', 'search': '',
                                 'limit': 2000, 'page': 2, 'page_size': 50}
    assert calls[1]['params'] == {'list_type': '', 'search': '', 'limit': 5000, 'after_id': 10}
