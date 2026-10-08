from unittest.mock import Mock

import pytest

from bit import rights_holder_policy as policy
from bit import bit_store_link_remote_update as remote
from erp import rights_holder_policy as persistence
from erp import mercadolibre_infraction_store as infractions
from erp import store_link_audit as audit


@pytest.mark.parametrize("sales,retained", [(0, False), (5, False), (6, True), (20, True),
                                          (None, True), ("", True), (-1, True), (True, True), (1.5, True)])
def test_deletion_uses_latest_sales_and_protects_unknown_values(monkeypatch, sales, retained):
    client = Mock()
    client.get_marketplace_item.side_effect = [
        {"id": "MLM123", "status": "paused", "title": "Exact product", "sold_quantity": sales},
        {"id": "MLM123", "status": "deleted"},
    ]
    monkeypatch.setattr(remote, "MercadoLibreClient", lambda _: client)
    mark = Mock()
    monkeypatch.setattr(remote, "mark_store_links_deleted", mark)
    result = remote._delete_one_link(
        {"id": 1, "item_id": "MLM123", "sold_quantity": 5}, {},
        required_title="Exact product", max_sold_quantity=5,
    )
    if retained:
        assert result["status"] == "retained"
        assert result["retention_reason"]
        client.update_global_item.assert_not_called()
        mark.assert_not_called()
    else:
        assert result["status"] == "success"
        client.update_global_item.assert_called_once_with("MLM123", {"deleted": True})
        mark.assert_called_once_with([1])


def test_changed_title_is_preserved_before_any_write(monkeypatch):
    client = Mock()
    client.get_marketplace_item.return_value = {
        "id": "MLM123", "status": "active", "title": "Exact product Plus", "sold_quantity": 0}
    monkeypatch.setattr(remote, "MercadoLibreClient", lambda _: client)
    result = remote._delete_one_link({"id": 1, "item_id": "MLM123"}, {},
                                     required_title="Exact product", max_sold_quantity=5)
    assert result["status"] == "retained"
    client.update_global_item.assert_not_called()


def test_sales_increase_after_pause_restores_link_and_never_deletes(monkeypatch):
    client = Mock()
    client.get_marketplace_item.side_effect = [
        {"id": "MLM123", "status": status, "title": "Exact product", "sold_quantity": sales}
        for status, sales in [("active", 5), ("paused", 6), ("active", 6)]
    ]
    monkeypatch.setattr(remote, "MercadoLibreClient", lambda _: client)
    mark = Mock()
    monkeypatch.setattr(remote, "mark_store_links_deleted", mark)
    result = remote._delete_one_link({"id": 1, "item_id": "MLM123"}, {},
                                     required_title="Exact product", max_sold_quantity=5)
    assert result["status"] == "retained"
    assert [call.args for call in client.update_global_item.call_args_list] == [
        ("MLM123", {"status": "paused"}), ("MLM123", {"status": "active"})]
    mark.assert_not_called()


def test_groups_reports_and_continues_after_sibling_failure(monkeypatch):
    lease = Mock()
    lease.acquire.return_value = True
    monkeypatch.setattr(policy, "InterProcessLock", lambda *args, **kwargs: lease)
    products = Mock(return_value={"changed": 2, "product_ids": [1, 2]})
    monkeypatch.setattr(policy, "mark_products_infringing", products)
    links = [{"id": n, "token_id": n, "item_id": f"MLM{n}", "salesperson": "Alice"} for n in (1, 2, 3)]
    find = Mock(return_value=links)
    monkeypatch.setattr(policy, "same_title_links", find)
    monkeypatch.setattr(policy.bit_mysql, "get_mercado_store_token", lambda n: {"id": n, "access_token": "secret"})
    monkeypatch.setattr(policy, "_client_and_token", lambda token: (None, token))
    def delete(row, token, **kwargs):
        assert kwargs == {"required_title": "Product", "max_sold_quantity": 5}
        if row["id"] == 1:
            raise RuntimeError("remote unavailable")
        return {"status": "retained" if row["id"] == 2 else "success", "item_id": row["item_id"]}
    monkeypatch.setattr(policy, "_delete_one_link", delete)
    with pytest.raises(RuntimeError, match="部分处理失败"):
        policy.apply_rights_holder_records({"id": 1, "organization_key": "company-a"}, [
            {"source_type": "rights_holder", "title": " Product ", "item_id": "MLM1", "source_id": "case1"},
            {"source_type": "rights_holder", "title": "Product", "item_id": "MLM2", "source_id": "case2"},
        ])
    products.assert_called_once_with("Product")
    find.assert_called_once_with("Product", "company-a")
    lease.release.assert_called_once()
    history = audit.history(operation_type="rights_holder")
    assert history[0]["phase"] == "partial"
    assert [row["status"] for row in history[0]["details"]["result"]["links"]] == ["error", "retained", "success"]
    assert len(audit.history(operation_type="rights_holder", salesperson="Alice")) == 3
    assert "secret" not in str(history)


def test_resolved_and_detection_records_do_not_trigger_deletion(monkeypatch):
    process = Mock(return_value={"status": "success"})
    monkeypatch.setattr(policy, "process_rights_holder_product", process)
    policy.apply_rights_holder_records({"id": 1}, [
        {"source_type": "detection", "title": "P"},
        {"source_type": "rights_holder", "title": "P", "reason_code": "LEGACY_PAGE"},
        {"source_type": "rights_holder", "title": "P", "is_current": False, "resolution_status": "appeal_success"},
    ])
    process.assert_not_called()
    policy.apply_rights_holder_records({"id": 1}, [
        {"source_type": "rights_holder", "item_id": "MLM1", "title": "P", "is_current": False, "resolution_status": "appeal_failed"}])
    process.assert_called_once()


class Connection:
    def __init__(self, rows=(), fail=False):
        self.rows, self.fail = list(rows), fail
        self.queries, self.committed, self.closed, self.rolled_back = [], False, False, False
    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        if self.fail: raise RuntimeError("database unavailable")
    executemany = execute
    def fetchall(self): return self.rows
    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True
    def close(self): self.closed = True


def test_product_matches_are_exact_include_translations_and_update_every_match(monkeypatch):
    monkeypatch.setattr(persistence.products, "ensure_collection_tables", lambda _: None)
    db = Connection([{"id": 1, "review_status": "approved"}, {"id": 2, "review_status": "infringing"},
                     {"id": 3, "review_status": "unreviewed"}])
    result = persistence.mark_products_infringing("Product", connection_factory=lambda: db)
    assert result["matched"] == 3 and result["changed"] == 2
    assert result["product_ids"] == [1, 2, 3]
    query, params = db.queries[0]
    assert "LIKE" not in query and "BINARY" in query
    assert "title_es" in query and "title_pt" in query and "JSON_VALID" in query
    assert params == ("Product",) * 3
    assert db.queries[1][1] == (1, 3)
    assert db.committed and db.closed


def test_link_scope_excludes_other_companies_and_includes_known_deleted_history(monkeypatch):
    monkeypatch.setattr(persistence.links, "ensure_store_link_table", lambda _: None)
    db = Connection()
    persistence.same_title_links("Product", "company-a", connection_factory=lambda: db)
    query, params = db.queries[0]
    assert "BINARY TRIM(links.`title`) = BINARY %s" in query
    assert "organization_key" in query
    assert "<> 'deleted'" not in query
    assert params == ("Product", "company-a")


def test_repeated_report_does_not_rehydrate_or_delete_confirmed_deleted_link(monkeypatch):
    lease = Mock()
    lease.acquire.return_value = True
    monkeypatch.setattr(policy, "InterProcessLock", lambda *args, **kwargs: lease)
    monkeypatch.setattr(policy, "mark_products_infringing", lambda title: {"changed": 0})
    monkeypatch.setattr(policy, "same_title_links", lambda *args: [
        {"id": 1, "item_id": "MLM1", "status": "deleted", "is_current": 0},
        {"id": 2, "item_id": "MLM2", "status": "active", "is_current": 0},
    ])
    token = Mock()
    delete = Mock()
    monkeypatch.setattr(policy.bit_mysql, "get_mercado_store_token", token)
    monkeypatch.setattr(policy, "_delete_one_link", delete)
    result = policy.process_rights_holder_product({"id": 1}, "P", [{"item_id": "MLM1"}])
    assert result["status"] == "success"
    token.assert_not_called()
    delete.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_official_report_hook_runs_only_after_commit_and_connection_close(monkeypatch, failure):
    monkeypatch.setattr(infractions, "ensure_infraction_tables", lambda _: None)
    db = Connection(fail=failure)
    applied = []
    def apply(token, rows):
        assert db.committed and db.closed
        applied.append(rows)
    monkeypatch.setattr(policy, "apply_rights_holder_records", apply)
    records = [{"source_type": "rights_holder", "source_id": "case", "item_id": "MLM1", "title": "P"}]
    if failure:
        with pytest.raises(RuntimeError):
            infractions.upsert_infraction_records({"id": 1}, records, connection_factory=lambda: db)
        assert applied == [] and db.rolled_back
    else:
        assert infractions.upsert_infraction_records({"id": 1}, iter(records), connection_factory=lambda: db) == 1
        assert applied == [records]


def test_page_snapshot_hook_is_also_connected(monkeypatch):
    monkeypatch.setattr(infractions, "ensure_infraction_tables", lambda _: None)
    db = Connection()
    hook = Mock()
    monkeypatch.setattr(policy, "apply_rights_holder_records", hook)
    infractions.replace_pppi_snapshot({"id": 1}, "MLM", [], connection_factory=lambda: db)
    hook.assert_called_once_with({"id": 1}, [])
    assert db.committed and db.closed
