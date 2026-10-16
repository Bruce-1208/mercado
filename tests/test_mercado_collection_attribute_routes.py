import pytest

from test_browser_extension_management_categories import workbench, HEADERS  # noqa: F401
from test_mercado_attribute_preflight_routes import logged_client


def test_auto_check_is_read_only_and_uses_authorized_saved_rows(workbench, monkeypatch):
    from erp import mercadolibre_batch_publish as batch
    from erp import mercadolibre_follow_sell as publisher
    access, reads, tokens = [], [], []
    monkeypatch.setattr(workbench.bit_db_api, "check_catalog_access", lambda *a, **kw: access.append((a, kw)))
    monkeypatch.setattr(workbench, "db_get_mercado_collection_items_by_ids", lambda ids: reads.append(ids) or [
        {"id": 91, "category_id": "MLM123", "source": {"attributes": [{"id": "BRAND", "value_name": "Acme"}]}}
    ])
    monkeypatch.setattr(workbench.bit_db_api, "list_mercado_store_tokens", lambda: {"rows": [{"id": 7}, {"id": 99}]})
    monkeypatch.setattr(workbench, "_filter_mercado_tokens_for_user", lambda data: {"rows": [data["rows"][0]]})
    monkeypatch.setattr(batch, "DatabaseMercadoLibreClient", lambda token, **kw: tokens.append(token) or object())
    monkeypatch.setattr(publisher, "_category_attribute_schema", lambda *a: [{"id": "MODEL", "tags": {"required": True}}])
    response = logged_client(workbench).get('/api/mercado-collection/attribute-check?ids=91')
    assert response.status_code == 200, response.json
    assert response.headers["Cache-Control"] == "no-store"
    assert access == [(("collection", [91]), {"operate": False})]
    assert reads == [[91]] and tokens == [7]
    assert response.json["data"]["results"][0]["missing"][0]["attribute_id"] == "MODEL"


def test_denied_check_never_reads_product_or_category(workbench, monkeypatch):
    def denied(*a, **kw):
        raise PermissionError()
    monkeypatch.setattr(workbench.bit_db_api, "check_catalog_access", denied)
    monkeypatch.setattr(workbench, "db_get_mercado_collection_items_by_ids", lambda ids: pytest.fail("unauthorized read"))
    assert logged_client(workbench).get('/api/mercado-collection/attribute-check?ids=91').status_code == 403


@pytest.mark.parametrize("ids", ["", "0", "-1", "x", ",".join(str(i) for i in range(1, 12))])
def test_check_rejects_invalid_or_unbounded_ids(workbench, monkeypatch, ids):
    monkeypatch.setattr(workbench.bit_db_api, "check_catalog_access", lambda *a, **kw: pytest.fail("invalid ids"))
    assert logged_client(workbench).get('/api/mercado-collection/attribute-check?ids=' + ids).status_code == 400


def test_check_requires_login(workbench):
    assert workbench.app.test_client().get('/api/mercado-collection/attribute-check?ids=91').status_code in {302, 401}
