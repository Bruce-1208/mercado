from unittest.mock import Mock, patch

import pytest

from bit import weight_dimensions_records as records


def order(number="1", **changes):
    return dict(order_number=number, query_status="已读取",
                _dimension_compensation_confirmed=True, _token_id=1,
                _global_item_id="CBT1", marketplace_item_id="MLM1",
                actual_weight_g="400", actual_dimensions_cm="10x5x5",
                time=f"2026-09-{int(number):02} 00:00:00", **changes)


def test_expansion_pages_exact_title_scope_and_duplicate_links():
    client = Mock()
    client.get_marketplace_item.side_effect = lambda item, **kw: {
        "id": item, "title": "Same", "cbt_item_id": "CBT" + item[-1]}
    listing = Mock(side_effect=[
        {"pages": 2, "rows": [{"token_id": 2, "item_id": "MLB2", "title": "Same"},
                                {"token_id": 3, "item_id": "MLB3", "title": "Same"},
                                {"token_id": 1, "item_id": "MLM4", "title": "Same extra"}]},
        {"pages": 2, "rows": [{"token_id": 2, "item_id": "MLB2", "title": "Same"}]},
    ])
    with patch.object(records.bit_db_api, "list_mercado_store_links", listing), \
         patch.object(records.bit_mysql, "get_mercado_store_token", return_value={}), \
         patch.object(records, "_client_and_token", return_value=(client, {})), \
         patch.object(records.bit_db_api, "append_weight_dimensions_record_log"):
        result = records._expand_same_title_records([order()], [1, 2])
    assert {(r["_token_id"], r["marketplace_item_id"]) for r in result} == {(1, "MLM1"), (2, "MLB2")}
    assert [c.kwargs["page"] for c in listing.call_args_list] == [1, 2]
    assert all(c.kwargs["token_ids"] == [1, 2] for c in listing.call_args_list)


@pytest.mark.parametrize("failed_item", [None, "MLB2"])
def test_execute_deduplicates_latest_measurements_and_keeps_each_net(failed_item):
    originals = [order("1"), order("2")]
    originals[1]["actual_weight_g"] = "600"
    task = {"owner": "user", "records": originals, "authorized_ids": [1, 2]}
    client = Mock(access_token="test", timeout=10)
    def read(item, **kwargs):
        return {"id": item, "title": "Same", "cbt_item_id": "CBT" + item[-1],
                "net_proceeds": [{"currency_id": "USD", "amount": 11 if item == "MLM1" else 22}]}
    client.get_marketplace_item.side_effect = read
    def write(item, changes):
        if item == failed_item:
            raise RuntimeError("remote write failed")
    client.update_global_item.side_effect = write
    with patch.object(records, "_tasks", {"test": task}), \
         patch.object(records.bit_mysql, "get_mercado_store_token", return_value={}), \
         patch.object(records, "_client_and_token", return_value=(client, {})), \
         patch.object(records, "MercadoLibreClient", return_value=client), \
         patch.object(records.bit_db_api, "list_mercado_store_links", return_value={"pages": 1, "rows": [
             {"token_id": 2, "item_id": "MLB2", "title": "Same"}]}), \
         patch.object(records, "_package_attributes") as package, \
         patch.object(records.bit_db_api, "append_weight_dimensions_record_log"):
        records._run_execute("test", "user", action="zeshun")
    assert package.call_count == 2
    assert {c.args[1:] for c in package.call_args_list} == {("CBT1", "600", "10x5x5"), ("CBT2", "600", "10x5x5")}
    assert {(c.args[0], c.args[1]["net_proceeds"]) for c in client.update_global_item.call_args_list} == {("MLM1", 11), ("MLB2", 22)}
    assert all(r["_zeshun_status"] == ("失败" if failed_item else "成功") for r in originals)
    assert all(r["current_net_proceeds_usd"] == "11" for r in originals)
    if failed_item:
        assert all("MLB2" in r["execution_error"] for r in originals)


def test_lookup_failure_blocks_success_and_does_not_expand():
    row = order()
    with patch.object(records.bit_mysql, "get_mercado_store_token", side_effect=RuntimeError("unavailable")), \
         patch.object(records.bit_db_api, "append_weight_dimensions_record_log"):
        assert records._expand_same_title_records([row], [1]) == []
    assert row["_zeshun_status"] == "失败"
