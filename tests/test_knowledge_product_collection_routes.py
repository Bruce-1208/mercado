import pytest
from bit import bit_interface
from bit import knowledge_product_collection as collect

pytestmark = pytest.mark.usefixtures('subscribed_workbench_account')


def authorize(monkeypatch, permissions):
    monkeypatch.setattr(bit_interface, 'get_current_workbench_user', lambda: {
        'id': 11, 'username': 'collector', 'organization_key': 'org-a',
        'permissions': permissions, 'access_version': 1,
    })
    monkeypatch.setattr(bit_interface, 'workbench_selected_organization', lambda user: 'org-a')


def test_open_confirm_and_stop_are_scoped_and_always_require_manual_confirmation(monkeypatch):
    authorize(monkeypatch, ['infringement_knowledge.view', 'infringement_knowledge.manage'])
    calls = []
    monkeypatch.setattr(collect.manager, 'start', lambda owner, source: calls.append(('open', owner, source)) or {'phase': 'waiting_login'})
    monkeypatch.setattr(collect.manager, 'confirm', lambda owner: calls.append(('confirm', owner)) or {'phase': 'collecting'})
    monkeypatch.setattr(collect.manager, 'stop', lambda owner: calls.append(('stop', owner)) or {'phase': 'stopping'})
    client = bit_interface.app.test_client()
    for action, payload in [('open', {'source': 'lanjing', 'require_login': False}), ('confirm-login', {}), ('stop', {})]:
        response = client.post('/api/infringement-knowledge/collection/' + action, json=payload)
        assert response.status_code == 202
    assert calls == [('open', ('org-a', '11'), 'lanjing'), ('confirm', ('org-a', '11')), ('stop', ('org-a', '11'))]


def test_viewer_cannot_open_confirm_or_stop(monkeypatch):
    authorize(monkeypatch, ['infringement_knowledge.view'])
    monkeypatch.setattr(collect.manager, 'start', lambda *args: pytest.fail('unauthorized browser access'))
    client = bit_interface.app.test_client()
    for action in ['open', 'confirm-login', 'stop']:
        assert client.post('/api/infringement-knowledge/collection/' + action, json={'source': 'zying'}).status_code == 403


def test_product_listing_only_reads_current_enterprise(monkeypatch):
    authorize(monkeypatch, ['infringement_knowledge.view'])
    calls = []
    monkeypatch.setattr(collect.manager.store, 'listing', lambda organization, **kwargs: calls.append((organization, kwargs)) or {'rows': []})
    response = bit_interface.app.test_client().get('/api/infringement-knowledge/collection/products?source=lanjing&page=2&search=Brand&organization=org-b')
    assert response.status_code == 200
    assert calls == [('org-a', {'source': 'lanjing', 'search': 'Brand', 'page': '2'})]
