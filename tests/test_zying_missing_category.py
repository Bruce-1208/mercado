from unittest.mock import Mock

import pytest

from bit import bit_zying_caiji as collector


def record(category_id="4159"):
    return {"product_id": "123", "title": "Product", "_category_site": "CBT",
            "_category_id": category_id}


def test_empty_category_retries_then_keeps_reference_and_snapshot_warning(monkeypatch):
    read = Mock(return_value=[{"root": []}])
    retry = Mock(return_value={"root": []})
    monkeypatch.setattr(collector, "_zying_api_read_many", read)
    monkeypatch.setattr(collector, "_zying_api_post", retry)

    @collector._reuse_zying_frontend_signer
    def run():
        for _ in range(2):
            rows = [record(), record()]
            collector._enrich_product_categories("token", rows)
            for row in rows:
                assert row["product_category_id"] == "CBT4159"
                assert row["product_category"] == ""
                assert row["category_lookup_status"] == "missing"
                assert "分类名称待补全" in row["raw_text"]
                snapshot = collector._finalize_zying_listing_snapshot(row)["listing_snapshot"]
                assert snapshot["source"]["category_id"] == "CBT4159"
                assert snapshot["plugin_snapshot"]["category_lookup_status"] == "missing"
    run()
    assert read.call_count == retry.call_count == 1
    run()
    assert read.call_count == retry.call_count == 2


def test_empty_category_retry_can_restore_the_real_name(monkeypatch):
    monkeypatch.setattr(collector, "_zying_api_read_many", lambda *a: [{"root": []}])
    monkeypatch.setattr(collector, "_zying_api_post", lambda *a, **kw: {
        "root": [{"cate_cateid": "CBT4159", "cate_name": "Recovered category"}]})
    rows = [record()]
    collector._enrich_product_categories("token", rows)
    assert rows[0]["product_category"] == "Recovered category"
    assert rows[0]["category_lookup_status"] == "resolved"
    assert rows[0]["category_lookup_warning"] == ""


@pytest.mark.parametrize("error", [collector.ZyingAuthenticationError,
                                  collector.ZyingRateLimitError,
                                  collector.ZyingCollectionStopped, RuntimeError])
def test_retry_errors_are_not_converted_to_missing_category(monkeypatch, error):
    monkeypatch.setattr(collector, "_zying_api_read_many", lambda *a: [{"root": []}])
    monkeypatch.setattr(collector, "_zying_api_post", Mock(side_effect=error("stop")))
    rows = [record()]
    with pytest.raises(error, match="stop"):
        collector._enrich_product_categories("token", rows)
    assert "product_category_id" not in rows[0]


@pytest.mark.parametrize("data", [{}, {"root": None}, {"root": {}},
                                  {"root": [None]}, {"root": [{}]}])
def test_malformed_category_response_still_fails(monkeypatch, data):
    monkeypatch.setattr(collector, "_zying_api_read_many", lambda *a: [data])
    retry = Mock()
    monkeypatch.setattr(collector, "_zying_api_post", retry)
    with pytest.raises(RuntimeError, match="本页未入库"):
        collector._enrich_product_categories("token", [record()])
    retry.assert_not_called()


def test_missing_category_does_not_prevent_full_page_reaching_both_writers(monkeypatch):
    monkeypatch.setattr(collector, "list_zying_product_developers", lambda token: [])
    monkeypatch.setattr(collector, "_load_zying_category_name_lookup", lambda reader: {})

    def api_post(session, token, command, payload, **kwargs):
        if command == "sale.stat":
            return {"list": {"data": [{"id": i, "title": f"Product {i}"} for i in (1, 2)]}}
        if command == "sale.detail":
            return {"root": [{"sale_id": payload["id"], "sale_weight": 240,
                              "sale_size": [9, 5, 11], "sale_siteid": 8,
                              "sale_attrs": {"8": {"site": "CBT", "kindid":
                                  "4159" if payload["id"] == "1" else "430974"}}}]}
        assert command == "meli_category.detail"
        return {"root": [] if payload["id"] == "4159" else [
            {"cate_cateid": "CBT430974", "cate_name": "Known category"}]}

    monkeypatch.setattr(collector, "_zying_api_post", api_post)
    monkeypatch.setattr(collector, "_zying_api_read_many", lambda token, command, payloads: [
        api_post(None, token, command, payload) for payload in payloads])
    writes = []
    collector.collect_zying_products_api(
        auth_token="token", max_items=2, existing_product_id_reader=lambda ids: set(),
        product_writer=lambda rows: writes.append(rows) or len(rows),
        product_mirror_writer=lambda rows: writes.append(rows) or {"count": len(rows)},
    )
    assert len(writes) == 2
    for rows in writes:
        assert [r["product_category_id"] for r in rows] == ["CBT4159", "CBT430974"]
        assert [r["category_lookup_status"] for r in rows] == ["missing", "resolved"]
        assert all(r["listing_snapshot"]["weight_g"] == 240 for r in rows)
