from pathlib import Path

import pytest
from bit import mercado_official_news as news


def test_public_news_uses_official_categories_and_sorts_without_store_tokens(monkeypatch):
    monkeypatch.setattr(news, '_cache', None)
    def fetch(name, slug):
        return [{'id': slug, 'title': name, 'from_date': {
            'latam': '2026-09-01T00:00:00Z', 'policy-updates': '2026-09-20T00:00:00Z',
            'platform-updates': '2026-09-10T00:00:00Z',
        }[slug]}]
    monkeypatch.setattr(news, 'fetch_category', fetch)
    data = news.get_official_news()
    assert [r['id'] for r in data['notices']] == ['policy-updates', 'platform-updates', 'latam']
    assert data['failed_sources'] == []
    monkeypatch.setattr(news, 'fetch_category', lambda *a: pytest.fail('cache missed'))
    assert news.get_official_news() == data


def test_news_partial_failure_and_all_failed_are_not_empty_success(monkeypatch):
    monkeypatch.setattr(news, '_cache', None)
    def fetch(name, slug):
        if slug != 'latam':
            raise ValueError('upstream failed')
        return [{'id': '1', 'from_date': '2026-09-01'}]
    monkeypatch.setattr(news, 'fetch_category', fetch)
    assert news.get_official_news()['failed_sources'] == ['平台功能更新', '政策新规']
    monkeypatch.setattr(news, 'fetch_category', lambda *a: (_ for _ in ()).throw(ValueError()))
    with pytest.raises(RuntimeError):
        news.get_official_news()


def test_normalize_public_news_ignores_external_urls_and_strips_html(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {'Data': {'data': [
                {'post_guid': 'abc', 'title': '<b>政策调整</b>', 'summary': '新规 &amp; 更新',
                 'publish_time': '2026-09-20', 'source_url': 'https://untrusted.example'},
                {'title': 'missing identifier'},
            ]}}
    monkeypatch.setattr(news.requests, 'get', lambda *a, **k: Response())
    rows = news.fetch_category('政策新规', 'policy-updates')
    assert len(rows) == 1
    assert rows[0]['title'] == '政策调整'
    assert rows[0]['description'] == '新规 & 更新'
    assert rows[0]['action_url'] == 'https://mercadolibre.cn/news/info?post_guid=abc&type=policy-updates'


def test_news_and_store_reminders_have_separate_routes_and_permissions(monkeypatch):
    from bit import bit_interface as web
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: {
        'id': 12, 'username': 'tester', 'permissions': ['reputation.view'], 'access_version': 1,
    })
    monkeypatch.setattr(news, 'get_official_news', lambda **kw: {'notices': [{'title': '官方新闻'}]})
    monkeypatch.setattr(web.bit_db_api, 'list_mercado_store_tokens', lambda: {'rows': []})
    client = web.app.test_client()
    assert client.get('/api/overview/mercado-notices').get_json()['data']['notices'][0]['title'] == '官方新闻'
    assert client.get('/api/reputation/mercado-notices').get_json()['data']['total_stores'] == 0
    monkeypatch.setattr(web, 'get_current_workbench_user', lambda: {
        'id': 12, 'username': 'tester', 'permissions': ['customer_service.view'], 'access_version': 1,
    })
    assert client.get('/api/reputation/mercado-notices').status_code == 403


def test_reminders_panel_is_inside_reputation_page():
    template = (Path(__file__).parents[1] / 'bit/templates/index.html').read_text()
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(template, 'html.parser')
    assert soup.select_one('#tab-reputation #reputation-mercado-notices')
    assert soup.select_one('#tab-overview #overview-mercado-notices')
    assert not soup.select_one('#tab-overview #reputation-mercado-notices')
