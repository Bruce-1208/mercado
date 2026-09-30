from unittest.mock import Mock, patch

import pytest

from bit import weight_dimensions_records as records
from mercado_api.client import MercadoLibreClient


@pytest.mark.parametrize("dimensions", ["14x13x13", ""])
def test_package_attributes_reads_marketplace_endpoint_before_global_write(dimensions):
    client = MercadoLibreClient("test")
    original = [
        {"id": "BRAND", "value_name": "Example"},
        {"id": "PACKAGE_WEIGHT", "value_name": "500 g", "value_struct": {"number": 500, "unit": "g"},
         "values": [{"name": "500 g"}]},
        {"id": "PACKAGE_LENGTH", "value_name": "10 cm"},
        {"id": "PACKAGE_WIDTH", "value_name": "8 cm"},
        {"id": "PACKAGE_HEIGHT", "value_name": "6 cm"},
    ]
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET" and path == "/marketplace/items/CBT3588524519":
            return {"attributes": original if len(calls) == 1 else calls[1][2]["json_body"]["attributes"]}
        if method == "PUT" and path == "/global/items/CBT3588524519":
            return {}
        raise AssertionError(f"Unexpected endpoint: {method} {path}")

    client.request = request
    records._package_attributes(client, "CBT3588524519", "866", dimensions)
    assert [c[:2] for c in calls] == [
        ("GET", "/marketplace/items/CBT3588524519"),
        ("PUT", "/global/items/CBT3588524519"),
        ("GET", "/marketplace/items/CBT3588524519"),
    ]
    attributes = calls[1][2]["json_body"]["attributes"]
    values = {a["id"]: a["value_name"] for a in attributes}
    expected = {"PACKAGE_WEIGHT": "866 g",
                "PACKAGE_LENGTH": "14 cm" if dimensions else "10 cm",
                "PACKAGE_WIDTH": "13 cm" if dimensions else "8 cm",
                "PACKAGE_HEIGHT": "13 cm" if dimensions else "6 cm"}
    if dimensions:
        expected.update(PACKAGE_WIDTH="13 cm", PACKAGE_HEIGHT="13 cm")
    assert values == expected
    assert len(attributes) == len(expected)
    assert all(set(a) == {"id", "value_name"} for a in attributes)
    assert original[1]["value_name"] == "500 g"


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


@pytest.mark.parametrize("failed_item", [None, "MLB2", "CBT2"])
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
    def update_package(_client, global_id, *_args):
        if global_id == failed_item:
            raise RuntimeError("package write failed")

    with patch.object(records, "_tasks", {"test": task}), \
         patch.object(records.bit_mysql, "get_mercado_store_token", return_value={}), \
         patch.object(records, "_client_and_token", return_value=(client, {})), \
         patch.object(records, "MercadoLibreClient", return_value=client), \
         patch.object(records.bit_db_api, "list_mercado_store_links", return_value={"pages": 1, "rows": [
             {"token_id": 2, "item_id": "MLB2", "title": "Same"}]}), \
         patch.object(records, "_package_attributes", side_effect=update_package) as package, \
         patch.object(records.bit_db_api, "append_weight_dimensions_record_log"):
        records._run_execute("test", "user", action="zeshun")
    assert package.call_count == 2
    assert {c.args[1:] for c in package.call_args_list} == {("CBT1", "600", "10x5x5"), ("CBT2", "600", "10x5x5")}
    expected_net = {("MLM1", 11)} if failed_item == "CBT2" else {("MLM1", 11), ("MLB2", 22)}
    assert {(c.args[0], c.args[1]["net_proceeds"]) for c in client.update_global_item.call_args_list} == expected_net
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


@pytest.mark.parametrize("failure", ["missing_global", "closed_store", "remote_error"])
def test_failed_sibling_does_not_block_valid_links_or_claim_full_success(failure):
    row = order()
    client = Mock()
    def read(item, **kwargs):
        if item == "MLB2" and failure == "remote_error":
            raise RuntimeError("remote unavailable")
        return {"title": "Same", "cbt_item_id": "" if item == "MLB2" else "CBT1"}
    client.get_marketplace_item.side_effect = read
    def token(token_id):
        if token_id == 2 and failure == "closed_store":
            raise RuntimeError("该店铺已关闭")
        return {}
    with patch.object(records.bit_mysql, "get_mercado_store_token", side_effect=token), \
         patch.object(records, "_client_and_token", return_value=(client, {})), \
         patch.object(records.bit_db_api, "list_mercado_store_links", return_value={"pages": 1, "rows": [
             {"token_id": 2, "item_id": "MLB2", "title": "Same"}]}), \
         patch.object(records.bit_db_api, "append_weight_dimensions_record_log"):
        children = records._expand_same_title_records([row], [1, 2])
        assert len(children) == 2
        valid = next(c for c in children if c["marketplace_item_id"] == "MLM1")
        valid["_zeshun_status"] = "成功"
        assert next(c for c in children if c["marketplace_item_id"] == "MLB2")["_expansion_error"]
        records._finish_same_title_records([row], children)
    assert row["execution_status"] == "部分完成"
    assert "MLB2" in row["execution_error"]
    assert "1/2" in row["execution_logs"][-1]["message"]


def package_item(weight="500", user_product_id=None):
    return {"user_product_id": user_product_id, "attributes": [
        {"id": "PACKAGE_WEIGHT", "value_name": weight + " g"},
        *[{"id": "PACKAGE_" + side, "value_name": "10 cm"} for side in ("LENGTH", "WIDTH", "HEIGHT")],
    ]}


def test_user_product_still_uses_compatible_global_item_endpoint():
    client = Mock()
    client.get_marketplace_item.side_effect = [package_item(user_product_id="CBTU123"), package_item("600")]
    records._package_attributes(client, "CBT1", "600", "10x10x10")
    assert client.update_global_item.call_args.args[0] == "CBT1"
    client.request.assert_not_called()


def test_success_response_without_changed_measurements_is_not_success():
    client = Mock()
    client.get_marketplace_item.return_value = package_item()
    with patch.object(records.time, "sleep"), pytest.raises(RuntimeError, match="回读尚未确认"):
        records._package_attributes(client, "CBT1", "600", "10x10x10")
    assert client.update_global_item.call_count == 1


def test_delayed_readback_retries_reads_without_repeating_write():
    client = Mock()
    client.get_marketplace_item.side_effect = [package_item(), package_item(), package_item(), package_item("600")]
    with patch.object(records.time, "sleep") as sleep:
        records._package_attributes(client, "CBT1", "600", "10x10x10")
    assert client.update_global_item.call_count == 1
    assert [c.args[0] for c in sleep.call_args_list] == [1, 2]


def test_missing_dimensions_cannot_be_invented_for_weight_only_update():
    client = Mock()
    client.get_marketplace_item.return_value = {"attributes": []}
    with pytest.raises(ValueError, match="无法保留原尺寸"):
        records._package_attributes(client, "CBT1", "600", "")
    client.update_global_item.assert_not_called()


def test_equal_measurements_skip_remote_write_with_unit_conversion():
    client = Mock()
    remote = package_item()
    remote["attributes"][0]["value_name"] = "0.6 kg"
    client.get_marketplace_item.return_value = remote
    records._package_attributes(client, "CBT1", "600", "")
    client.update_global_item.assert_not_called()


@pytest.mark.parametrize("weight,dimensions", [("49", "10x10x10"), ("500", "10x10x2"), ("500", "NaNx10x10")])
def test_invalid_real_measurements_are_not_silently_padded(weight, dimensions):
    client = Mock()
    with pytest.raises(ValueError):
        records._package_attributes(client, "CBT1", weight, dimensions)
    client.update_global_item.assert_not_called()
    client.request.assert_not_called()


def test_full_api_cause_is_preserved():
    error = RuntimeError('PUT /global/items/CBT1 失败 (400): {"message":"Error calling Global Update API","cause":[{"code":"item.dimensions","message":"height is invalid"}]}')
    assert "height is invalid" in records._update_error(error)


def test_history_persists_child_target_and_immutable_submission_snapshot():
    parent = order()
    child = {**parent, "_execution_parent": parent, "marketplace_item_id": "MLB2", "_global_item_id": "CBT2",
             "_execution_id": "run-1", "_execution_action": "zeshun", "_execution_operator": "user:7",
             "_submitted_weight_g": "600", "_submitted_dimensions_cm": "20x10x5",
             "current_net_proceeds_usd": "12.34"}
    with patch.object(records.bit_db_api, "append_weight_dimensions_record_log") as save:
        records._log_execution(child, "净收益", "成功", "已重提净收益")
    number, event, updates = save.call_args.args
    child["_submitted_weight_g"] = "999"
    assert number == parent["order_number"]
    assert event["marketplace_item_id"] == "MLB2"
    assert event["global_item_id"] == "CBT2"
    assert event["submitted_weight_g"] == "600"
    assert event["submitted_dimensions_cm"] == "20x10x5"
    assert event["net_proceeds_usd"] == "12.34"
    assert event["execution_id"] == "run-1"
    assert event["operator"] == "user:7"
    assert parent["execution_logs"][-1] == event
    assert "current_net_proceeds_usd" not in updates, "a sibling's net must not replace the original listing's net"


@pytest.mark.parametrize("closed_original", [False, True])
@pytest.mark.parametrize("state", [{"status": "closed"}, {"status": "closed", "sub_status": ["deleted"]}])
def test_closed_listings_skip_without_global_and_do_not_block_active_updates(closed_original, state):
    row = order()
    client = Mock(access_token="test", timeout=10)
    def read(item, **kwargs):
        closed = item == "MLB2" or closed_original
        return {"id": item, "title": "Same", "cbt_item_id": "" if closed else "CBT1",
                **(state if closed else {"status": "active"}),
                "net_proceeds": [{"currency_id": "USD", "amount": 4.77}]}
    client.get_marketplace_item.side_effect = read
    task = {"owner": "user", "records": [row], "authorized_ids": [1, 2]}
    with patch.object(records, "_tasks", {"test": task}), \
         patch.object(records.bit_mysql, "get_mercado_store_token", return_value={}), \
         patch.object(records, "_client_and_token", return_value=(client, {})), \
         patch.object(records, "MercadoLibreClient", return_value=client), \
         patch.object(records.bit_db_api, "list_mercado_store_links", return_value={"pages": 1, "rows": [
             {"token_id": 2, "item_id": "MLB2", "title": "Same"}]}), \
         patch.object(records, "_package_attributes") as package, \
         patch.object(records.bit_db_api, "append_weight_dimensions_record_log"):
        records._run_execute("test", "user", action="zeshun")
    assert package.call_count == (0 if closed_original else 1)
    assert client.update_global_item.call_count == (0 if closed_original else 1)
    assert row["execution_status"] == ("跳过" if closed_original else "完成")
    assert row["execution_error"] == ""
    skipped = [l for l in row["execution_logs"] if l["status"] == "跳过"]
    assert any(l.get("marketplace_item_id") == "MLB2" for l in skipped)
    assert "跳过已关闭或删除链接" in row["execution_logs"][-1]["message"]


@pytest.mark.parametrize('wrapped', [True, False])
def test_history_reads_site_settings_rows(wrapped):
    settings = [{'site_id': 'MLM', 'salesperson': 'Alice', 'group_name': 'Group'}]
    payload = {'token_id': 1, 'rows': settings} if wrapped else settings
    with patch.object(records.bit_db_api, 'list_mercado_store_site_settings', return_value=payload):
        context = records._order_history_context(order(), {})
        listing = records._listing_history_context(order(), 1, 'MLM1', {}, {}, {})
    assert context['salesperson'] == listing['salesperson'] == 'Alice'
    assert context['store_group'] == listing['store_group'] == 'Group'


def test_server_zying_updates_latest_product_without_marketplace_writes():
    from contextlib import contextmanager
    rows = [order('1', product_id='P1'), order('2', product_id='P1')]
    rows[1]['actual_weight_g'] = '650'
    task = {'owner': 'user', 'status': 'ready', 'records': rows}
    browser = Mock()

    @contextmanager
    def factory(log):
        yield browser

    def run_thread(**kwargs):
        thread = Mock()
        thread.start.side_effect = lambda: kwargs['target'](*kwargs['args'])
        return thread

    with patch.object(records, '_tasks', {'test': task}), \
         patch.object(records.threading, 'Thread', side_effect=run_thread), \
         patch.object(records.bit_db_api, 'list_mercado_store_site_settings', return_value={'rows': []}), \
         patch.object(records.bit_db_api, 'append_weight_dimensions_record_log'), \
         patch.object(records.bit_mysql, 'get_mercado_store_token') as token, \
         patch.object(records, '_package_attributes') as package:
        records.start_execute('test', 'user', action='zying', selected_order_numbers=['1', '2'], erp_browser_factory=factory)
    browser.update_package_by_product_id.assert_called_once_with('P1', '650', '10x5x5')
    token.assert_not_called()
    package.assert_not_called()
    assert task['execute_status'] == 'completed'
    assert all(row['_zying_status'] == '成功' for row in rows)
    assert all('_zeshun_status' not in row for row in rows)
